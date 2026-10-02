"""Transport contract for the GOO-319 search schedule routes.

``resolve_project`` is a fake that records the action and refuses SUPERVISE
for a caller without the supervisor role (the real rule is
``project_access._DECISION_ROLE``; the PostgreSQL proof exercises it).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import search_updates as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.workspace import WorkspaceRole
from src.schemas.research_engine import SearchDeltaResponse
from src.services.research_engine import search_update_service as svc
from src.services.research_engine.corpus_export import _canonical
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
BASE = f"/research-engine/projects/{PROJECT}/search-schedules"
CREATE = {
    "source_run_id": str(uuid4()),
    "step_id": "search",
    "strategy_version": "sha256:" + "a" * 64,
    "cron": "0 6 * * 1",
    "timezone": "Europe/London",
    "idempotency_key": "k1",
}


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    actions: list[ResearchAction]
    roles: frozenset[str]
    workspace_role: WorkspaceRole


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.add = MagicMock()
    db.commit, db.flush, db.execute, db.rollback, db.get = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    h = _Harness(
        db=db,
        actions=[],
        roles=frozenset({"supervisor"}),
        workspace_role=WorkspaceRole.OWNER,
    )

    async def fake_resolve(
        _db: object,
        project_id: UUID,
        _user: object,
        action: ResearchAction = ResearchAction.VIEW,
        **_: object,
    ) -> Any:
        h.actions.append(action)
        if action == ResearchAction.SUPERVISE and "supervisor" not in h.roles:
            raise HTTPException(status_code=403, detail="supervisor role required")
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id),
            organization_id=uuid4(),
            effective_roles=h.roles,
            workspace_role=h.workspace_role,
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def test_reviewer_cannot_create_schedule_403(harness: _Harness) -> None:
    harness.roles = frozenset({"reviewer"})
    response = harness.client.post(BASE, json=CREATE)
    assert response.status_code == 403
    assert harness.actions == [ResearchAction.SUPERVISE]
    harness.db.commit.assert_not_awaited()
    version = {
        "expected_tip_id": str(uuid4()),
        "enabled": False,
        "idempotency_key": "v",
    }
    response = harness.client.post(f"{BASE}/{uuid4()}/versions", json=version)
    assert response.status_code == 403


def test_owner_without_supervisor_403(harness: _Harness) -> None:
    # The workspace owner holds no supervisor role: refused like anyone else.
    harness.roles = frozenset()
    assert harness.client.post(BASE, json=CREATE).status_code == 403
    # A supervisor without edit access could never run the schedule.
    harness.roles = frozenset({"supervisor"})
    harness.workspace_role = WorkspaceRole.VIEWER
    response = harness.client.post(BASE, json=CREATE)
    assert response.status_code == 403
    assert response.json()["detail"] == svc.EDITOR_REQUIRED
    harness.db.commit.assert_not_awaited()


def test_invalid_payloads_are_422(harness: _Harness) -> None:
    bad_hash = {**CREATE, "strategy_version": "md5:abc"}
    assert harness.client.post(BASE, json=bad_hash).status_code == 422
    half = {
        "expected_tip_id": str(uuid4()),
        "strategy_version": CREATE["strategy_version"],
        "idempotency_key": "v",
    }
    assert (
        harness.client.post(f"{BASE}/{uuid4()}/versions", json=half).status_code == 422
    )


def test_delta_export_sealed_and_read_only(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, version_id = uuid4(), uuid4()
    items = [
        {
            "report_id": "r-a",
            "class": "corrected_retracted",
            "evidence": {"notices": [{"notice_doi": "10.1/n", "type": "retraction"}]},
            "publication": {"check": "performed", "source": "crossref"},
        },
        {
            "report_id": "r-d",
            "class": "unknown",
            "reason": "not_returned",
            "evidence": {},
            "publication": {"check": "performed", "source": "crossref"},
        },
    ]
    delta = SearchDeltaResponse.model_validate(
        {
            "execution_id": execution,
            "collection_id": PROJECT,
            "schedule_id": uuid4(),
            "schedule_version_id": version_id,
            "scheduled_local": "2026-01-05T06:00",
            "baseline_digest": "b" * 64,
            "corpus_snapshot_digest": "c" * 64,
            "import_receipt_id": uuid4(),
            "delta_hash": "d" * 64,
            "counts": {
                "new": 0,
                "changed": 0,
                "corrected_retracted": 1,
                "unchanged": 0,
                "unknown": 1,
            },
            "items": items,
            "coverage": {},
            "citation_chasing": {"required": False},
        }
    )
    monkeypatch.setattr(svc, "accepted_delta", AsyncMock(return_value=delta))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    harness.db.get.return_value = SimpleNamespace(
        id=version_id,
        schedule_id=delta.schedule_id,
        owner_id=USER,
        protocol_version_id=uuid4(),
        source_run_id=uuid4(),
        step_id="search",
        strategy_version=CREATE["strategy_version"],
        query="q",
        cron="0 6 * * 1",
        timezone="Europe/London",
        enabled=True,
        supersedes_schedule_version_id=None,
        baseline_digest="b" * 64,
        created_at=now,
    )
    response = harness.client.get(f"{BASE}/executions/{execution}/delta")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    package = response.json()
    assert package["schema"] == svc.DELTA_SCHEMA
    body = package["body"]
    assert package["body_sha256"] == hashlib.sha256(_canonical(body)).hexdigest()
    assert body["counts"]["corrected_retracted"] == 1
    assert {i["class"] for i in body["items"]} == {"corrected_retracted", "unknown"}
    assert "deleted" not in json.dumps(body["items"])
    filtered = harness.client.get(
        f"{BASE}/executions/{execution}/delta", params={"class": "unknown"}
    ).json()["body"]
    assert [i["report_id"] for i in filtered["items"]] == ["r-d"]
    assert filtered["counts"] == body["counts"]  # counts stay whole
    assert harness.actions == [ResearchAction.VIEW, ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()
    harness.db.add.assert_not_called()
