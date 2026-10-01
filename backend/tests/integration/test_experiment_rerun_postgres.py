"""Real PostgreSQL proof for GOO-313's fresh reruns.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now ends
at ``c0f2a4b6d8e9``. GOO-312's seed and fake run give the original: a
completed, conformant run whose single ``analyze`` step has a complete
manifest and archived code, lock, input and outputs.

The rerun goes through the real routes, the real ``execute_attempt`` worker
and the real ``SandboxManager.run_isolated`` restore mode. Only the e2b
``AsyncSandbox`` is fake: a local temporary directory whose ``pip freeze``
returns what was "installed", whose ``sha256sum`` really hashes the restored
files and whose command runs the archived script with the local interpreter.
Admission enqueues nothing (``_enqueue`` is recorded) and the test calls the
worker itself.

Amendment to the plan's step 1, forced by GOO-312's seed: its metrics output
is ``{"n", "mean_y"}``, so the numeric pointer is ``/mean_y`` (not
``/pooled/estimate``).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-313 section):

- the ``(rerun_id, attempt)`` unique loss treated as a win and the blobs
  kept: step 5, outputs exist for the cancelled attempt;
- ``sweep_expired`` a no-op: step 6, no ``interrupted`` row;
- ``require_run_conformance`` removed from admission: step 9, the tampered
  copy is admitted;
- ``ck_experiment_rerun_attempts_executed`` dropped: step 3, a
  ``restoration_failed`` row with ``reproduced`` is accepted.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_experiment_rerun_postgres.py``.
"""

import asyncio
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine.experiments import router as experiments_router
from src.api.research_engine.reruns import router as reruns_router
from src.api.research_engine.runs import router as runs_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_experiment import ResearchRunArtifact, ResearchRunManifest
from src.models.research_rerun import ExperimentRerun
from src.models.research_run import ResearchRun
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research_decisions import replay_decisions
from src.services.research_engine import experiment_service, manifest_rules
from src.services.research_engine import rerun_rules as rules
from src.services.research_engine import rerun_service as svc
from src.services.research_engine import step_executor
from src.services.sandbox import e2b_sandbox_manager as sandbox_module
from src.services.sandbox.e2b_sandbox_manager import SandboxManager
from tests.integration.test_run_manifest_postgres import (
    CSV,
    LOCK,
    _FakeSandbox,
    _run,
    _setup,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
OUTPUTS = ["fig.svg", "metrics.json", "table.csv"]
RULE = {
    "schema": rules.RULE_SCHEMA,
    "outputs": [
        {"name": "fig.svg", "mode": "bytes"},
        {
            "name": "metrics.json",
            "mode": "json_numeric",
            "pointers": ["/mean_y"],
            "abs": 1e-12,
            "rel": 0.0,
        },
        {"name": "table.csv", "mode": "bytes"},
    ],
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Exit(Exception):
    """Carries ``exit_code`` like e2b's ``CommandExitException``."""

    def __init__(self, code: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__("exit")
        self.exit_code, self.stdout, self.stderr = code, stdout, stderr


class _LocalSandbox:
    """The e2b ``AsyncSandbox`` surface ``run_isolated`` uses, over a temp dir."""

    count = 0
    fail_create = False
    template_override: str | None = None
    lock_override: bytes | None = None
    corrupt_input: bool = False
    on_command: Callable[[], Any] | None = None
    perturb: Callable[[Path], None] | None = None
    log: list[str] = []

    def __init__(self, template: str) -> None:
        type(self).count += 1
        self.sandbox_id = f"local-{type(self).count}"
        self.template = template
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requirements = b""
        self.installed = b""
        self.files = SimpleNamespace(write=self._write, read=self._read)
        self.commands = SimpleNamespace(run=self._run)

    @classmethod
    def reset(cls) -> None:
        cls.fail_create, cls.template_override, cls.lock_override = False, None, None
        cls.corrupt_input, cls.on_command, cls.perturb = False, None, None
        cls.log = []

    @classmethod
    async def create(cls, **kwargs: Any) -> "_LocalSandbox":
        assert kwargs["envs"] == {}
        if cls.fail_create:
            raise RuntimeError("no capacity")
        return cls(kwargs["template"])

    async def get_info(self) -> Any:
        return SimpleNamespace(template_id=self.template_override or self.template)

    async def kill(self) -> None:
        self.tmp.cleanup()

    def _path(self, path: str) -> Path:
        assert path.startswith("/work/"), path
        return self.root / path.removeprefix("/work/")

    async def _write(self, path: str, data: str | bytes) -> None:
        raw = data.encode() if isinstance(data, str) else data
        if path == "/tmp/nous-requirements.txt":
            self.requirements = raw
            return
        if self.corrupt_input and path.startswith("/work/in/"):
            raw = raw + b"\n"
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    async def _read(self, path: str, format: str = "text") -> Any:
        assert format == "bytes"
        if path == "/etc/os-release":
            return b'ID="fixture"\n'
        target = self._path(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()

    async def _run(self, cmd: str, **kwargs: Any) -> Any:
        assert kwargs.get("envs") == {}
        type(self).log.append(cmd)
        if cmd.startswith("pip install"):
            self.installed = self.requirements
            return SimpleNamespace(stdout="", stderr="")
        if cmd == "pip freeze --all":
            lock = self.lock_override or self.installed
            return SimpleNamespace(stdout=lock.decode(), stderr="")
        if cmd == "python -VV":
            return SimpleNamespace(stdout="Python 3.11.9\n", stderr="")
        if cmd.startswith("sha256sum -- "):
            lines = [
                f"{_sha((self.root / name).read_bytes())}  {name}\n"
                for name in shlex.split(cmd)[2:]
                if (self.root / name).is_file()
            ]
            return SimpleNamespace(stdout="".join(lines), stderr="")
        if cmd.startswith("mkdir -p "):
            (self.root / "out").mkdir(exist_ok=True)
            return SimpleNamespace(stdout="", stderr="")
        assert cmd == "python main.py" and kwargs.get("cwd") == "/work", cmd
        if self.on_command is not None:
            await type(self).on_command()  # type: ignore[misc]
        done = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "main.py"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise _Exit(done.returncode, done.stdout, done.stderr)
        if self.perturb is not None:
            type(self).perturb(self.root / "out")  # type: ignore[misc]
        return SimpleNamespace(stdout=done.stdout, stderr=done.stderr)


def _app(w: Any, user: str) -> FastAPI:
    app = FastAPI()
    for router in (runs_router, experiments_router, reruns_router):
        app.include_router(router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    org = w.ids["foreign_org" if user == "F" else "org"]
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=w.ids[user], organization_id=org
    )
    return app


def _client(w: Any, user: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(w, user)), base_url="http://t")


async def _admit(w: Any, run: UUID, key: str, user: str = "R", rule: Any = None) -> Any:
    body: dict[str, Any] = {"idempotency_key": key}
    if rule is not None:
        body["rule"] = rule
    async with _client(w, user) as client:
        return await client.post(f"/research-engine/runs/{run}/reruns", json=body)


async def _get(w: Any, rerun: Any, user: str = "V") -> dict[str, Any]:
    async with _client(w, user) as client:
        response = await client.get(f"/research-engine/reruns/{rerun}")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _post(w: Any, rerun: Any, action: str, user: str = "R") -> Any:
    async with _client(w, user) as client:
        return await client.post(f"/research-engine/reruns/{rerun}/{action}")


async def _eligibility(w: Any, run: UUID, user: str = "V") -> dict[str, Any]:
    async with _client(w, user) as client:
        response = await client.get(f"/research-engine/runs/{run}/rerun-eligibility")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _copy_run(
    w: Any,
    source: UUID,
    *,
    edit: Callable[[dict[str, Any]], None] | None = None,
    plan_hash: str | None = None,
    archive: bool = False,
) -> UUID:
    """A completed copy of ``source`` with its own (possibly edited)
    manifest and, with ``archive``, rows citing the same archived bytes."""
    async with w.factory() as db:
        run = await db.get(ResearchRun, source)
        assert run is not None
        copy = ResearchRun(
            blueprint_id=run.blueprint_id,
            blueprint_version=run.blueprint_version,
            protocol_version_id=run.protocol_version_id,
            effective_plan_hash=plan_hash or run.effective_plan_hash,
            conformance_status=run.conformance_status,
            status=run.status,
            reproducibility_manifest={"parameters_override": {}},
        )
        db.add(copy)
        await db.flush()
        original = (
            await db.execute(
                select(ResearchRunManifest).where(ResearchRunManifest.run_id == source)
            )
        ).scalar_one()
        document = deepcopy(original.manifest)
        document["run_id"] = str(copy.id)
        if edit is not None:
            edit(document)
        if archive:
            for artifact in (
                await db.execute(
                    select(ResearchRunArtifact).where(
                        ResearchRunArtifact.run_id == source
                    )
                )
            ).scalars():
                db.add(
                    ResearchRunArtifact(
                        id=uuid4(),
                        run_id=copy.id,
                        collection_id=artifact.collection_id,
                        organization_id=artifact.organization_id,
                        role=artifact.role,
                        name=artifact.name,
                        media_type=artifact.media_type,
                        sha256=artifact.sha256,
                        byte_size=artifact.byte_size,
                        storage_key=artifact.storage_key,
                    )
                )
        state, missing = manifest_rules.completeness(document)
        db.add(
            ResearchRunManifest(
                id=uuid4(),
                run_id=copy.id,
                collection_id=original.collection_id,
                schema_version=2,
                manifest=document,
                manifest_hash=manifest_rules.manifest_hash(document),
                completeness=state,
                missing=missing,
            )
        )
        await db.commit()
        return cast(UUID, copy.id)


def _rerun_files(storage: LocalArtifactStorage, org: Any, rerun: Any) -> list[Path]:
    base = storage.root / "artifacts" / str(org) / "research-reruns" / str(rerun)
    return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []


async def test_rerun_reproduces_and_every_failure_is_explicit(
    screening_factory: Factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = screening_factory
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    original = _FakeSandbox()
    monkeypatch.setattr(step_executor, "get_sandbox_manager", lambda: original)
    for module in (step_executor, experiment_service, svc):
        monkeypatch.setattr(module, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(
        "src.api.research_engine.runs._build_connectors", lambda **_: object()
    )

    async def admitted(**_: Any) -> bool:
        return True

    monkeypatch.setattr("src.api.research_engine.runs.admit_expensive_work", admitted)
    monkeypatch.setenv("E2B_API_KEY", "e2b_" + "fixture_key")  # not a secret
    monkeypatch.setattr(sandbox_module, "_e2b_available", True)
    monkeypatch.setattr(sandbox_module, "AsyncSandbox", _LocalSandbox)
    monkeypatch.setattr(svc, "get_sandbox_manager", SandboxManager)
    enqueued: list[tuple[Any, int]] = []
    monkeypatch.setattr(
        svc, "_enqueue", lambda _db, rerun_id, n: enqueued.append((rerun_id, n))
    )
    _LocalSandbox.reset()

    async def execute(rerun: Any, attempt: int = 1) -> dict[str, Any]:
        await svc.execute_attempt(factory, UUID(str(rerun)), attempt)
        return await _get(w, rerun)

    w = await _setup(factory, tmp_path)
    org = w.ids["org"]
    run1, stream = await _run(w)
    assert "event: run_complete" in stream, stream
    async with _client(w, "V") as client:
        manifest_body = (
            await client.get(f"/research-engine/runs/{run1}/manifest/v2")
        ).json()
    manifest = manifest_body["manifest"]
    assert manifest_body["completeness"] == "complete"

    # 1. Reproduced.
    assert (await _eligibility(w, run1)) == {
        "eligible": True,
        "reasons": [],
        "default_rule": rules.default_rule(manifest),
    }
    response = await _admit(w, run1, "k1", rule=RULE)
    assert response.status_code == 202, response.text
    rerun1 = response.json()
    assert enqueued == [(UUID(rerun1["id"]), 1)]
    assert [(a["attempt"], a["status"]) for a in rerun1["attempts"]] == [(1, "queued")]
    expected_rule = rules.validate_rule(RULE, manifest)
    assert rerun1["rule"] == expected_rule
    assert rerun1["rule_hash"] == rules.rule_hash(expected_rule)
    assert rerun1["manifest_hash"] == manifest_body["manifest_hash"]
    replay = await _admit(w, run1, "k1", rule=RULE)
    assert replay.status_code == 200 and replay.json()["id"] == rerun1["id"]
    body = await execute(rerun1["id"])
    (attempt,) = body["attempts"]
    assert (attempt["status"], attempt["reproduction"]) == ("executed", "reproduced")
    assert attempt["sandbox_id"].startswith("local-")  # not the original's sandbox
    assert attempt["template_id"] == manifest["environment"]["template_id"]
    assert [row["name"] for row in attempt["comparison"]] == OUTPUTS
    for row, declared in zip(attempt["comparison"], manifest["outputs"]):
        assert row["expected_sha256"] == declared["sha256"] == row["actual_sha256"]
        assert row["equal"] is True
    (numeric,) = attempt["comparison"][1]["numeric"]
    assert numeric["pointer"] == "/mean_y" and numeric["abs_diff"] == 0.0
    assert attempt["environment_validation"]["template_verified"] is True
    assert attempt["environment_validation"]["lock_verified"] is True
    files = attempt["input_validation"]["files"]
    assert set(files) == {"main.py", "in/data.csv"}
    assert all(f["verified_in_sandbox"] is True for f in files.values())
    assert files["in/data.csv"]["expected_sha256"] == _sha(CSV)
    assert _LocalSandbox.log.count("python main.py") == 1
    async with _client(w, "V") as client:
        for output in attempt["outputs"]:
            got = await client.get(
                f"/research-engine/reruns/{rerun1['id']}/attempts/1/outputs/"
                f"{output['name']}"
            )
            assert got.status_code == 200 and _sha(got.content) == output["sha256"]
            assert got.headers["X-Content-SHA256"] == output["sha256"]
        exported = await client.get(
            f"/research-engine/reruns/{rerun1['id']}/comparison"
        )
    comparison = exported.json()
    assert comparison["schema"] == rules.COMPARISON_SCHEMA
    assert comparison["rule_hash"] == rerun1["rule_hash"]
    assert "storage_key" not in exported.text and "artifacts/" not in exported.text
    assert (
        await _scalar(
            factory,
            "SELECT rule_hash FROM experiment_reruns WHERE id = :r",
            r=UUID(rerun1["id"]),
        )
        == rerun1["rule_hash"]
    )

    # 2. Mismatch: the metric moves by 1e-6, beyond the declared 1e-12.
    def nudge(out: Path) -> None:
        data = json.loads((out / "metrics.json").read_text())
        data["mean_y"] += 1e-6
        (out / "metrics.json").write_text(json.dumps(data))

    _LocalSandbox.perturb = nudge
    rerun2 = (await _admit(w, run1, "k2", rule=RULE)).json()
    attempt = (await execute(rerun2["id"]))["attempts"][0]
    _LocalSandbox.perturb = None
    assert (attempt["status"], attempt["reproduction"]) == (
        "executed",
        "not_reproduced",
    )
    (numeric,) = attempt["comparison"][1]["numeric"]
    assert numeric["within"] is False
    assert numeric["abs_diff"] == pytest.approx(1e-6)
    assert [r["equal"] for r in attempt["comparison"]] == [True, False, True]

    # 3. Failed restoration: a corrupt archive, a drifted lock, a changed input.
    rerun3 = (await _admit(w, run1, "k3")).json()  # admitted before corruption
    key = await _scalar(
        factory,
        """SELECT storage_key FROM research_run_artifacts
           WHERE run_id = :r AND role = 'input'""",
        r=run1,
    )
    await storage.put(key, CSV + b"tampered", "text/csv")
    refused = await _eligibility(w, run1)
    assert refused["eligible"] is False
    assert refused["reasons"] == ["artifact_corrupt:data.csv"]
    blocked = await _admit(w, run1, "k4")
    assert blocked.status_code == 409
    assert blocked.json() == {
        "detail": svc.NOT_ELIGIBLE,
        "reasons": ["artifact_corrupt:data.csv"],
    }
    attempt = (await execute(rerun3["id"]))["attempts"][0]
    assert (attempt["status"], attempt["reproduction"]) == (
        "restoration_failed",
        None,
    )
    assert attempt["reasons"] == ["artifact_corrupt:data.csv"]
    assert attempt["outputs"] == [] and attempt["comparison"] is None
    await storage.put(key, CSV, "text/csv")
    _LocalSandbox.lock_override = LOCK + b"scipy==1.14.0\n"
    rerun_lock = (await _admit(w, run1, "k5")).json()
    attempt = (await execute(rerun_lock["id"]))["attempts"][0]
    _LocalSandbox.lock_override = None
    assert attempt["status"] == "restoration_failed"
    assert attempt["reasons"] == ["environment_lock_mismatch"]
    assert attempt["environment_validation"]["lock_verified"] is False
    _LocalSandbox.corrupt_input = True
    rerun_input = (await _admit(w, run1, "k6")).json()
    commands_before = _LocalSandbox.log.count("python main.py")
    attempt = (await execute(rerun_input["id"]))["attempts"][0]
    _LocalSandbox.corrupt_input = False
    assert attempt["reasons"] == ["input_mismatch:data.csv"]
    assert attempt["input_validation"]["files"]["in/data.csv"] == {
        "expected_sha256": _sha(CSV),
        "verified_in_sandbox": False,
    }
    assert _LocalSandbox.log.count("python main.py") == commands_before
    # The CHECK keeps "executed" and "reproduced" apart in the database too.
    async with factory() as db:
        with pytest.raises(IntegrityError) as check:
            await db.execute(
                text("""INSERT INTO experiment_rerun_attempts
                        (id, rerun_id, attempt, status, reproduction,
                         environment_validation, input_validation, reasons,
                         outputs)
                        VALUES (:id, :r, 2, 'restoration_failed', 'reproduced',
                                '{}', '{}', '[]', '[]')"""),
                {"id": uuid4(), "r": UUID(rerun3["id"])},
            )
        assert getattr(check.value.orig, "sqlstate", None) == "23514"

    # 4. Environment unavailable.
    _LocalSandbox.fail_create = True
    rerun4 = (await _admit(w, run1, "k7")).json()
    attempt = (await execute(rerun4["id"]))["attempts"][0]
    _LocalSandbox.fail_create = False
    assert (attempt["status"], attempt["reasons"]) == (
        "environment_unavailable",
        ["sandbox_unavailable"],
    )

    # 5. Cancel race: the reviewer cancels while the command runs; the
    # worker's later insert loses and its blobs are deleted.
    rerun5 = (await _admit(w, run1, "k8")).json()

    async def cancel_mid_run() -> None:
        cancelled = await _post(w, rerun5["id"], "cancel")
        assert cancelled.status_code == 200, cancelled.text

    _LocalSandbox.on_command = cancel_mid_run
    body = await execute(rerun5["id"])
    _LocalSandbox.on_command = None
    (attempt,) = body["attempts"]
    assert (attempt["status"], attempt["reproduction"]) == ("cancelled", None)
    assert attempt["outputs"] == [] and attempt["comparison"] is None
    assert attempt["started_at"] is not None  # the worker's claim
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM experiment_rerun_attempts WHERE rerun_id = :r",
            r=UUID(rerun5["id"]),
        )
        == 1
    )
    assert _rerun_files(storage, org, rerun5["id"]) == []
    again = await _post(w, rerun5["id"], "cancel")  # idempotent
    assert again.status_code == 200 and len(again.json()["attempts"]) == 1

    # 6. Interrupt and retry: a claim whose lease expired is swept.
    rerun6 = (await _admit(w, run1, "k9", rule=RULE)).json()
    monkeypatch.setattr(svc, "LEASE_SECONDS", -60)
    async with factory() as db:
        row = await db.get(ExperimentRerun, UUID(rerun6["id"]))
        await svc._lock(db, row.collection_id)
        await svc._claim(db, row, 1, actor_id=row.requested_by_id, actor_role="machine")
        await db.commit()
    monkeypatch.setattr(svc, "LEASE_SECONDS", 960)
    assert (await _get(w, rerun6["id"]))["attempts"][0]["status"] == "interrupted"
    async with factory() as db:
        assert await svc.sweep_expired(db) == 1
    async with factory() as db:
        assert await svc.sweep_expired(db) == 0
    (attempt,) = (await _get(w, rerun6["id"]))["attempts"]
    assert attempt["status"] == "interrupted" and attempt["finished_at"]
    assert attempt["reasons"] == ["lease_expired"]
    retried = await _post(w, rerun6["id"], "retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["attempts"][1]["status"] == "running"
    assert enqueued[-1] == (UUID(rerun6["id"]), 2)
    body = await execute(rerun6["id"], 2)
    assert [a["status"] for a in body["attempts"]] == ["interrupted", "executed"]
    assert body["attempts"][1]["reproduction"] == "reproduced"
    assert body["rule_hash"] == rerun6["rule_hash"] == rerun1["rule_hash"]
    assert (await _post(w, rerun6["id"], "retry")).status_code == 409
    assert (await _post(w, rerun5["id"], "retry")).status_code == 200  # cancelled

    # 7. Ineligible: a legacy run and an incomplete manifest.
    legacy = await _eligibility(w, w.ids["run"])
    assert legacy["eligible"] is False and legacy["default_rule"] is None
    assert legacy["reasons"][:2] == [
        "no_manifest",
        "manifest_incomplete:schema_version<2",
    ]
    assert "run_not_completed" in legacy["reasons"]
    legacy_admit = await _admit(w, w.ids["run"], "legacy")
    assert legacy_admit.status_code == 409

    def strip(document: dict[str, Any]) -> None:
        document["seed"] = None
        document["environment"] = None

    incomplete = await _copy_run(w, run1, edit=strip)
    blocked = await _admit(w, incomplete, "incomplete")
    assert blocked.status_code == 409
    reasons = blocked.json()["reasons"]
    assert [r for r in reasons if r.startswith("manifest_incomplete:")] == [
        "manifest_incomplete:environment.lock_sha256",
        "manifest_incomplete:environment.template_id",
        "manifest_incomplete:seed",
    ]

    # 8. Authorization and lifecycle.
    owner = await _admit(w, run1, "owner", user="O")
    assert owner.status_code == 403
    assert (await _admit(w, run1, "viewer", user="V")).status_code == 403
    assert (await _admit(w, run1, "foreign", user="F")).status_code == 404
    async with _client(w, "F") as client:
        assert (
            await client.get(f"/research-engine/reruns/{rerun1['id']}")
        ).status_code == 404
        assert (
            await client.get(f"/research-engine/runs/{run1}/rerun-eligibility")
        ).status_code == 404
    assert (await _post(w, rerun1["id"], "cancel", user="O")).status_code == 403
    rerun8 = (await _admit(w, run1, "k10")).json()
    status_before = await _scalar(
        factory, "SELECT research_status FROM collections WHERE id = :p", p=w.p1
    )
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    attempt = (await execute(rerun8["id"]))["attempts"][0]
    assert (attempt["status"], attempt["reasons"]) == (
        "cancelled",
        ["project_lifecycle"],
    )
    assert (await _admit(w, run1, "archived")).status_code == 409
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = :s WHERE id = :p"),
            {"s": status_before, "p": w.p1},
        )
        await db.commit()

    # 9. Conformance: a superseded protocol still authorizes; a drifted plan
    # hash refuses.
    async with factory() as db:
        await db.execute(
            text("""UPDATE research_protocol_versions
                    SET status = 'superseded', superseded_at = now()
                    WHERE id = :v"""),
            {"v": w.version},
        )
        await db.execute(
            text("""UPDATE research_protocols SET current_approved_version_id = NULL
                    WHERE current_approved_version_id = :v"""),
            {"v": w.version},
        )
        await db.commit()
    retained = await _admit(w, run1, "k11")
    assert retained.status_code == 202, retained.text
    tampered = await _copy_run(w, run1, plan_hash="0" * 64, archive=True)
    assert (await _eligibility(w, tampered))["eligible"] is True
    refused_plan = await _admit(w, tampered, "tampered")
    assert refused_plan.status_code == 409
    assert (
        refused_plan.json()["detail"]
        == "Run execution plan does not match its approved protocol"
    )

    # 10. Insert-only, and the stream replays.
    for table in ("experiment_reruns", "experiment_rerun_attempts"):
        for statement in (
            (
                f"UPDATE {table} SET created_at = now()"
                if table == "experiment_reruns"
                else f"UPDATE {table} SET finished_at = now()"
            ),
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as blocked_write:
                    await db.execute(text(statement))
                assert getattr(blocked_write.value.orig, "sqlstate", None) == "55000"
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_reproduction",
            aggregate_id=w.p1,
        )
    kinds = [e.event_type for e in events]
    assert kinds.count("rerun.admitted") == 10
    assert kinds.count("rerun.attempt_finished") == await _scalar(
        factory, "SELECT count(*) FROM experiment_rerun_attempts"
    )
    async with factory() as db:
        assert await svc.latest_reproduction(db, run1) == "reproduced"
        assert await svc.latest_reproduction(db, incomplete) == "not_attempted"
