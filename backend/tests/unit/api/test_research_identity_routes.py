"""Transport contract for GOO-299 report identity routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
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
from src.services.research_engine.project_access import ResearchAction

BASE = "/api/v1/research-engine/projects"


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    actions: list[ResearchAction]
    denied: dict[ResearchAction, int]


def _routes() -> Any:
    return import_module("src.api.research_engine.identities")


def _report(report_id: UUID, status: str | None = None) -> dict[str, Any]:
    return {
        "id": report_id,
        "title_snapshot": "Paper",
        "identifiers": {"doi": ["10.1000/x"]},
        "study_id": uuid4() if status else None,
        "study_link_status": status,
        "observations": [],
    }


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    db = AsyncMock()
    actions: list[ResearchAction] = []
    denied: dict[ResearchAction, int] = {}

    async def fake_resolve(
        _db: object, project_id: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        assert user_id == user.id
        actions.append(action)
        if action in denied:
            raise HTTPException(status_code=denied[action], detail="denied")
        return SimpleNamespace(collection=SimpleNamespace(id=project_id))

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
                client=client, user=user, db=db, actions=actions, denied=denied
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _link_body(status: str) -> dict[str, Any]:
    return {"status": status, "rationale": "why", "idempotency_key": "k1"}


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("proposed", ResearchAction.REVIEW),
        ("confirmed", ResearchAction.ADJUDICATE),
        ("disputed", ResearchAction.ADJUDICATE),
    ],
)
def test_study_link_maps_status_to_action_and_commits_once(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    action: ResearchAction,
) -> None:
    project_id, report_id = uuid4(), uuid4()
    link = AsyncMock(return_value=_report(report_id, status))
    monkeypatch.setattr(_routes(), "link_study", link)

    response = harness.client.post(
        f"{BASE}/{project_id}/reports/{report_id}/study-link", json=_link_body(status)
    )

    assert response.status_code == 200, response.text
    assert response.json()["study_link_status"] == status
    assert harness.actions == [action]
    assert link.await_args is not None
    assert link.await_args.args[2:4] == (report_id, harness.user.id)
    harness.db.commit.assert_awaited_once()


def test_missing_role_is_403_without_calling_the_service(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.denied[ResearchAction.ADJUDICATE] = 403
    link = AsyncMock()
    monkeypatch.setattr(_routes(), "link_study", link)

    response = harness.client.post(
        f"{BASE}/{uuid4()}/reports/{uuid4()}/study-link",
        json=_link_body("confirmed"),
    )

    assert response.status_code == 403
    link.assert_not_awaited()
    harness.db.commit.assert_not_awaited()


def test_foreign_report_is_a_stable_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _routes(),
        "candidates",
        AsyncMock(
            side_effect=HTTPException(status_code=404, detail="Report not found")
        ),
    )

    response = harness.client.get(f"{BASE}/{uuid4()}/reports/{uuid4()}/candidates")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Report not found"
    assert harness.actions == [ResearchAction.VIEW]


def test_merge_and_split_require_adjudication(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, survivor, loser = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        _routes(), "merge_reports", AsyncMock(return_value=_report(survivor))
    )
    monkeypatch.setattr(
        _routes(), "split_report", AsyncMock(return_value=_report(uuid4()))
    )

    merged = harness.client.post(
        f"{BASE}/{project_id}/reports/merge",
        json={
            "surviving_report_id": str(survivor),
            "merged_report_ids": [str(loser)],
            "rationale": "dup",
            "idempotency_key": "m1",
        },
    )
    split = harness.client.post(
        f"{BASE}/{project_id}/reports/{survivor}/split",
        json={"source_ids": [str(uuid4())], "rationale": "no", "idempotency_key": "s1"},
    )

    assert (merged.status_code, split.status_code) == (200, 200)
    assert harness.actions == [ResearchAction.ADJUDICATE, ResearchAction.ADJUDICATE]
    assert harness.db.commit.await_count == 2


def test_reads_use_view_and_never_commit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = uuid4()
    monkeypatch.setattr(
        _routes(), "list_reports", AsyncMock(return_value=[_report(uuid4())])
    )
    monkeypatch.setattr(_routes(), "history", AsyncMock(return_value=[]))

    listed = harness.client.get(f"{BASE}/{project_id}/reports")
    replayed = harness.client.get(f"{BASE}/{project_id}/reports/history")

    assert (listed.status_code, replayed.status_code) == (200, 200)
    assert harness.actions == [ResearchAction.VIEW, ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()
