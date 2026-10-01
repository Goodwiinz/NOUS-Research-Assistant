"""Transport contract for the GOO-318 archive deposit routes.

``resolve_project`` is a fake that records the action and refuses RELEASE
for a caller without a decision role (the real rule is
``project_access._DECISION_ROLE``; the PostgreSQL proof exercises it). The
live Zenodo sandbox is NOT RUN here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.api.research import deposits as routes
from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research import deposit_service as svc
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
SHA = "a" * 64
TOKEN = "zen" + "odo-" + uuid4().hex


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    actions: list[ResearchAction]
    roles: frozenset[str]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    db.add = MagicMock()
    db.commit, db.flush, db.execute, db.rollback = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    h = _Harness(db=db, actions=[], roles=frozenset({"adjudicator"}))

    async def fake_resolve(
        _db: object,
        project_id: UUID,
        _user: object,
        action: ResearchAction = ResearchAction.VIEW,
        **_: object,
    ) -> Any:
        h.actions.append(action)
        if action == ResearchAction.RELEASE and not h.roles:
            raise HTTPException(status_code=403, detail="adjudicator or supervisor")
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id),
            organization_id=uuid4(),
            effective_roles=h.roles,
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    monkeypatch.setattr(settings, "ZENODO_SANDBOX_TOKEN", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "ZENODO_ACCOUNT_LABEL", "nous-fixture")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "release_id": str(uuid4()),
        "package_sha256": SHA,
        "idempotency_key": "k1",
        **extra,
    }


BASE = f"/api/v1/projects/{PROJECT}/deposits"


def test_owner_without_release_role_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.roles = frozenset()
    service = AsyncMock()
    for name in ("approve", "revoke", "request_deposit", "requeue"):
        monkeypatch.setattr(svc, name, service)
    calls = [
        ("approvals", _body(rationale="ok")),
        (f"approvals/{uuid4()}/revoke", {"rationale": "x", "idempotency_key": "k"}),
        ("", _body()),
        (f"{uuid4()}/requeue", None),
    ]
    for path, body in calls:
        response = harness.client.post(f"{BASE}/{path}".rstrip("/"), json=body)
        assert response.status_code == 403, path
    assert harness.actions == [ResearchAction.RELEASE] * 4
    service.assert_not_called()


def test_request_without_approval_409(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = SimpleNamespace(id=uuid4(), package_sha256=SHA, snapshot={})
    monkeypatch.setattr(svc, "_begin", AsyncMock(return_value=("k", "f" * 64, None)))
    monkeypatch.setattr(svc, "_verified_release", AsyncMock(return_value=release))
    monkeypatch.setattr(svc, "_approvals", AsyncMock(return_value=[]))
    response = harness.client.post(BASE, json=_body())
    assert response.status_code == 409
    assert response.json() == {"detail": "No valid deposit approval"}
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()


def test_unconfigured_token_503_no_rows(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ZENODO_SANDBOX_TOKEN", None)
    begin = AsyncMock()
    monkeypatch.setattr(svc, "_begin", begin)
    for path, body in (("", _body()), ("/approvals", _body(rationale="ok"))):
        response = harness.client.post(f"{BASE}{path}", json=body)
        assert response.status_code == 503
        assert response.json() == {"detail": "Archive deposits are not configured"}
    begin.assert_not_awaited()
    harness.db.add.assert_not_called()
    harness.db.commit.assert_not_awaited()
    monkeypatch.setattr(settings, "ZENODO_SANDBOX_TOKEN", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "ZENODO_ACCOUNT_LABEL", "")
    assert harness.client.post(BASE, json=_body()).status_code == 503


def _row(phase: str, previous: Any, **extra: Any) -> SimpleNamespace:
    row = SimpleNamespace(
        id=uuid4(),
        collection_id=PROJECT,
        release_id=extra.pop("release_id", None),
        repository="zenodo_sandbox",
        account_ref="zenodo_sandbox:nous-fixture",
        requested_by_id=USER,
        phase=phase,
        outcome="succeeded",
        retryable=False,
        remote_deposition_id="42",
        remote_record_id=None,
        doi=None,
        reason=None,
        response={},
        files=[],
        previous_id=None if previous is None else previous.id,
        created_at=datetime.now(timezone.utc),
    )
    for key, value in extra.items():
        setattr(row, key, value)
    return row


def test_list_never_shows_doi_before_verified() -> None:
    files = [{"name": "package.zip", "sha256": SHA, "md5": "1" * 32, "bytes": 3}]
    root = _row("prepared", None, release_id=uuid4(), files=files)
    doi = "10.5072/zenodo.42"
    draft = _row("draft_created", root, doi=doi)
    upload = _row("files_uploaded", draft)
    # The remote never said submitted: not published, no DOI.
    unsubmitted = _row("published", upload, remote_record_id="42", doi=doi)
    view = svc._response([root, draft, upload, unsubmitted], [], "pending")
    assert (view.status, view.doi, view.remote_record_id) == (
        "files_uploaded",
        None,
        None,
    )
    published = _row(
        "published",
        upload,
        remote_record_id="42",
        doi=doi,
        response={"submitted": True},
    )
    view = svc._response([root, draft, upload, published], [], "pending")
    assert (view.status, view.doi, view.remote_record_id) == ("published", None, "42")
    mismatch = _row(
        "verified",
        published,
        outcome="failed",
        response={"mismatch": ["checksum:package.zip"]},
        reason="readback_mismatch",
    )
    view = svc._response([root, draft, upload, published, mismatch], [], "done")
    assert (view.status, view.doi) == ("failed", None)
    assert view.attempts[-1].mismatch == ["checksum:package.zip"]
    verified = _row("verified", published, doi=doi, remote_record_id="42")
    view = svc._response([root, draft, upload, published, verified], [], "done")
    assert (view.status, view.doi) == ("verified", doi)
    assert view.doi_url == f"https://doi.org/{doi}"
    assert "doi" not in view.attempts[0].model_dump()
