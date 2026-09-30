"""Transport contract for GOO-303 full-text acquisition and PRISMA routes."""

from __future__ import annotations

import json
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
from src.services.research_engine.prisma import (
    PrismaInconsistency,
    PrismaInputs,
    Record,
    Report,
)
from src.services.research_engine.project_access import ResearchAction

BASE = "/api/v1/research-engine/projects"


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    actions: list[ResearchAction]


def _routes() -> Any:
    return import_module("src.api.research_engine.acquisition")


def _state(request_id: UUID | None = None) -> dict[str, Any]:
    return {
        "request_id": request_id or uuid4(),
        "report_id": uuid4(),
        "requested_by_id": uuid4(),
        "requested_at": datetime.now(timezone.utc),
        "state": "pending",
        "attempts": [],
    }


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    db = AsyncMock()
    actions: list[ResearchAction] = []

    async def fake_resolve(
        _db: object, project_id: UUID, user_id: UUID, action: ResearchAction
    ) -> SimpleNamespace:
        assert user_id == user.id
        actions.append(action)
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
            yield _Harness(client=client, user=user, db=db, actions=actions)
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("created", [True, False])
def test_request_is_edit_commits_once_and_replay_is_200(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, created: bool
) -> None:
    project_id = uuid4()
    service = AsyncMock(return_value=(_state(), created))
    monkeypatch.setattr(_routes(), "request_fulltext", service)

    response = harness.client.post(
        f"{BASE}/{project_id}/fulltext/requests",
        json={"report_id": str(uuid4()), "idempotency_key": "k"},
    )

    assert response.status_code == (201 if created else 200), response.text
    assert harness.actions == [ResearchAction.EDIT]
    assert service.await_args is not None
    assert service.await_args.args[2] == harness.user.id
    harness.db.commit.assert_awaited_once()


def test_attempt_is_edit_and_commits_once(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = uuid4()
    service = AsyncMock(return_value=(_state(request_id), True))
    monkeypatch.setattr(_routes(), "record_attempt", service)

    response = harness.client.post(
        f"{BASE}/{uuid4()}/fulltext/requests/{request_id}/attempts",
        json={
            "outcome": "unavailable",
            "attempted_on": "2026-09-29",
            "reason": "not held",
            "idempotency_key": "a",
        },
    )

    assert response.status_code == 201, response.text
    assert harness.actions == [ResearchAction.EDIT]
    assert service.await_args is not None
    assert service.await_args.args[2:4] == (request_id, harness.user.id)
    harness.db.commit.assert_awaited_once()


def test_unavailable_without_reason_is_422_before_the_service(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AsyncMock()
    monkeypatch.setattr(_routes(), "record_attempt", service)

    response = harness.client.post(
        f"{BASE}/{uuid4()}/fulltext/requests/{uuid4()}/attempts",
        json={
            "outcome": "unavailable",
            "attempted_on": "2026-09-29",
            "idempotency_key": "a",
        },
    )

    assert response.status_code == 422
    service.assert_not_awaited()


def test_foreign_request_id_is_a_stable_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _routes(),
        "record_attempt",
        AsyncMock(
            side_effect=HTTPException(
                status_code=404, detail="Full text request not found"
            )
        ),
    )

    response = harness.client.post(
        f"{BASE}/{uuid4()}/fulltext/requests/{uuid4()}/attempts",
        json={
            "outcome": "requested",
            "attempted_on": "2026-09-29",
            "idempotency_key": "a",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Full text request not found"
    harness.db.commit.assert_not_awaited()


def _inputs() -> PrismaInputs:
    report = uuid4()
    return PrismaInputs(
        records=(Record("r1", "provider", "openalex", report),),
        rejected_imports=0,
        workspace_documents=0,
        reports=(Report(report, None, None, None),),
        outcomes=(),
        attempts=(),
        merges=(),
        versions={"protocol_version_ids": [], "stream_heads": {"x:y": 1}},
    )


def test_reads_are_view_and_never_commit(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = uuid4()
    monkeypatch.setattr(_routes(), "list_fulltext", AsyncMock(return_value=[]))
    monkeypatch.setattr(_routes(), "load_inputs", AsyncMock(return_value=_inputs()))

    listed = harness.client.get(f"{BASE}/{project_id}/fulltext")
    flow = harness.client.get(f"{BASE}/{project_id}/prisma")

    assert (listed.status_code, flow.status_code) == (200, 200), flow.text
    assert flow.json()["schema"] == "nous.academic.prisma-flow.v1"
    assert flow.json()["body"]["counts"]["records_identified"] == 1
    assert harness.actions == [ResearchAction.VIEW, ResearchAction.VIEW]
    harness.db.commit.assert_not_awaited()


def test_export_is_an_attachment_whose_body_equals_the_flow(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = uuid4()
    monkeypatch.setattr(_routes(), "load_inputs", AsyncMock(return_value=_inputs()))

    flow = harness.client.get(f"{BASE}/{project_id}/prisma").json()
    exported = harness.client.get(f"{BASE}/{project_id}/prisma/export")
    markdown = harness.client.get(f"{BASE}/{project_id}/prisma/export?format=md")
    bad = harness.client.get(f"{BASE}/{project_id}/prisma/export?format=pdf")

    assert exported.status_code == 200
    disposition = exported.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"prisma-flow-{project_id}-{flow['body_sha256'][:12]}.json" in disposition
    package = json.loads(exported.content)
    assert package["body"] == flow["body"]
    assert package["body_sha256"] == flow["body_sha256"]
    assert markdown.headers["content-disposition"].endswith('.md"')
    assert "flowchart TD" in markdown.text
    assert bad.status_code == 422
    harness.db.commit.assert_not_awaited()


def test_inconsistent_flow_is_a_stable_500(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_routes(), "load_inputs", AsyncMock(return_value=_inputs()))
    monkeypatch.setattr(
        _routes().prisma,
        "derive_prisma_flow",
        lambda _inputs: (_ for _ in ()).throw(PrismaInconsistency("secret detail")),
    )

    response = harness.client.get(f"{BASE}/{uuid4()}/prisma")

    assert response.status_code == 500
    assert "secret detail" not in response.text
    assert "PRISMA flow inconsistent" in response.text
