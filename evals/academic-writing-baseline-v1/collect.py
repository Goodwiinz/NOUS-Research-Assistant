#!/usr/bin/env python3
"""Validate and aggregate retained GOO-293 writing trial artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = Path(__file__).resolve().parent
DOC_MARKER_RE = re.compile(r"\[Doc (\d+)\]")
VERDICTS = {
    "objective": {"passed", "failed", "not_run"},
    "semantic": {"passed", "failed", "not_run", "judge_unavailable"},
    "authorization": {"passed", "failed", "not_run"},
    "infrastructure": {"passed", "failed"},
}
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential)",
    re.IGNORECASE,
)


class EvidenceError(ValueError):
    """Raised when retained trial evidence violates the declared protocol."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read JSON evidence {path}: {exc}") from exc


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _file_digest(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"cannot read retained artifact {path}: {exc}") from exc
    return f"sha256:{digest}"


def _resolve_artifact(bundle_path: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise EvidenceError(f"{bundle_path.name}: artifact path is required")
    candidate = (bundle_path.parent / raw_path).resolve()
    bundle_root = bundle_path.parent.resolve()
    if candidate != bundle_root and bundle_root not in candidate.parents:
        raise EvidenceError(f"{bundle_path.name}: artifact escapes its trial directory")
    if not candidate.is_file():
        raise EvidenceError(
            f"{bundle_path.name}: retained artifact is missing: {raw_path}"
        )
    return candidate


def _verify_declared_artifact(bundle_path: Path, record: Any) -> Path:
    if not isinstance(record, dict):
        raise EvidenceError(f"{bundle_path.name}: artifact record must be an object")
    path = _resolve_artifact(bundle_path, record.get("path"))
    observed = _file_digest(path)
    if record.get("sha256") != observed:
        raise EvidenceError(
            f"{bundle_path.name}: checksum mismatch for {record.get('path')}"
        )
    return path


def _reject_sensitive_configuration(value: Any, path: str = "configuration") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if (
                SENSITIVE_KEY_RE.search(str(key))
                and child is not None
                and child != ""
                and child is not False
            ):
                raise EvidenceError(
                    f"retained runtime configuration contains sensitive field {child_path}"
                )
            _reject_sensitive_configuration(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_configuration(child, f"{path}[{index}]")


def _source_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _harness_digest() -> str:
    files = [TASK_DIR / "collect.py"]
    lines = [f"{_file_digest(path)}  {path.relative_to(ROOT)}" for path in files]
    return _canonical_digest(lines)


def _corpus(protocol: dict[str, Any], name: str) -> tuple[Path, dict[str, Any]]:
    relative = protocol.get("corpora", {}).get(name)
    if not isinstance(relative, str):
        raise EvidenceError(f"protocol corpus {name!r} is missing")
    path = (TASK_DIR / relative).resolve()
    value = _load_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        raise EvidenceError(f"corpus {name!r} must contain a tasks list")
    return path, value


def _task_index(
    protocol: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    tasks: dict[str, dict[str, Any]] = {}
    cohorts: dict[str, str] = {}
    seen_digests: dict[str, str] = {}
    for cohort in ("development", "held_out"):
        _, corpus = _corpus(protocol, cohort)
        for task in corpus["tasks"]:
            task_id = task.get("id") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise EvidenceError(f"{cohort} corpus contains a task without an id")
            if task_id in tasks:
                raise EvidenceError(f"duplicate task id across corpora: {task_id}")
            digest = _canonical_digest(task)
            if digest in seen_digests:
                raise EvidenceError(
                    f"development and held-out tasks overlap: {task_id} and "
                    f"{seen_digests[digest]}"
                )
            seen_digests[digest] = task_id
            tasks[task_id] = task
            cohorts[task_id] = cohort
    return tasks, cohorts


def _expected_trials(protocol: dict[str, Any]) -> set[tuple[str, int]]:
    supported = set(protocol.get("supported_seeds", []))
    expected: set[tuple[str, int]] = set()
    plan = protocol.get("trial_plan")
    if not isinstance(plan, list) or not plan:
        raise EvidenceError("protocol trial_plan must be a non-empty list")
    for entry in plan:
        task_id = entry.get("task_id") if isinstance(entry, dict) else None
        seeds = entry.get("seeds") if isinstance(entry, dict) else None
        if not isinstance(task_id, str) or not isinstance(seeds, list) or not seeds:
            raise EvidenceError("each trial_plan entry requires task_id and seeds")
        for seed in seeds:
            if seed not in supported:
                raise EvidenceError(f"trial seed {seed!r} is not declared as supported")
            key = (task_id, seed)
            if key in expected:
                raise EvidenceError(f"duplicate planned trial: {task_id}/{seed}")
            expected.add(key)
    return expected


def _trial_kinds(protocol: dict[str, Any]) -> dict[tuple[str, int], str]:
    return {
        (entry["task_id"], seed): entry["kind"]
        for entry in protocol["trial_plan"]
        for seed in entry["seeds"]
    }


def validate_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    tasks, cohorts = _task_index(protocol)
    expected = _expected_trials(protocol)
    missing = sorted(task_id for task_id, _ in expected if task_id not in tasks)
    if missing:
        raise EvidenceError(f"trial plan references unknown tasks: {missing}")
    expected_cohorts = {cohorts[task_id] for task_id, _ in expected}
    if expected_cohorts != {"development", "held_out"}:
        raise EvidenceError("trial plan must include development and held-out tasks")
    canonical = [
        entry for entry in protocol["trial_plan"] if entry.get("kind") == "canonical"
    ]
    canonical_count = sum(len(entry["seeds"]) for entry in canonical)
    if canonical_count != 5:
        raise EvidenceError("the canonical comparison must declare exactly five trials")
    return {
        "tasks": tasks,
        "cohorts": cohorts,
        "expected": expected,
        "kinds": _trial_kinds(protocol),
    }


def _validate_verdicts(bundle_path: Path, trial: dict[str, Any]) -> dict[str, str]:
    verdicts = trial.get("verdicts")
    if not isinstance(verdicts, dict):
        raise EvidenceError(f"{bundle_path.name}: verdicts object is required")
    normalized: dict[str, str] = {}
    for dimension, allowed in VERDICTS.items():
        verdict = verdicts.get(dimension)
        if verdict not in allowed:
            raise EvidenceError(
                f"{bundle_path.name}: invalid {dimension} verdict {verdict!r}"
            )
        normalized[dimension] = verdict
    if normalized["infrastructure"] == "failed" and any(
        normalized[name] == "passed"
        for name in ("objective", "semantic", "authorization")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: infrastructure failure cannot be a product pass"
        )
    if normalized["semantic"] == "judge_unavailable" and trial.get("passed") is True:
        raise EvidenceError(
            f"{bundle_path.name}: unavailable judge cannot produce a product pass"
        )
    return normalized


def _verify_invalid_revision(bundle_path: Path, proof: Any) -> None:
    if not isinstance(proof, dict) or proof.get("attempted") is not True:
        raise EvidenceError(f"{bundle_path.name}: invalid revision proof is required")
    if proof.get("outcome") != "blocked" or not proof.get("review_id"):
        raise EvidenceError(
            f"{bundle_path.name}: invalid revision was not retained as blocked"
        )
    review_path = _verify_declared_artifact(bundle_path, proof.get("review"))
    review = _load_json(review_path)
    if (
        review.get("id") != proof.get("review_id")
        or review.get("outcome") != "blocked"
        or not review.get("candidate_content_hash")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: blocked revision review is not persisted"
        )
    before = proof.get("before")
    after = proof.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise EvidenceError(
            f"{bundle_path.name}: revision before/after state is required"
        )
    identity = ("draft_id", "version", "content_sha256")
    if any(before.get(field) != after.get(field) for field in identity):
        raise EvidenceError(
            f"{bundle_path.name}: invalid revision replaced the current valid artifact"
        )


def _verify_success_artifacts(bundle_path: Path, trial: dict[str, Any]) -> None:
    artifacts = trial.get("artifacts")
    if not isinstance(artifacts, dict):
        raise EvidenceError(f"{bundle_path.name}: successful trial lacks artifacts")
    draft = artifacts.get("draft")
    if not isinstance(draft, dict) or not draft.get("id"):
        raise EvidenceError(f"{bundle_path.name}: exact saved draft id is required")
    if not isinstance(draft.get("version"), int) or draft["version"] < 1:
        raise EvidenceError(f"{bundle_path.name}: saved draft version is invalid")
    draft_path = _verify_declared_artifact(bundle_path, draft)
    draft_digest = _file_digest(draft_path)
    if draft.get("content_sha256") != draft_digest:
        raise EvidenceError(f"{bundle_path.name}: saved draft content hash mismatch")
    draft_content = draft_path.read_text(encoding="utf-8")
    markers = sorted(set(DOC_MARKER_RE.findall(draft_content)), key=int)
    if not markers:
        raise EvidenceError(
            f"{bundle_path.name}: saved draft has no canonical citations"
        )

    review_path = _verify_declared_artifact(bundle_path, artifacts.get("review"))
    review = _load_json(review_path)
    if review.get("outcome") != "passed" or not review.get("id"):
        raise EvidenceError(f"{bundle_path.name}: persisted passing review is required")
    if review.get("candidate_content_hash") != draft_digest.removeprefix("sha256:"):
        raise EvidenceError(
            f"{bundle_path.name}: review is not bound to the saved draft"
        )

    adjudication_path = _verify_declared_artifact(
        bundle_path, artifacts.get("adjudication")
    )
    adjudication = _load_json(adjudication_path)
    if (
        adjudication.get("independent") is not True
        or adjudication.get("verdict") != "passed"
        or not adjudication.get("id")
        or not adjudication.get("reviewer")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: independent passing adjudication is required"
        )
    if adjudication.get("draft_content_sha256") != draft_digest:
        raise EvidenceError(
            f"{bundle_path.name}: adjudication is not bound to the saved draft"
        )
    if adjudication.get("task_digest") != trial.get("task_digest"):
        raise EvidenceError(
            f"{bundle_path.name}: adjudication is not bound to the frozen task"
        )
    runtime = trial.get("runtime") or {}
    if adjudication.get("reviewer") in {runtime.get("provider"), runtime.get("model")}:
        raise EvidenceError(
            f"{bundle_path.name}: adjudicator must be independent of the trial model"
        )

    authorization_path = _verify_declared_artifact(
        bundle_path, artifacts.get("authorization")
    )
    authorization = _load_json(authorization_path)
    if authorization.get("draft_id") != draft.get("id"):
        raise EvidenceError(
            f"{bundle_path.name}: authorization evidence is not bound to the draft"
        )
    checks = authorization.get("checks")
    if not isinstance(checks, dict) or checks.get("collaborator_download") != 200:
        raise EvidenceError(
            f"{bundle_path.name}: collaborator download authorization is unproven"
        )
    if checks.get("foreign_project_download") != 404:
        raise EvidenceError(f"{bundle_path.name}: foreign-project denial is unproven")

    citation_path = _verify_declared_artifact(bundle_path, artifacts.get("citations"))
    citations = _load_json(citation_path)
    if not isinstance(citations, list) or not citations:
        raise EvidenceError(f"{bundle_path.name}: persisted citations are required")
    citation_markers = sorted(
        {str(row.get("citation_index")) for row in citations if isinstance(row, dict)},
        key=int,
    )
    if citation_markers != markers:
        raise EvidenceError(
            f"{bundle_path.name}: saved citation identities do not reconcile"
        )
    for row in citations:
        if not row.get("document_id"):
            raise EvidenceError(
                f"{bundle_path.name}: citation rows require document ids"
            )
        if (
            not row.get("citation_id")
            and row.get("metadata_source") != "document_fallback"
        ):
            raise EvidenceError(
                f"{bundle_path.name}: citation rows must identify canonical metadata source"
            )

    exports = artifacts.get("exports")
    if not isinstance(exports, dict):
        raise EvidenceError(f"{bundle_path.name}: downloaded exports are required")
    markdown_path = _verify_declared_artifact(bundle_path, exports.get("markdown"))
    markdown = markdown_path.read_text(encoding="utf-8")
    if "## References" not in markdown:
        raise EvidenceError(
            f"{bundle_path.name}: Markdown export has no References section"
        )
    references = markdown.split("## References", 1)[1]
    for marker in markers:
        if f"[Doc {marker}]" not in references and f"doc{marker}" not in references:
            raise EvidenceError(
                f"{bundle_path.name}: Markdown References lost canonical doc{marker}"
            )

    latex_path = _verify_declared_artifact(bundle_path, exports.get("latex"))
    try:
        with zipfile.ZipFile(latex_path) as archive:
            tex_name = next(
                name for name in archive.namelist() if name.endswith(".tex")
            )
            tex = archive.read(tex_name).decode("utf-8")
            bib = archive.read("references.bib").decode("utf-8")
    except (
        OSError,
        StopIteration,
        UnicodeDecodeError,
        zipfile.BadZipFile,
        KeyError,
    ) as exc:
        raise EvidenceError(
            f"{bundle_path.name}: LaTeX export cannot be opened: {exc}"
        ) from exc
    for marker in markers:
        if f"\\cite{{doc{marker}}}" not in tex or not re.search(
            rf"@\w+\{{doc{marker}\s*,", bib
        ):
            raise EvidenceError(
                f"{bundle_path.name}: LaTeX/BibTeX lost canonical doc{marker}"
            )

    _verify_invalid_revision(bundle_path, trial.get("invalid_revision"))


def validate_trial(
    bundle_path: Path,
    trial: dict[str, Any],
    protocol_state: dict[str, Any],
    source_sha: str,
) -> tuple[tuple[str, int], dict[str, str]]:
    task_id = trial.get("task_id")
    seed = trial.get("seed")
    if not isinstance(task_id, str) or not isinstance(seed, int):
        raise EvidenceError(
            f"{bundle_path.name}: task_id and integer seed are required"
        )
    key = (task_id, seed)
    if key not in protocol_state["expected"]:
        raise EvidenceError(f"{bundle_path.name}: undeclared trial {task_id}/{seed}")
    task = protocol_state["tasks"][task_id]
    if trial.get("cohort") != protocol_state["cohorts"][task_id]:
        raise EvidenceError(f"{bundle_path.name}: cohort does not match frozen corpus")
    if trial.get("task_digest") != _canonical_digest(task):
        raise EvidenceError(f"{bundle_path.name}: task digest does not match corpus")
    runtime = trial.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("source_sha") != source_sha:
        raise EvidenceError(f"{bundle_path.name}: source SHA does not match this run")
    if not runtime.get("provider") or not runtime.get("model"):
        raise EvidenceError(f"{bundle_path.name}: provider and model are required")
    if not isinstance(runtime.get("configuration"), dict):
        raise EvidenceError(f"{bundle_path.name}: model configuration is required")
    _reject_sensitive_configuration(runtime["configuration"])
    verdicts = _validate_verdicts(bundle_path, trial)
    if all(
        verdicts[name] == "passed"
        for name in ("objective", "semantic", "authorization", "infrastructure")
    ):
        if (
            runtime.get("authenticated") is not True
            or runtime.get("real_provider") is not True
        ):
            raise EvidenceError(
                f"{bundle_path.name}: a pass requires an authenticated "
                "real-provider run"
            )
        _verify_success_artifacts(bundle_path, trial)
    elif not str(trial.get("failure_reason") or "").strip():
        raise EvidenceError(
            f"{bundle_path.name}: non-passing trial needs a failure reason"
        )
    return key, verdicts


def collect(
    protocol_path: Path, trials_dir: Path, output_path: Path, source_sha: str
) -> dict[str, Any]:
    protocol = _load_json(protocol_path)
    if not isinstance(protocol, dict):
        raise EvidenceError("protocol must be a JSON object")
    state = validate_protocol(protocol)
    bundles = sorted(trials_dir.glob("*/trial.json"))
    if not bundles:
        raise EvidenceError(f"no retained trial bundles found under {trials_dir}")

    observed: dict[tuple[str, int], dict[str, str]] = {}
    trials = []
    for bundle_path in bundles:
        trial = _load_json(bundle_path)
        if not isinstance(trial, dict):
            raise EvidenceError(f"{bundle_path}: trial bundle must be an object")
        key, verdicts = validate_trial(bundle_path, trial, state, source_sha)
        if key in observed:
            raise EvidenceError(f"duplicate retained trial: {key[0]}/{key[1]}")
        observed[key] = verdicts
        trials.append(trial)
    missing = sorted(state["expected"] - set(observed))
    if missing:
        raise EvidenceError(f"missing declared trials: {missing}")

    dimensions = {
        name: dict(Counter(verdicts[name] for verdicts in observed.values()))
        for name in VERDICTS
    }
    product_passes = sum(
        all(verdicts[name] == "passed" for name in VERDICTS)
        for verdicts in observed.values()
    )
    unscored = sum(
        verdicts["infrastructure"] == "failed"
        or verdicts["semantic"] == "judge_unavailable"
        for verdicts in observed.values()
    )
    sample_sizes = {
        "canonical": sum(kind == "canonical" for kind in state["kinds"].values()),
        "near_boundary": sum(
            kind == "near_boundary" for kind in state["kinds"].values()
        ),
        "development": sum(
            state["cohorts"][task_id] == "development" for task_id, _ in observed
        ),
        "held_out": sum(
            state["cohorts"][task_id] == "held_out" for task_id, _ in observed
        ),
    }
    report = {
        "schema_version": "1.0",
        "protocol_id": protocol.get("id"),
        "source_sha": source_sha,
        "protocol_digest": _file_digest(protocol_path),
        "harness_digest": _harness_digest(),
        "corpus_digests": {
            name: _file_digest(_corpus(protocol, name)[0])
            for name in ("development", "held_out")
        },
        "sample_size": len(observed),
        "sample_sizes": sample_sizes,
        "product_passes": product_passes,
        "product_failures": len(observed) - product_passes - unscored,
        "unscored": unscored,
        "dimensions": dimensions,
        "trials": trials,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=TASK_DIR / "protocol.json")
    parser.add_argument("--trials-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", default=None)
    args = parser.parse_args()
    try:
        report = collect(
            args.protocol.resolve(),
            args.trials_dir.resolve(),
            args.output.resolve(),
            args.source_sha or _source_sha(),
        )
    except (EvidenceError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "sample_size": report["sample_size"],
                "product_passes": report["product_passes"],
                "product_failures": report["product_failures"],
                "unscored": report["unscored"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
