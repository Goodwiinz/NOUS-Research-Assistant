"""Transport contract for the GOO-316 statement, venue and ORCID routes.

``resolve_project`` is a fake (no project role needed for EDIT); the ORCID
exchange is mocked, so live OAuth is NOT RUN here. The PostgreSQL proof
(``tests/integration/test_statements_venue_postgres.py``) covers real
statements, approvals, identity states, venue checks and anonymization.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import auth_orcid
from src.api.research import manuscript_releases as release_routes
from src.api.research import statements as routes
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research import manuscript_release_service as release_svc
from src.services.research import statements_service as svc
from src.services.research_engine.project_access import ResearchAction

pytestmark = pytest.mark.unit

PROJECT, USER = uuid4(), uuid4()
# Built by concatenation so no token-shaped literal is committed.
ACCESS = "acc" + "ess-" + uuid4().hex
REFRESH = "ref" + "resh-" + uuid4().hex


class _Harness(SimpleNamespace):
    client: TestClient
    db: MagicMock
    actions: list[ResearchAction]
    added: list[Any]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    db = MagicMock()
    h = _Harness(db=db, actions=[], added=[])
    db.add = MagicMock(side_effect=h.added.append)
    db.commit, db.flush, db.execute = AsyncMock(), AsyncMock(), AsyncMock()

    async def refresh(row: Any) -> None:
        row.created_at = getattr(row, "token_received_at", None)

    db.refresh = refresh

    async def fake_resolve(
        _db: object,
        project_id: UUID,
        _user: object,
        action: ResearchAction = ResearchAction.VIEW,
        **_: object,
    ) -> Any:
        h.actions.append(action)
        return SimpleNamespace(
            collection=SimpleNamespace(id=project_id),
            workspace=SimpleNamespace(owner_id=USER, members=[]),
            organization_id=uuid4(),
            engine=None,
            effective_roles=frozenset(),
        )

    monkeypatch.setattr(routes, "resolve_project", fake_resolve)
    monkeypatch.setattr(release_routes, "resolve_project", fake_resolve)
    app = FastAPI()
    for router in (routes.router, release_routes.router, auth_orcid.router):
        app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.settings, "ORCID_CLIENT_ID", "APP-FIXTURE")
    monkeypatch.setattr(svc.settings, "ORCID_BASE_URL", "https://sandbox.orcid.org")


def test_orcid_start_503_without_client_id(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc.settings, "ORCID_CLIENT_ID", "")
    for path in ("start", "callback?code=c&state=s"):
        response = harness.client.get(f"/api/v1/auth/orcid/{path}")
        assert response.status_code == 503
        assert response.json() == {"detail": "ORCID not configured"}
    _configure(monkeypatch)
    started = harness.client.get("/api/v1/auth/orcid/start")
    assert started.status_code == 200
    url = started.json()["authorize_url"]
    assert url.startswith("https://sandbox.orcid.org/oauth/authorize?")
    assert "scope=%2Fauthenticate" in url and "state=" in url


def test_orcid_callback_rejects_forged_or_expired_state(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    exchange = AsyncMock()
    monkeypatch.setattr(svc, "_exchange", exchange)
    expired = svc.sign_state(USER, now=0.0)
    other = svc.sign_state(uuid4())
    payload, _, signature = svc.sign_state(USER).partition(".")
    forged = f"{payload}.{'0' * len(signature)}"
    for state in (expired, other, forged, "nonsense"):
        response = harness.client.get(
            "/api/v1/auth/orcid/callback", params={"code": "c", "state": state}
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "orcid_state_invalid"}
    denied = harness.client.get(
        "/api/v1/auth/orcid/callback", params={"error": "access_denied: <raw>"}
    )
    assert denied.json() == {"detail": "orcid_denied"}
    exchange.assert_not_awaited()
    assert harness.added == []


def test_orcid_callback_stores_no_token(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)

    async def exchange(code: str) -> dict[str, Any]:
        assert code == "the-code"
        return {
            "orcid": "0000-0002-1825-0097",
            "name": "Ada Lovelace",
            "scope": "/authenticate",
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "token_type": "bearer",
        }

    monkeypatch.setattr(svc, "_exchange", exchange)
    response = harness.client.get(
        "/api/v1/auth/orcid/callback",
        params={"code": "the-code", "state": svc.sign_state(USER)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["orcid"] == "0000-0002-1825-0097"
    assert response.json()["environment"] == "sandbox"
    (row,) = harness.added
    stored = [getattr(row, c.name) for c in row.__table__.columns]
    for secret in (ACCESS, REFRESH):
        assert secret not in response.text
        assert all(secret not in str(value) for value in stored)

    async def broken(code: str) -> dict[str, Any]:
        raise ValueError("raw upstream text")

    monkeypatch.setattr(svc, "_exchange", broken)
    failed = harness.client.get(
        "/api/v1/auth/orcid/callback",
        params={"code": "c", "state": svc.sign_state(USER)},
    )
    assert failed.status_code == 502
    assert failed.json() == {"detail": "orcid_exchange_failed"}


def test_in_app_self_approval_by_non_author_403(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_id = uuid4()
    statement_set = SimpleNamespace(
        id=set_id,
        set_hash="a" * 64,
        supersedes_set_id=None,
        body={
            "authors": [
                {"author_key": "a", "user_id": str(USER)},
                {"author_key": "b", "user_id": None},
            ]
        },
    )

    async def begin(*_: object, **__: object) -> tuple[str, str, None]:
        return "approve:k", "f" * 64, None

    async def returns(value: Any) -> Any:
        return value

    monkeypatch.setattr(svc, "_begin", begin)
    monkeypatch.setattr(svc, "_set", lambda *_: returns(statement_set))
    monkeypatch.setattr(svc, "_sets", lambda *_: returns([statement_set]))
    response = harness.client.post(
        f"/api/v1/projects/{PROJECT}/statements/{set_id}/approvals",
        json={
            "author_key": "b",
            "set_hash": "a" * 64,
            "method": "in_app_self",
            "idempotency_key": "k",
        },
    )
    assert harness.actions == [ResearchAction.EDIT]
    assert response.status_code == 403
    assert response.json()["detail"] == svc.NOT_THE_AUTHOR
    attestation = harness.client.post(
        f"/api/v1/projects/{PROJECT}/statements/{set_id}/approvals",
        json={
            "author_key": "b",
            "set_hash": "a" * 64,
            "method": "recorded_attestation",
            "idempotency_key": "k2",
        },
    )
    assert attestation.status_code == 422
    assert harness.added == []
    harness.db.commit.assert_not_awaited()


def test_package_default_variant_unchanged(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def package(*args: Any) -> tuple[bytes, str]:
        seen.append(args[3])
        return b"zip-" + args[3].encode(), "b" * 64

    monkeypatch.setattr(release_svc, "package_bytes", package)
    base = f"/api/v1/projects/{PROJECT}/manuscript-releases/{uuid4()}/package"
    default = harness.client.get(base)
    assert default.status_code == 200
    assert default.content == b"zip-identified"
    assert default.headers["x-content-sha256"] == "b" * 64
    assert not default.headers["content-disposition"].endswith('-anonymized.zip"')
    anonymized = harness.client.get(base, params={"variant": "anonymized"})
    assert anonymized.content == b"zip-anonymized"
    assert anonymized.headers["content-disposition"].endswith('-anonymized.zip"')
    assert harness.client.get(base, params={"variant": "raw"}).status_code == 422
    assert seen == ["identified", "anonymized"]
