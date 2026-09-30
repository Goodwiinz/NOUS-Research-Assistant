"""Transport contract for the GOO-306 claims routes.

Mutation checks (docs/testing/agent-orchestration-mutation-checks.md, GOO-306):
- dropping the ``ResearchProjectRole.ADJUDICATOR`` check at the top of
  ``claims_service.assess`` makes
  ``test_assess_requires_adjudicator_owner_403`` fail (no service 403);
- deleting the ``_replayed_event`` call in ``claims_service._begin`` makes
  ``test_replay_returns_200_same_ids`` fail (a second ``claim.versioned``);
- removing the pinned-claims pre-check in
  ``DraftGenerationService.delete_draft`` makes
  ``test_delete_draft_with_claims_409`` fail (the draft is deleted).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole
from src.services.research import claims_service as svc
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftRetainedError,
)
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
CONTENT = "Intro. Trials reduced mortality by 12%. End."
PASSAGE = "Trials reduced mortality by 12%."
START = CONTENT.index(PASSAGE)


def _routes() -> Any:
    return import_module("src.api.research.claims")


class _Harness(SimpleNamespace):
    client: TestClient
    user: SimpleNamespace
    db: AsyncMock
    roles: set[ResearchProjectRole]
    workspace_role: list[str]
    base: str
    added: dict[Any, Any]


@pytest.fixture
def harness(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    roles: set[ResearchProjectRole] = set()
    workspace_role = ["owner"]
    project_id = uuid4()
    added: dict[Any, Any] = {}

    def add(row: Any) -> None:
        added[row.id] = row

    async def refresh(row: Any) -> None:
        if getattr(row, "created_at", None) is None:
            row.created_at = NOW

    async def get(_model: Any, row_id: Any) -> Any:
        return added.get(row_id)

    db = AsyncMock()
    db.add = MagicMock(side_effect=add)
    db.refresh = AsyncMock(side_effect=refresh)
    db.get = AsyncMock(side_effect=get)

    async def fake_resolve(
        _db: object,
        pid: UUID,
        user_id: UUID,
        action: ResearchAction = ResearchAction.VIEW,
    ) -> SimpleNamespace:
        """Like resolve_project: EDIT needs editor+ (404); ADJUDICATE is
        deliberately not enforced so the service's own check is exercised."""
        assert user_id == user.id and pid == project_id
        if action == ResearchAction.EDIT and workspace_role[0] == "viewer":
            raise HTTPException(404, "Project not found")
        return SimpleNamespace(
            collection=SimpleNamespace(id=pid),
            effective_roles=frozenset(roles),
            organization_id=user.organization_id,
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
                roles=roles,
                workspace_role=workspace_role,
                base=f"/api/v1/projects/{project_id}/claims",
                added=added,
            )
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """An in-memory claims stream: append records, replay finds by key."""
    events: list[dict[str, Any]] = []

    async def replayed(_db: object, _stream: object, key: str, fingerprint: str) -> Any:
        for event in events:
            if event["idempotency_key"] == key:
                if event["request_fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency conflict")
                return SimpleNamespace(payload=event["payload"])
        return None

    async def append(_db: object, **kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(svc, "lock_aggregate_stream", AsyncMock())
    monkeypatch.setattr(svc, "_replayed_event", replayed)
    monkeypatch.setattr(svc, "append_decision", append)
    return events


def _draft() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), version=1, content=CONTENT, generation_params=None
    )


def _claim_body(draft_id: Any, **overrides: Any) -> dict[str, Any]:
    return {
        "draft_id": str(draft_id),
        "start_char": START,
        "end_char": START + len(PASSAGE),
        "text": PASSAGE,
        "idempotency_key": "c1",
        **overrides,
    }


def test_create_requires_edit_viewer_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock()
    monkeypatch.setattr(svc, "create_claim", create)
    harness.workspace_role[0] = "viewer"
    response = harness.client.post(harness.base, json=_claim_body(uuid4()))
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Project not found"
    create.assert_not_awaited()


def test_assess_requires_adjudicator_owner_403(harness: _Harness) -> None:
    # A workspace owner with no research role holds no implicit ADJUDICATE
    # power; the service refuses before it touches the database.
    body = {
        "claim_version_id": str(uuid4()),
        "stance": "supporting",
        "link_ids": [str(uuid4())],
        "rationale": "the trial reports it",
        "idempotency_key": "a1",
    }
    response = harness.client.post(f"{harness.base}/{uuid4()}/assessments", json=body)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "adjudicator role required"
    harness.db.execute.assert_not_awaited()


def test_text_mismatch_422_stable_detail(
    harness: _Harness, ledger: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _draft()
    monkeypatch.setattr(svc, "_draft", AsyncMock(return_value=draft))
    response = harness.client.post(
        harness.base,
        json=_claim_body(draft.id, text="Trials reduced mortality by 13%."),
    )
    assert response.status_code == 422
    assert (
        response.json()["error"]["message"] == "Text does not match the draft passage"
    )
    assert ledger == []


def test_legacy_link_has_no_hash_or_span(
    harness: _Harness, ledger: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_id, version_id, citation_id = uuid4(), uuid4(), uuid4()
    version = SimpleNamespace(id=version_id, draft_id=uuid4())
    monkeypatch.setattr(svc, "_claim", AsyncMock())
    monkeypatch.setattr(svc, "_version", AsyncMock(return_value=version))
    monkeypatch.setattr(svc, "_version_tip", AsyncMock(return_value=version))
    monkeypatch.setattr(
        svc,
        "_legacy_target",
        AsyncMock(return_value={"draft_citation_id": citation_id, "document_id": None}),
    )
    url = f"{harness.base}/{claim_id}/links"
    body = {
        "claim_version_id": str(version_id),
        "kind": "legacy_unanchored",
        "draft_citation_id": str(citation_id),
        "idempotency_key": "l1",
    }
    response = harness.client.post(url, json={**body, "start_char": 0, "end_char": 4})
    assert response.status_code == 422
    assert "must not carry start_char, end_char" in response.json()["error"]["message"]

    response = harness.client.post(url, json=body)
    assert response.status_code == 201, response.text
    link = response.json()
    assert link["kind"] == "legacy_unanchored"
    assert link["draft_citation_id"] == str(citation_id)
    for column in ("source_hash", "text_sha256", "start_char", "end_char", "quote"):
        assert link[column] is None
    (event,) = ledger
    assert event["event_type"] == "claim.linked"
    assert event["payload"]["quote_sha256"] is None


def test_delete_draft_with_claims_409(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = import_module("src.api.research.drafts")
    monkeypatch.setattr(drafts, "_validate_project_ownership", AsyncMock())
    draft = SimpleNamespace(id=uuid4(), is_current=False)
    found, pinned = MagicMock(), MagicMock()
    found.scalar_one_or_none.return_value = draft
    pinned.first.return_value = (uuid4(),)
    harness.db.execute = AsyncMock(side_effect=[found, pinned])
    response = harness.client.delete(
        harness.base.replace("/claims", f"/drafts/{draft.id}")
    )
    assert response.status_code == 409
    assert (
        response.json()["error"]["message"]
        == "Draft has claims; it is retained as evidence"
    )
    harness.db.delete.assert_not_awaited()
    harness.db.commit.assert_not_awaited()


async def test_delete_draft_service_raises_retained() -> None:
    db = AsyncMock()
    found, pinned = MagicMock(), MagicMock()
    found.scalar_one_or_none.return_value = SimpleNamespace(is_current=True)
    pinned.first.return_value = (uuid4(),)
    db.execute = AsyncMock(side_effect=[found, pinned])
    with pytest.raises(DraftRetainedError):
        await DraftGenerationService(db).delete_draft(uuid4(), uuid4())
    db.delete.assert_not_awaited()


def test_replay_returns_200_same_ids(
    harness: _Harness, ledger: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _draft()
    monkeypatch.setattr(svc, "_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(svc, "_draft_review_id", AsyncMock(return_value=None))
    body = _claim_body(draft.id)

    first = harness.client.post(harness.base, json=body)
    assert first.status_code == 201, first.text
    created = first.json()
    assert created["version"]["version_no"] == 1
    assert created["version"]["text"] == PASSAGE
    assert created["version"]["draft_content_hash"] == svc.claim_rules.content_hash(
        CONTENT
    )

    again = harness.client.post(harness.base, json=body)
    assert again.status_code == 200
    assert again.json()["id"] == created["id"]
    assert again.json()["version"]["id"] == created["version"]["id"]
    assert [e["event_type"] for e in ledger] == ["claim.versioned"]

    conflict = harness.client.post(
        harness.base, json={**body, "kind": "interpretation"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["message"] == "Idempotency conflict"
    assert len(ledger) == 1
