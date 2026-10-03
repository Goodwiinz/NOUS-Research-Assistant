from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


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


def test_cli_auth_approve_requires_authenticated_user(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cli-auth/approve",
        json={"session_id": "missing", "verification_code": "bad"},
    )

    assert response.status_code in {401, 403}


def test_cli_auth_approve_marks_session_approved_and_stores_credentials(
    app: FastAPI, client: TestClient
) -> None:
    from src.api.auth.cli_auth import get_current_user
    from src.models.user import UserRole

    session = client.post("/api/v1/cli-auth/start").json()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1",
        email="admin@multimodal-rag.com",
        organization_id="org-1",
        role=UserRole.ADMIN,
    )

    try:
        approval = client.post(
            "/api/v1/cli-auth/approve",
            headers={"Authorization": "Bearer fake-supabase-token"},
            json={
                "session_id": session["session_id"],
                "verification_code": session["verification_code"],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

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
