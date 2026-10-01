"""Read-only AWS rollout assertions. Requires Helm and PyYAML."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import Any, Iterator

import yaml

CHART = Path(__file__).resolve().parents[1]


def render(*extra: str) -> list[dict[str, Any]]:
    output = subprocess.check_output(
        [
            "helm",
            "template",
            "nous-dev-aws",
            str(CHART),
            "-f",
            str(CHART / "values.yaml"),
            "-f",
            str(CHART / "values-aws.yaml"),
            *extra,
        ],
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(output) if doc]


def pod_specs(
    docs: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for doc in docs:
        if doc["kind"] in ("Deployment", "Job"):
            yield doc, doc["spec"]["template"]["spec"]
        elif doc["kind"] == "CronJob":
            yield doc, doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]


class DeploymentConsistency(unittest.TestCase):
    def test_migration_completes_before_all_new_consumers(self) -> None:
        docs = render()
        jobs = [
            d
            for d in docs
            if d["kind"] == "Job"
            and d["metadata"]["labels"].get("app.kubernetes.io/component")
            == "migrations"
        ]
        self.assertEqual(len(jobs), 1, "AWS needs one migration writer")
        job = jobs[0]
        wave = int(job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        self.assertEqual(wave, 1, "secrets and service accounts must sync first")
        self.assertNotIn("argocd.argoproj.io/hook", job["metadata"]["annotations"])
        self.assertNotIn("ttlSecondsAfterFinished", job["spec"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        migration = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(migration["command"], ["alembic", "upgrade", "heads"])
        consumers = []
        for doc, pod in pod_specs(docs):
            component = doc["metadata"]["labels"].get("app.kubernetes.io/component")
            if component not in (
                "backend",
                "celery-worker",
                "celery-beat",
                "synthetic-traffic",
            ):
                continue
            consumers.append(component)
            self.assertGreater(
                int(doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
                wave,
            )
            self.assertNotIn(
                "run-migrations", [c["name"] for c in pod.get("initContainers", [])]
            )
            self.assertEqual(pod["containers"][0]["image"], migration["image"])
            self.assertEqual(pod["containers"][0]["envFrom"], migration["envFrom"])
        self.assertCountEqual(
            consumers, ["backend", "celery-worker", "celery-beat", "synthetic-traffic"]
        )

    def test_job_identity_changes_with_image_and_config(self) -> None:
        def name(*extra: str) -> str:
            return next(
                str(d["metadata"]["name"]) for d in render(*extra) if d["kind"] == "Job"
            )

        original = name()
        self.assertEqual(original, name())
        self.assertLessEqual(len(original), 63)
        job = next(d for d in render() if d["kind"] == "Job")
        policy = job["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"]
        changed_policy = "Always" if policy != "Always" else "IfNotPresent"
        self.assertNotEqual(
            original, name("--set", "backend.image.pullPolicy=" + changed_policy)
        )
        self.assertNotEqual(
            original, name("--set", "backend.image.digest=sha256:" + "a" * 64)
        )
        self.assertNotEqual(
            original,
            name(
                "--set",
                "backend.env[0].name=ENVIRONMENT",
                "--set",
                "backend.env[0].value=changed",
            ),
        )

    def test_unique_env_and_synthetic_override_precedence(self) -> None:
        docs = render(
            "--set",
            "syntheticTraffic.env[0].name=ENVIRONMENT",
            "--set",
            "syntheticTraffic.env[0].value=override",
            "--set",
            "syntheticTraffic.env[1].name=PYTHONPATH",
            "--set",
            "syntheticTraffic.env[1].value=/override",
        )
        for doc, pod in pod_specs(docs):
            for container in pod.get("containers", []) + pod.get("initContainers", []):
                names = [e["name"] for e in container.get("env", [])]
                self.assertEqual(
                    len(names),
                    len(set(names)),
                    f"{doc['kind']}/{doc['metadata']['name']}/{container['name']} has duplicate env",
                )
                if container["name"] == "synthetic-traffic":
                    env = {e["name"]: e.get("value") for e in container["env"]}
                    self.assertEqual(env["ENVIRONMENT"], "override")
                    self.assertEqual(env["PYTHONPATH"], "/override")


if __name__ == "__main__":
    unittest.main()
