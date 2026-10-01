"""Transport contract for the GOO-314 peer-review and draft-diff routes.

``resolve_project`` is a fake that records the requested action and grants
whatever roles the test sets, so the service's own adjudicator check is
exercised even when the route-level gate is bypassed. The service runs for
real over a mocked session. The PostgreSQL proof
(``tests/integration/test_peer_review_postgres.py``) covers real access,
anchors, concurrency and retention.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.research import drafts as drafts_api
from src.api.research import peer_review as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research import peer_review_service as svc
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.project_access import ResearchAction
from src.shared.peer_review_schemas import (
    CommentDetail,
    CommentVersionResponse,
    DraftRef,
    RoundDetail,
    RoundResponse,
)

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
KEY = "k-1"


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    roles: frozenset[Role]
    actions: list[ResearchAction]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.add, db.commit, db.flush = MagicMock(), AsyncMock(), AsyncMock()
    db.execute = AsyncMock()
    h = _Harness(db=db, roles=frozenset(), actions=[])

    async def fake_resolve(
        _db: object,
        project_id: UUID,
        _user: object,
        action: ResearchAction = ResearchAction.VIEW,
        **_: object,
    ) -> Any:
        h.actions.append(action)
        collection = SimpleNamespace(id=project_id)
        workspace = SimpleNamespace(owner_id=USER, members=[])
        return SimpleNamespace(
            collection=collection, workspace=workspace, effective_roles=h.roles
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(drafts_api.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _base() -> str:
    return f"/api/v1/projects/{PROJECT}/peer-review"


def test_resolution_by_owner_without_adjudicator_403(harness: _Harness) -> None:
    root = uuid4()
    body = {"kind": "resolved", "response_id": str(uuid4()), "idempotency_key": KEY}
    response = harness.client.post(f"{_base()}/comments/{root}/decisions", json=body)
    # The route asks for ADJUDICATE; even when a (broken) resolver lets the
    # owner through, the service refuses before touching the stream.
    assert harness.actions == [ResearchAction.ADJUDICATE]
    assert response.status_code == 403
    assert response.json()["detail"] == "adjudicator role required"
    harness.db.execute.assert_not_awaited()
    harness.db.commit.assert_not_awaited()
    assignment = {"kind": "assigned", "assignee_id": str(USER), "idempotency_key": KEY}
    harness.client.post(f"{_base()}/comments/{root}/decisions", json=assignment)
    assert harness.actions[-1] == ResearchAction.EDIT


def test_cross_project_revised_draft_404(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def begin(*_: object, **__: object) -> tuple[str, str, None]:
        return "respond:k", "f" * 64, None

    comment = SimpleNamespace(quote=None)
    round_row = SimpleNamespace(draft_id=uuid4())

    async def comment_root(*_: object) -> tuple[Any, list[Any]]:
        return round_row, [comment]

    monkeypatch.setattr(svc, "_begin", begin)
    monkeypatch.setattr(svc, "_comment_root", comment_root)
    # The revised draft lookup is Collection-scoped: another project's id
    # finds nothing.
    harness.db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=None),
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
    )
    response = harness.client.post(
        f"{_base()}/comments/{uuid4()}/responses",
        json={
            "kind": "change",
            "body": "Rewrote it.",
            "revised_draft_id": str(uuid4()),
            "idempotency_key": KEY,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Draft not found"
    harness.db.commit.assert_not_awaited()


def _detail() -> RoundDetail:
    draft_id = uuid4()

    def comment(number: int, status: Any, state: Any, quote: str | None) -> Any:
        version = CommentVersionResponse(
            id=uuid4(),
            round_id=uuid4(),
            reviewer_id=uuid4(),
            number=number,
            body=f"Comment {number}",
            draft_id=draft_id,
            draft_content_hash="a" * 64,
            start_char=None if quote is None else 0,
            end_char=None if quote is None else len(quote),
            quote=quote,
            quote_sha256=None if quote is None else "b" * 64,
            author_id=USER,
            created_at=NOW,
        )
        return CommentDetail(
            comment_root_id=version.id,
            reviewer_id=version.reviewer_id,
            reviewer_label="Reviewer 1",
            number=number,
            status=status,
            anchor_state=state,
            current=version,
            versions=[version],
            responses=[],
            decisions=[],
        )

    return RoundDetail(
        round=RoundResponse(
            id=uuid4(),
            collection_id=PROJECT,
            draft_id=draft_id,
            draft_version=1,
            draft_content_hash="a" * 64,
            label="Round 1",
            created_by_id=USER,
            created_at=NOW,
            reviewers=[],
        ),
        target=DraftRef(id=uuid4(), version=2, content_hash="c" * 64),
        comments=[
            comment(1, "resolved", "unresolved_anchor", "Old sentence."),
            comment(2, "open", "unresolved_anchor", "Deleted sentence."),
            comment(3, "responded", "general", None),
        ],
    )


def test_export_lists_unresolved_section(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _detail()

    async def get_round(*_: object) -> RoundDetail:
        return detail

    monkeypatch.setattr(svc, "get_round", get_round)
    url = f"{_base()}/rounds/{detail.round.id}/export"
    markdown = harness.client.get(url)
    assert markdown.status_code == 200
    assert "attachment" in markdown.headers["content-disposition"]
    text = markdown.text
    unresolved = text.split("## Unresolved items", 1)[1]
    assert "comment 2: open; anchor unresolved_anchor" in unresolved
    assert '(original quote: "Deleted sentence.")' in unresolved
    assert "comment 3: responded" in unresolved
    assert "comment 1:" not in unresolved
    assert "### Comment 1 (resolved; anchor: unresolved_anchor)" in text
    package = json.loads(harness.client.get(url, params={"format": "json"}).content)
    assert package["schema"] == "nous.peer-review-response.v1"
    assert package["body_sha256"] == canonical_json_sha256(package["body"])
    assert [u["status"] for u in package["body"]["unresolved"]] == [
        "open",
        "responded",
    ]


def test_compare_route_unchanged(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    validate = AsyncMock()
    monkeypatch.setattr(drafts_api, "_validate_project_ownership", validate)
    compared = {"similarity_score": 0.5, "word_count_diff": 1}
    monkeypatch.setattr(
        drafts_api.DraftGenerationService,
        "compare_drafts",
        AsyncMock(return_value=compared),
    )
    base = f"/api/v1/projects/{PROJECT}/drafts"
    response = harness.client.get(
        f"{base}/compare", params={"version_a": 1, "version_b": 2}
    )
    assert response.status_code == 200
    assert response.json() == compared
    old, new = "One. Two.", "One. Deux."
    drafts = [
        SimpleNamespace(id=uuid4(), version=1, content=old),
        SimpleNamespace(id=uuid4(), version=2, content=new),
    ]
    harness.db.execute.return_value = MagicMock(
        scalars=MagicMock(return_value=iter(drafts))
    )
    diff = harness.client.get(
        f"{base}/diff",
        params={"from_draft_id": str(drafts[0].id), "to_draft_id": str(drafts[1].id)},
    ).json()
    assert diff["from"]["content_hash"] == hashlib.sha256(old.encode()).hexdigest()
    assert diff["to"]["version"] == 2
    assert diff["hunks"] == [
        {
            "op": "replace",
            "old": [5, 9],
            "new": [5, 10],
            "old_text": "Two.",
            "new_text": "Deux.",
        }
    ]
