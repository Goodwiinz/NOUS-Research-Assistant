"""Transport contract for the GOO-308 journey and audit-bundle routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import fields
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research_engine import journey as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research_engine import journey
from src.services.research_engine.project_access import ResearchAction

BASE = "/api/v1/research-engine/projects"


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    actions: list[ResearchAction]
    denied: dict[UUID, int]
    calls: list[str]


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4())
    db = AsyncMock()
    actions: list[ResearchAction] = []
    denied: dict[UUID, int] = {}
    calls: list[str] = []

    async def fake_resolve(
        _db: object, project_id: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        assert user_id == user.id
        assert calls == ["snapshot"], "the snapshot must open before the access check"
        actions.append(action)
        if project_id in denied:
            raise HTTPException(status_code=denied[project_id], detail="denied")
        return SimpleNamespace(collection=SimpleNamespace(id=project_id))

    async def fake_snapshot(_db: object) -> None:
        calls.append("snapshot")

    async def fake_facts(_db: object, _context: object) -> journey.StageFacts:
        calls.append("facts")
        return journey.StageFacts()

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    monkeypatch.setattr(journey, "begin_read_snapshot", fake_snapshot)
    monkeypatch.setattr(journey, "facts", fake_facts)
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield _Harness(
                client=client,
                user=user,
                db=db,
                actions=actions,
                denied=denied,
                calls=calls,
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def test_journey_foreign_project_404(harness: _Harness) -> None:
    project_id = uuid4()
    harness.denied[project_id] = 404
    response = harness.client.get(f"{BASE}/{project_id}/journey")
    assert response.status_code == 404
    assert harness.calls == ["snapshot"]  # no facts read for a denied caller
    harness.db.commit.assert_not_awaited()


def test_journey_archived_200(harness: _Harness) -> None:
    # VIEW never refuses an archived project; only mutating actions do.
    response = harness.client.get(f"{BASE}/{uuid4()}/journey")
    assert response.status_code == 200, response.text
    assert harness.actions == [ResearchAction.VIEW]
    body = response.json()
    assert [s["key"] for s in body["stages"]] == list(journey.STAGES)
    assert body["current"] == "plan"
    harness.db.commit.assert_not_awaited()


def test_journey_reviewer_sees_no_observation_counts(harness: _Harness) -> None:
    body: dict[str, Any] = harness.client.get(f"{BASE}/{uuid4()}/journey").json()
    expected = {
        key: {f.name for f in fields(type(getattr(journey.StageFacts(), key)))}
        for key in journey.STAGES
    }
    assert {s["key"]: set(s["facts"]) for s in body["stages"]} == expected
    assert expected["select"] == {
        "queued_reports",
        "resolved_reports",
        "open_conflicts",
        "pending_fulltext",
        "not_retrieved",
        "excluded",
        "prisma_error",
    }
    assert not any("observation" in k for keys in expected.values() for k in keys)
