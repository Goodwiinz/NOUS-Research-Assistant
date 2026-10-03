"""Transport contract for the GOO-320 review version routes.

``resolve_project`` is a fake that records the action and refuses SUPERVISE
for a caller without the supervisor role (the real rule is
``project_access._DECISION_ROLE``; the PostgreSQL proof exercises it).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import review_versions as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine import corpus_export
from src.services.research_engine import review_update_service as svc
from src.services.research_engine.corpus_export import _canonical
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
BASE = f"/research-engine/projects/{PROJECT}/review-versions"
ROOT = {"rationale": "baseline review", "idempotency_key": "k1"}


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    actions: list[ResearchAction]
    roles: frozenset[str]


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
    h = _Harness(db=db, actions=[], roles=frozenset({"supervisor"}))

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
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def test_reviewer_cannot_create_version_403(harness: _Harness) -> None:
    harness.roles = frozenset({"reviewer"})
    version = uuid4()
    for path, body in (
        (BASE, ROOT),
        (f"{BASE}/{version}/work", None),
        (
            f"{BASE}/{version}/release",
            {"release_id": str(uuid4()), "idempotency_key": "r"},
        ),
    ):
        response = harness.client.post(path, json=body)
        assert response.status_code == 403, path
    assert harness.actions == [ResearchAction.SUPERVISE] * 3
    harness.db.commit.assert_not_awaited()
    harness.db.add.assert_not_called()


def test_owner_without_supervisor_403(harness: _Harness) -> None:
    # The workspace owner holds no supervisor role: refused like anyone else.
    harness.roles = frozenset()
    assert harness.client.post(BASE, json=ROOT).status_code == 403
    harness.db.commit.assert_not_awaited()


def test_successor_shape_is_422(harness: _Harness) -> None:
    half = {**ROOT, "parent_review_version_id": str(uuid4())}
    assert harness.client.post(BASE, json=half).status_code == 422
    uncertain_root = {**ROOT, "carry_with_uncertainty": [str(uuid4())]}
    assert harness.client.post(BASE, json=uncertain_root).status_code == 422
    bad_hash = {
        **ROOT,
        "parent_review_version_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "delta_hash": "not-a-hash",
    }
    assert harness.client.post(BASE, json=bad_hash).status_code == 422
    harness.db.commit.assert_not_awaited()


def test_export_read_only_no_commit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = uuid4()
    body = {"project_id": str(PROJECT), "version": {"id": str(version)}}

    async def fake_export(_db: Any, context: Any, version_id: UUID) -> Any:
        assert version_id == version
        return {
            **corpus_export.seal(body, "2026-10-01T00:00:00+00:00"),
            "schema": svc.EXPORT_SCHEMA,
        }

    monkeypatch.setattr(svc, "export_version", fake_export)
    response = harness.client.get(f"{BASE}/{version}/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    package = response.json()
    assert package["schema"] == svc.EXPORT_SCHEMA
    assert package["body_sha256"] == hashlib.sha256(_canonical(body)).hexdigest()
    accounting = AsyncMock(
        side_effect=HTTPException(status_code=409, detail=svc.NOT_RECONCILED)
    )
    monkeypatch.setattr(svc, "accounting", accounting)
    response = harness.client.get(f"{BASE}/{version}/accounting")
    assert response.status_code == 409
    assert response.json()["detail"] == svc.NOT_RECONCILED
    assert harness.actions == [ResearchAction.VIEW, ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()
    harness.db.add.assert_not_called()
