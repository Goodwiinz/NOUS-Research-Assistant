from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

USER_AGENT = "nous-cli/1.4.0 (darwin; arm64)"


@pytest.fixture
def app() -> FastAPI:
    from src.api.auth.cli_auth import get_cli_auth_session_store
    from src.api.auth.cli_auth import router as cli_auth_router
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    app = FastAPI()
    app.include_router(cli_auth_router, prefix="/api/v1")
    app.dependency_overrides[get_cli_auth_session_store] = lambda: store
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _sign_in(app: FastAPI, *, is_cli: bool = False) -> None:
    """Authenticate as user-1: a browser (Supabase) session, or a CLI token."""
    from src.core.dependencies import get_current_user
    from src.core.security import TokenData, get_current_user_token
    from src.models.user import UserRole

    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id="user-1",
        email="admin@multimodal-rag.com",
        organization_id="org-1",
        role="admin",
        is_cli=is_cli,
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1",
        email="admin@multimodal-rag.com",
        organization_id="org-1",
        role=UserRole.ADMIN,
    )


def _wrong_code(code: str) -> str:
    return ("A" if code[0] != "A" else "B") + code[1:]


def test_cli_auth_approve_requires_authenticated_user(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cli-auth/approve",
        json={"session_id": "missing", "verification_code": "bad"},
    )

    assert response.status_code in {401, 403}


def test_cli_auth_approve_marks_session_approved_and_stores_credentials(
    app: FastAPI, client: TestClient
) -> None:
    session = client.post("/api/v1/cli-auth/start").json()
    _sign_in(app)

    approval = client.post(
        "/api/v1/cli-auth/approve",
        headers={"Authorization": "Bearer fake-supabase-token"},
        json={
            "session_id": session["session_id"],
            "verification_code": session["verification_code"],
        },
    )

    assert approval.status_code == 200
    body = approval.json()
    assert body["status"] == "approved"
    assert body["token"]
    assert body["organization_id"] == "org-1"
    assert body["user_email"] == "admin@multimodal-rag.com"

    status_response = client.get(
        f"/api/v1/cli-auth/status/{session['session_id']}",
        headers={"X-CLI-Poll-Token": session["poll_token"]},
    )
    assert status_response.status_code == 200
    assert status_response.headers["cache-control"] == "no-store"
    status_body = status_response.json()
    assert status_body["status"] == "approved"
    assert status_body["token"] == body["token"]


def test_cli_auth_approve_accepts_code_typed_lowercase_without_separator(
    app: FastAPI, client: TestClient
) -> None:
    session = client.post("/api/v1/cli-auth/start").json()
    _sign_in(app)

    typed = session["verification_code"].replace("-", "").lower()
    approval = client.post(
        "/api/v1/cli-auth/approve",
        json={"session_id": session["session_id"], "verification_code": typed},
    )

    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"


def test_cli_auth_approve_rejects_wrong_code_with_400_and_keeps_session_pending(
    app: FastAPI, client: TestClient
) -> None:
    session = client.post("/api/v1/cli-auth/start").json()
    _sign_in(app)

    approval = client.post(
        "/api/v1/cli-auth/approve",
        json={
            "session_id": session["session_id"],
            "verification_code": _wrong_code(session["verification_code"]),
        },
    )

    assert approval.status_code == 400
    assert "token" not in approval.text
    status_body = client.get(
        f"/api/v1/cli-auth/status/{session['session_id']}",
        headers={"X-CLI-Poll-Token": session["poll_token"]},
    ).json()
    assert status_body["status"] == "pending"
    assert "token" not in status_body


def test_cli_auth_approve_unknown_session_is_404(
    app: FastAPI, client: TestClient
) -> None:
    _sign_in(app)

    approval = client.post(
        "/api/v1/cli-auth/approve",
        json={"session_id": "does-not-exist", "verification_code": "ABCD-1234"},
    )

    assert approval.status_code == 404


def test_cli_auth_approve_rejects_cli_token_with_403(
    app: FastAPI, client: TestClient
) -> None:
    # A stolen CLI token must not be able to mint a fresh 30-day CLI token.
    session = client.post("/api/v1/cli-auth/start").json()
    _sign_in(app, is_cli=True)

    approval = client.post(
        "/api/v1/cli-auth/approve",
        json={
            "session_id": session["session_id"],
            "verification_code": session["verification_code"],
        },
    )

    assert approval.status_code == 403
    status_body = client.get(
        f"/api/v1/cli-auth/status/{session['session_id']}",
        headers={"X-CLI-Poll-Token": session["poll_token"]},
    ).json()
    assert status_body["status"] == "pending"


def test_cli_auth_session_info_shows_requester_without_code_or_poll_token(
    app: FastAPI, client: TestClient
) -> None:
    session = client.post(
        "/api/v1/cli-auth/start", headers={"User-Agent": USER_AGENT}
    ).json()
    _sign_in(app)

    response = client.get(f"/api/v1/cli-auth/session/{session['session_id']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "session_id",
        "status",
        "started_at",
        "expires_at",
        "requester_ip",
        "requester_user_agent",
    }
    assert body["session_id"] == session["session_id"]
    assert body["status"] == "pending"
    assert body["requester_ip"] == "testclient"
    assert body["requester_user_agent"] == USER_AGENT
    assert body["started_at"].endswith("Z")
    assert body["expires_at"] == session["expires_at"]
    assert session["verification_code"] not in response.text
    assert session["verification_code"].replace("-", "") not in response.text
    assert session["poll_token"] not in response.text


def test_cli_auth_session_info_requires_browser_session(
    app: FastAPI, client: TestClient
) -> None:
    session = client.post("/api/v1/cli-auth/start").json()
    url = f"/api/v1/cli-auth/session/{session['session_id']}"

    assert client.get(url).status_code in {401, 403}
    _sign_in(app, is_cli=True)
    assert client.get(url).status_code == 403


def test_cli_auth_session_info_unknown_session_is_404(
    app: FastAPI, client: TestClient
) -> None:
    _sign_in(app)

    response = client.get("/api/v1/cli-auth/session/does-not-exist")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_cli_auth_approve_after_five_wrong_codes_is_404_even_with_right_code(
    app: FastAPI, client: TestClient
) -> None:
    # The store denies the session once 5 wrong codes were tried. Telling the
    # user "code does not match" after that would have them retype a correct
    # code into a dead session, so the route reports it as no longer pending.
    session = client.post("/api/v1/cli-auth/start").json()
    _sign_in(app)
    wrong = {
        "session_id": session["session_id"],
        "verification_code": _wrong_code(session["verification_code"]),
    }
    for _ in range(5):
        assert client.post("/api/v1/cli-auth/approve", json=wrong).status_code == 400

    sixth = client.post(
        "/api/v1/cli-auth/approve",
        json={
            "session_id": session["session_id"],
            "verification_code": session["verification_code"],
        },
    )

    assert sixth.status_code == 404
    assert "token" not in sixth.text
    status_body = client.get(
        f"/api/v1/cli-auth/status/{session['session_id']}",
        headers={"X-CLI-Poll-Token": session["poll_token"]},
    ).json()
    assert status_body["status"] == "denied"
    assert "token" not in status_body
