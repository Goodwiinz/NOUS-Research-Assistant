"""Transport contract for the GOO-313 rerun routes.

``require_run`` and the project resolution are fakes (a foreign user gets
404, a user without the reviewer role 403 on REVIEW); the service runs for
real over a mocked session and in-memory storage. The PostgreSQL proof
(``tests/integration/test_experiment_rerun_postgres.py``) covers the real
access and lifecycle policy.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import reruns as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.artifacts.storage import MemoryArtifactStorage
from src.services.research_engine import manifest_rules
from src.services.research_engine import rerun_rules as rules
from src.services.research_engine import rerun_service as svc
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

RUN, COLLECTION, USER = uuid4(), uuid4(), uuid4()
SVG = b"<svg/>"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MANIFEST = {
    "schema": manifest_rules.SCHEMA,
    "code": {"sha256": _sha(b"code")},
    "environment": {"template_id": "tid", "lock_sha256": _sha(b"lock")},
    "seed": 1,
    "protocol_version_id": "p",
    "question_version_id": "q",
    "started_at": "t0",
    "completed_at": "t1",
    "status": "completed",
    "inputs": [],
    "outputs": [{"name": "fig.svg", "sha256": _sha(SVG), "artifact_id": "a"}],
}


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    roles: set[ResearchAction]
    storage: MemoryArtifactStorage
    appended: list[dict[str, Any]]
    enqueued: list[tuple[Any, int]]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.add, db.commit, db.flush = MagicMock(), AsyncMock(), AsyncMock()
    h = _Harness(
        db=db,
        roles={ResearchAction.VIEW},
        storage=MemoryArtifactStorage(),
        appended=[],
        enqueued=[],
    )
    run = SimpleNamespace(
        id=RUN, status="completed", conformance_status="conformant", is_deleted=False
    )
    blueprint = SimpleNamespace(steps=[{"type": "analyze"}])

    async def fake_require_run(_db: object, run_id: UUID, *_: object) -> Any:
        if run_id != RUN:
            raise HTTPException(404, "Run not found")
        return run

    async def fake_run_context(
        _db: object, _run: object, _user: object, action: ResearchAction
    ) -> Any:
        if action not in h.roles:
            raise HTTPException(403, "reviewer role required")
        return SimpleNamespace(collection=SimpleNamespace(id=COLLECTION)), blueprint

    code_key = f"artifacts/org/research-runs/{RUN}/{_sha(SVG)}"
    asyncio.run(h.storage.put(code_key, SVG, "image/svg+xml"))
    artifact = SimpleNamespace(
        role="output", name="fig.svg", sha256=_sha(SVG), storage_key=code_key
    )

    async def fake_manifest(_db: object, _run_id: object) -> Any:
        return SimpleNamespace(id=uuid4(), manifest=MANIFEST, manifest_hash="m" * 64)

    async def fake_artifacts(_db: object, _run_id: object) -> list[Any]:
        return [artifact]

    async def fake_append(_db: object, _rerun: object, **kwargs: Any) -> None:
        h.appended.append(kwargs)

    monkeypatch.setattr(routes, "require_run", fake_require_run)
    monkeypatch.setattr(svc, "run_context", fake_run_context)
    monkeypatch.setattr(svc, "_manifest", fake_manifest)
    monkeypatch.setattr(svc, "_artifacts", fake_artifacts)
    monkeypatch.setattr(svc, "_append", fake_append)
    monkeypatch.setattr(svc, "_lock", AsyncMock())
    monkeypatch.setattr(
        svc, "_enqueue", lambda _db, rerun_id, n: h.enqueued.append((rerun_id, n))
    )
    monkeypatch.setattr(svc, "get_artifact_storage", lambda: h.storage)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    h.client = TestClient(app)
    yield h


def test_admit_owner_without_reviewer_role_403(harness: _Harness) -> None:
    response = harness.client.post(
        f"/research-engine/runs/{RUN}/reruns", json={"idempotency_key": "k"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "reviewer role required"
    harness.db.commit.assert_not_awaited()
    harness.db.add.assert_not_called()
    assert harness.appended == [] and harness.enqueued == []
    foreign = harness.client.post(
        f"/research-engine/runs/{uuid4()}/reruns", json={"idempotency_key": "k"}
    )
    assert foreign.status_code == 404


def test_eligibility_zero_writes(harness: _Harness) -> None:
    before = dict(harness.storage.objects)
    response = harness.client.get(f"/research-engine/runs/{RUN}/rerun-eligibility")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "eligible": True,
        "reasons": [],
        "default_rule": rules.default_rule(MANIFEST),
    }
    harness.db.add.assert_not_called()
    harness.db.flush.assert_not_awaited()
    harness.db.commit.assert_not_awaited()
    assert harness.storage.objects == before and harness.appended == []
    # A corrupt archived output is a structured reason, still with no writes.
    key = next(iter(harness.storage.objects))
    harness.storage.objects[key] = b"<svg>tampered</svg>"
    body = harness.client.get(f"/research-engine/runs/{RUN}/rerun-eligibility").json()
    assert body["eligible"] is False and body["reasons"] == ["artifact_corrupt:fig.svg"]
    harness.db.commit.assert_not_awaited()


def test_rule_cannot_change_on_retry(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = rules.default_rule(MANIFEST)
    rerun = SimpleNamespace(
        id=uuid4(),
        collection_id=COLLECTION,
        run_id=RUN,
        manifest_id=uuid4(),
        manifest_hash="m" * 64,
        rule=rule,
        rule_hash=rules.rule_hash(rule),
        requested_by_id=USER,
        created_at=datetime.now(timezone.utc),
    )
    failed = SimpleNamespace(
        attempt=1,
        status="execution_failed",
        reproduction=None,
        reasons=["exit_code:1"],
        environment_validation={},
        input_validation={},
        comparison=None,
        comparison_hash=None,
        outputs=[],
        template_id="tid",
        sandbox_id="sbx",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
    )

    async def fake_rerun_for(
        _db: object, rerun_id: UUID, _user: object, action: ResearchAction
    ) -> Any:
        if action not in harness.roles:
            raise HTTPException(403, "reviewer role required")
        return rerun, None, None

    async def rows(_db: object, _rerun_id: object) -> list[Any]:
        return [failed]

    async def started(_db: object, _rerun: object) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(svc, "_rerun_for", fake_rerun_for)
    monkeypatch.setattr(svc, "_attempt_rows", rows)
    monkeypatch.setattr(svc, "_started", started)
    harness.roles.add(ResearchAction.REVIEW)
    loose = {
        "rule": {
            "schema": rules.RULE_SCHEMA,
            "outputs": [
                {
                    "name": "fig.svg",
                    "mode": "json_numeric",
                    "pointers": ["/x"],
                    "abs": 1.0,
                    "rel": 1.0,
                }
            ],
        }
    }
    response = harness.client.post(
        f"/research-engine/reruns/{rerun.id}/retry", json=loose
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rule"] == rule and body["rule_hash"] == rules.rule_hash(rule)
    assert harness.enqueued == [(rerun.id, 2)]
    (claim,) = harness.appended
    assert claim["event_type"] == "rerun.attempt_started"
    assert claim["payload"]["attempt"] == 2 and "rule" not in claim["payload"]
    assert rerun.rule == rule  # the stored rule object is never touched
