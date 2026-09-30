"""Transport contract for the GOO-307 promote and release-check routes.

``resolve_project`` is replaced by a fake that applies the real
``_DECISION_ROLE`` map, so RELEASE is refused before the service for an owner
without a role and for a reviewer. The gate itself runs for real on stubbed
inputs.
"""

from __future__ import annotations

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

from src.api.research import drafts as drafts_api
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research import draft_release_service as svc
from src.services.research import release_rules as rr
from src.services.research_engine import project_access
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT = uuid4()
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
SUPPORTED = "Mortality fell by 12% [Doc 1]."
MODEL_ONLY = "The effect was 40% larger [Doc 1]."
CONTENT = f"{SUPPORTED} {MODEL_ONLY}"
HASH = hashlib.sha256(CONTENT.encode()).hexdigest()


def _draft() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        project_id=PROJECT,
        version=1,
        title="Review",
        content=CONTENT,
        themes=[],
        word_count=12,
        citation_count=1,
        is_current=True,
        generation_params={},
        created_at=NOW,
    )


def _claims() -> list[rr.ClaimIn]:
    def claim(text: str, stance: str | None, observed: bool) -> rr.ClaimIn:
        link = rr.LinkIn(id=uuid4(), kind="source_span", live=True, observed=observed)
        start = CONTENT.index(text)
        return rr.ClaimIn(
            claim_version_id=uuid4(),
            kind="factual",
            start=start,
            end=start + len(text),
            text=text,
            attributed_to=None,
            links=(link,),
            assessment=(
                None
                if stance is None
                else rr.AssessmentIn(id=uuid4(), stance=stance, link_ids=(link.id,))
            ),
        )

    return [claim(SUPPORTED, "supporting", False), claim(MODEL_ONLY, None, True)]


class _Harness(SimpleNamespace):
    client: TestClient
    roles: set[Role]
    draft: SimpleNamespace


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    user = SimpleNamespace(id=uuid4())
    roles: set[Role] = set()
    draft = _draft()

    async def fake_resolve(
        _db: object,
        pid: UUID,
        _user_id: UUID,
        action: ResearchAction = ResearchAction.VIEW,
    ) -> SimpleNamespace:
        required = project_access._DECISION_ROLE.get(action)
        if required is not None and required.isdisjoint(roles):
            names = " or ".join(sorted(r.value for r in required))
            raise HTTPException(403, f"{names} role required")
        return SimpleNamespace(
            collection=SimpleNamespace(id=pid), effective_roles=frozenset(roles)
        )

    monkeypatch.setattr(drafts_api, "resolve_project", fake_resolve)
    monkeypatch.setattr(drafts_api, "_validate_project_ownership", AsyncMock())
    monkeypatch.setattr(svc, "_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(svc, "lock_aggregate_stream", AsyncMock())
    monkeypatch.setattr(svc, "_replayed_event", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_all", AsyncMock(return_value=[]))  # no task rows
    monkeypatch.setattr(svc, "_graph", AsyncMock(return_value=svc.Graph([], set())))
    monkeypatch.setattr(svc, "_live", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_releases", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "gate_inputs", AsyncMock(return_value=_claims()))
    app = FastAPI()
    app.include_router(drafts_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield _Harness(client=client, roles=roles, draft=draft)


def _promote(h: _Harness) -> Any:
    return h.client.post(
        f"/api/v1/projects/{PROJECT}/drafts/{h.draft.id}/versions/1/promote",
        json={"content_hash": HASH, "idempotency_key": "p-1"},
    )


def test_promote_reviewer_403_owner_without_role_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    promote = AsyncMock()
    monkeypatch.setattr(svc, "promote", promote)
    denied: list[set[Role]] = [set(), {Role.REVIEWER}]
    for roles in denied:
        harness.roles.clear()
        harness.roles.update(roles)
        response = _promote(harness)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "adjudicator or supervisor role required"
    promote.assert_not_called()


def test_promote_blocked_returns_precise_list(harness: _Harness) -> None:
    harness.roles.add(Role.ADJUDICATOR)
    response = _promote(harness)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "release_blocked"
    (blocker,) = detail["blockers"]
    start = CONTENT.index(MODEL_ONLY)
    assert blocker["code"] == "model_only"
    assert (blocker["start"], blocker["end"]) == (start, start + len(MODEL_ONLY))
    assert blocker["text"] == MODEL_ONLY
    check = harness.client.get(
        f"/api/v1/projects/{PROJECT}/drafts/{harness.draft.id}/versions/1/release"
    )
    assert check.status_code == 200, check.text
    assert check.json()["release_status"] == "candidate"
    assert [b["code"] for b in check.json()["blockers"]] == ["model_only"]


def test_list_drafts_includes_release_status_default_candidate(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = {"drafts": [harness.draft], "total": 1, "skip": 0, "limit": 20}
    monkeypatch.setattr(
        drafts_api.DraftGenerationService,
        "list_drafts",
        AsyncMock(return_value=listed),
    )
    response = harness.client.get(f"/api/v1/projects/{PROJECT}/drafts")
    assert response.status_code == 200, response.text
    (draft,) = response.json()["drafts"]
    assert draft["release_status"] == "candidate"
    assert draft["content_hash"] == HASH
    assert draft["is_current"] is True  # current, and still only a candidate


def test_export_candidate_is_labelled(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = drafts_api.DraftGenerationService
    monkeypatch.setattr(service, "get_draft", AsyncMock(return_value=harness.draft))
    monkeypatch.setattr(service, "get_draft_citations", AsyncMock(return_value=[]))
    response = harness.client.post(
        f"/api/v1/projects/{PROJECT}/drafts/{harness.draft.id}/export"
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert body.startswith("> Status: CANDIDATE — not verified. 1 unresolved item(s).")
    assert f"{MODEL_ONLY} **[UNRESOLVED: model_only]**" in body
    assert harness.draft.content == CONTENT  # the stored bytes are untouched
