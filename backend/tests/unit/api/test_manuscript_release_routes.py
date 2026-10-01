"""Transport contract for the GOO-315 manuscript release routes.

``resolve_project`` is a fake that applies the real RELEASE role policy
(``project_access._DECISION_ROLE``) to whatever roles the test sets. The
candidate path runs the real service over a mocked session with the snapshot
and package builders stubbed. The PostgreSQL proof
(``tests/integration/test_manuscript_release_postgres.py``) covers real
access, packaging, promotion, staleness and reproducibility.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.research import drafts as drafts_api
from src.api.research import manuscript_releases as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole as Role
from src.services.research import manuscript_release_service as svc
from src.services.research import manuscript_rules as rules
from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research_engine.audit_bundle import Part
from src.services.research_engine.project_access import _DECISION_ROLE, ResearchAction

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
KEY = "k-1"
CONTENT = "## Results\nAlpha holds.\n"
SERVICE_FILES = (
    Path(svc.__file__),
    Path(rules.__file__),
    Path(routes.__file__),
)


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
        required = _DECISION_ROLE.get(action)
        if required is not None and not (h.roles & required):
            raise HTTPException(status_code=403, detail="Insufficient project role")
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id),
            workspace=SimpleNamespace(owner_id=USER, members=[]),
            organization_id=uuid4(),
            engine=None,
            effective_roles=h.roles,
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
    return f"/api/v1/projects/{PROJECT}/manuscript-releases"


def test_promote_owner_without_role_403(harness: _Harness) -> None:
    body = {
        "expected_snapshot_hash": "a" * 64,
        "expected_content_hash": "b" * 64,
        "idempotency_key": KEY,
    }
    response = harness.client.post(f"{_base()}/{uuid4()}/promote", json=body)
    # The workspace owner holds no project role: RELEASE refuses before the
    # stream is locked or anything is read.
    assert harness.actions == [ResearchAction.RELEASE]
    assert response.status_code == 403
    harness.db.execute.assert_not_awaited()
    harness.db.commit.assert_not_awaited()


def test_promote_failing_obligations_409_lists_each(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_: object, **__: object) -> Any:
        raise svc.ObligationsNotMet(["claim_support", "peer_review"])

    monkeypatch.setattr(svc, "promote", refuse)
    harness.roles = frozenset({Role.ADJUDICATOR})
    body = {
        "expected_snapshot_hash": "a" * 64,
        "expected_content_hash": "b" * 64,
        "idempotency_key": KEY,
    }
    response = harness.client.post(f"{_base()}/{uuid4()}/promote", json=body)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Release obligations not met",
        "failing": ["claim_support", "peer_review"],
    }


def test_candidate_with_failing_claims_exposes_blockers(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = SimpleNamespace(id=uuid4(), version=1, content=CONTENT, title="t")
    content_hash = svc.claim_rules.content_hash(CONTENT)
    blocker = {
        "code": "unassessed",
        "claim_version_id": str(uuid4()),
        "start": 11,
        "end": 23,
        "text": "Alpha holds.",
        "detail": "No adjudicator assessment",
    }
    snapshot: dict[str, Any] = {
        "draft": {"id": str(draft.id), "content_hash": content_hash},
        "protocol": {"protocol_version_id": None},
    }
    stored: dict[str, bytes] = {}

    async def begin(*_: object, **__: object) -> tuple[str, str, None]:
        return "candidate:k", "f" * 64, None

    async def load_draft(*_: object) -> Any:
        return draft

    async def build(*_: object) -> dict[str, Any]:
        return snapshot

    async def inputs(*_: object) -> rules.CheckInputs:
        return rules.CheckInputs(content_hash, claim_blockers=(blocker,))

    async def parts(*_: object) -> list[Part]:
        return [Part("manuscript.source.md", None, CONTENT.encode(), content_hash)]

    async def put(key: str, content: bytes, _mime: str) -> None:
        stored[key] = content

    monkeypatch.setattr(svc, "_begin", begin)
    monkeypatch.setattr(svc, "_draft", load_draft)
    monkeypatch.setattr(svc, "build_snapshot", build)
    monkeypatch.setattr(svc, "check_inputs", inputs)
    monkeypatch.setattr(svc, "_parts", parts)
    monkeypatch.setattr(svc, "_append", AsyncMock())
    monkeypatch.setattr(
        svc,
        "get_artifact_storage",
        lambda: SimpleNamespace(put=put, delete=AsyncMock()),
    )
    harness.db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=None)
    )
    response = harness.client.post(
        _base(),
        json={
            "draft_id": str(draft.id),
            "expected_content_hash": content_hash,
            "idempotency_key": KEY,
        },
    )
    assert harness.actions == [ResearchAction.EDIT]
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["stage"] == "candidate" and body["status"] == "candidate"
    assert body["external_submission"] == "not_authorized"
    assert set(body["checks"]) == set(rules.CHECK_KEYS)
    assert body["checks"]["claim_support"] == {
        "state": "fail",
        "items": [
            {
                "code": "unassessed",
                "detail": "No adjudicator assessment",
                "ref": blocker["claim_version_id"],
            }
        ],
    }
    assert "claim_support" in body["failing_obligations"]
    (package,) = stored.values()
    assert body["package_sha256"] == svc._sha(package)
    paths = {f["path"] for f in body["package_files"]}
    assert paths == {"manuscript.source.md", "manifest.json", "SHA256SUMS"}
    harness.db.commit.assert_awaited_once()


def test_candidate_with_changed_content_409(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def begin(*_: object, **__: object) -> tuple[str, str, None]:
        return "candidate:k", "f" * 64, None

    async def load_draft(*_: object) -> Any:
        return SimpleNamespace(id=uuid4(), version=1, content=CONTENT)

    monkeypatch.setattr(svc, "_begin", begin)
    monkeypatch.setattr(svc, "_draft", load_draft)
    response = harness.client.post(
        _base(),
        json={
            "draft_id": str(uuid4()),
            "expected_content_hash": "0" * 64,
            "idempotency_key": KEY,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Draft content changed; reload"
    harness.db.commit.assert_not_awaited()


def test_draft_export_route_unchanged(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def owned(*_: object) -> None:
        return None

    async def export(_self: object, **_: object) -> dict[str, Any]:
        return {
            "format": "markdown",
            "filename": "draft.md",
            "content": CONTENT,
            "mime_type": "text/markdown",
        }

    async def never(*_: object, **__: object) -> Any:
        raise AssertionError("an ad-hoc export never builds a release")

    monkeypatch.setattr(drafts_api, "_validate_project_ownership", owned)
    monkeypatch.setattr(DraftGenerationService, "export_draft", export)
    monkeypatch.setattr(svc, "create_candidate", never)
    monkeypatch.setattr(svc, "build_snapshot", never)
    response = harness.client.post(
        f"/api/v1/projects/{PROJECT}/drafts/{uuid4()}/export?format=markdown"
    )
    assert response.status_code == 200
    assert response.text == CONTENT
    assert response.headers["content-disposition"] == (
        'attachment; filename="draft.md"'
    )


def test_no_outbound_http_in_service() -> None:
    banned = {"httpx", "requests", "aiohttp", "urllib", "http"}
    for path in SERVICE_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in banned, f"{path.name}: {name}"
