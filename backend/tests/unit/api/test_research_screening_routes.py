"""Transport contract for GOO-301 screening queue routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole
from src.services.research_engine import screening_service
from src.services.research_engine.project_access import ResearchAction

BASE = "/api/v1/research-engine/projects"
NOW = datetime(2026, 9, 29, tzinfo=timezone.utc).isoformat()


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    actions: list[ResearchAction]
    denied: dict[ResearchAction, tuple[int, str]]
    roles: set[ResearchProjectRole]


def _routes() -> Any:
    return import_module("src.api.research_engine.screening")


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    db = AsyncMock()
    actions: list[ResearchAction] = []
    denied: dict[ResearchAction, tuple[int, str]] = {}
    roles: set[ResearchProjectRole] = set()

    async def fake_resolve(
        _db: object, project_id: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        assert user_id == user.id
        actions.append(action)
        if action in denied:
            status, detail = denied[action]
            raise HTTPException(status_code=status, detail=detail)
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id),
            effective_roles=frozenset(roles),
        )

    monkeypatch.setattr(_routes(), "resolve_project", fake_resolve)
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
                roles=roles,
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _queue() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "stage": "title_abstract",
        "protocol_version_id": str(uuid4()),
        "criteria_hash": "c" * 64,
        "reviewer_mode": "single",
        "created_by_id": str(uuid4()),
        "created_at": NOW,
        "report_count": 3,
        "assignment_count": 0,
        "observation_count": 0,
        "suggestion_count": 0,
    }


def _assignment() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "queue_id": str(uuid4()),
        "reviewer_id": str(uuid4()),
        "assigned_by_id": str(uuid4()),
        "created_at": NOW,
    }


def _observation() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "queue_id": str(uuid4()),
        "report_id": str(uuid4()),
        "reviewer_id": str(uuid4()),
        "assignment_id": str(uuid4()),
        "decision": "include",
        "created_at": NOW,
    }


def _observation_body() -> dict[str, Any]:
    return {
        "report_id": str(uuid4()),
        "assignment_id": str(uuid4()),
        "criteria_hash": "c" * 64,
        "decision": "include",
        "idempotency_key": "k1",
    }


# (method, path suffix, service name, body, service result, action, status)
_MUTATIONS = [
    (
        "queues",
        "create_queue",
        {
            "protocol_version_id": str(uuid4()),
            "stage": "title_abstract",
            "idempotency_key": "q1",
        },
        _queue,
        ResearchAction.SUPERVISE,
        201,
    ),
    (
        "queues/{queue}/assignments",
        "assign",
        {"reviewer_user_id": str(uuid4()), "idempotency_key": "a1"},
        _assignment,
        ResearchAction.SUPERVISE,
        200,
    ),
    (
        "queues/{queue}/assignments/{assignment}/revoke",
        "revoke",
        {"reason": "left", "idempotency_key": "r1"},
        _assignment,
        ResearchAction.SUPERVISE,
        200,
    ),
    (
        "queues/{queue}/observations",
        "submit",
        _observation_body(),
        _observation,
        ResearchAction.REVIEW,
        200,
    ),
]


@pytest.mark.parametrize(
    ("suffix", "service", "body", "result", "action", "status"), _MUTATIONS
)
def test_each_mutation_maps_its_action_and_commits_once(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    service: str,
    body: dict[str, Any],
    result: Any,
    action: ResearchAction,
    status: int,
) -> None:
    project_id, queue_id, assignment_id = uuid4(), uuid4(), uuid4()
    call = AsyncMock(return_value=result())
    monkeypatch.setattr(_routes(), service, call)

    path = suffix.format(queue=queue_id, assignment=assignment_id)
    response = harness.client.post(f"{BASE}/{project_id}/screening/{path}", json=body)

    assert response.status_code == status, response.text
    assert harness.actions == [action]
    assert call.await_args is not None
    assert harness.user.id in call.await_args.args
    if "{queue}" in suffix:
        assert call.await_args.args[2] == queue_id
    harness.db.commit.assert_awaited_once()


def test_reads_use_view_and_never_commit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, queue_id = uuid4(), uuid4()
    monkeypatch.setattr(_routes(), "list_queues", AsyncMock(return_value=[_queue()]))
    monkeypatch.setattr(_routes(), "history", AsyncMock(return_value=[]))
    mine = AsyncMock(
        return_value={
            "queue": {
                "id": str(queue_id),
                "stage": "title_abstract",
                "protocol_version_id": str(uuid4()),
                "criteria_hash": "c" * 64,
                "exclusion_reasons": [],
            },
            "assignment_id": str(uuid4()),
            "items": [],
            "counts": {"total": 0, "screened": 0, "remaining": 0},
        }
    )
    monkeypatch.setattr(_routes(), "my_queue", mine)

    responses = [
        harness.client.get(f"{BASE}/{project_id}/screening/queues"),
        harness.client.get(f"{BASE}/{project_id}/screening/queues/{queue_id}/mine"),
        harness.client.get(f"{BASE}/{project_id}/screening/queues/{queue_id}/history"),
    ]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert harness.actions == [ResearchAction.VIEW] * 3
    assert mine.await_args is not None
    assert mine.await_args.args[2:] == (queue_id, harness.user.id)
    harness.db.commit.assert_not_awaited()


def test_owner_without_reviewer_role_cannot_submit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.denied[ResearchAction.REVIEW] = (403, "reviewer role required")
    submit = AsyncMock()
    monkeypatch.setattr(_routes(), "submit", submit)

    response = harness.client.post(
        f"{BASE}/{uuid4()}/screening/queues/{uuid4()}/observations",
        json=_observation_body(),
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "reviewer role required"
    submit.assert_not_awaited()
    harness.db.commit.assert_not_awaited()


def test_foreign_queue_is_a_stable_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _routes(),
        "submit",
        AsyncMock(
            side_effect=HTTPException(
                status_code=404, detail="Screening queue not found"
            )
        ),
    )

    response = harness.client.post(
        f"{BASE}/{uuid4()}/screening/queues/{uuid4()}/observations",
        json=_observation_body(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Screening queue not found"
    harness.db.commit.assert_not_awaited()


@pytest.mark.parametrize("role", [None, ResearchProjectRole.REVIEWER])
def test_history_is_view_and_redacted_for_the_caller(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    role: ResearchProjectRole | None,
) -> None:
    """GOO-302: any VIEW member reads history; the service redacts what the
    caller cannot see, so the route must pass the caller's own id."""
    if role is not None:
        harness.roles.add(role)
    monkeypatch.setattr(screening_service, "_queue", AsyncMock())
    monkeypatch.setattr(
        screening_service, "replay_decisions", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(screening_service, "_check_resolutions", AsyncMock())
    visible = AsyncMock(return_value=set())
    monkeypatch.setattr(screening_service, "visible_observation_ids", visible)

    response = harness.client.get(
        f"{BASE}/{uuid4()}/screening/queues/{uuid4()}/history"
    )

    assert response.status_code == 200, response.text
    assert harness.actions == [ResearchAction.VIEW]
    assert visible.await_args is not None
    assert visible.await_args.args[2] == harness.user.id
    harness.db.commit.assert_not_awaited()
