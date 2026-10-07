from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from src.api.auth.cli_auth import get_cli_auth_session_store
    from src.api.auth.cli_auth import router as cli_auth_router
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    app = FastAPI()
    app.include_router(cli_auth_router, prefix="/api/v1")
    store = InMemoryCLIAuthSessionStore()
    app.dependency_overrides[get_cli_auth_session_store] = lambda: store

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_cli_auth_start_returns_session_and_browser_url(client: TestClient) -> None:
    response = client.post("/api/v1/cli-auth/start")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["verification_code"]
    assert "/cli-auth?" in body["browser_url"]
    # Regression (GOO-403, RFC 8628 §5.4): the link must not carry the code
    # anywhere (query or fragment). The user types the code their own terminal
    # printed, so a link someone else sends cannot be approved in one click.
    parts = urlsplit(body["browser_url"])
    assert parse_qs(parts.query) == {"session_id": [body["session_id"]]}
    assert parts.fragment == ""
    assert body["verification_code"] not in body["browser_url"]
    assert body["verification_code"].replace("-", "") not in body["browser_url"]
    assert body["poll_token"]
    assert body["poll_interval_seconds"] == 2


def test_frontend_base_url_prefers_dedicated_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.auth.cli_auth import _frontend_base_url
    from src.core.config import settings

    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://www.goodwiinz.tech/")
    # CORS ordering must not influence the redirect target.
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://dev-app.gen-text.app")

    assert _frontend_base_url() == "https://www.goodwiinz.tech"


def test_frontend_base_url_falls_back_to_first_cors_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.auth.cli_auth import _frontend_base_url
    from src.core.config import settings

    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "")
    monkeypatch.setattr(
        settings, "CORS_ORIGINS", "https://www.goodwiinz.tech,http://localhost:3000"
    )

    assert _frontend_base_url() == "https://www.goodwiinz.tech"


def test_cli_auth_status_returns_pending_session(client: TestClient) -> None:
    started = client.post("/api/v1/cli-auth/start").json()

    response = client.get(
        f"/api/v1/cli-auth/status/{started['session_id']}",
        headers={"X-CLI-Poll-Token": started["poll_token"]},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "pending"
    assert body["session_id"] == started["session_id"]


def test_cli_auth_status_rejects_missing_or_wrong_poll_token(
    client: TestClient,
) -> None:
    started = client.post("/api/v1/cli-auth/start").json()
    url = f"/api/v1/cli-auth/status/{started['session_id']}"

    for rejected in (
        client.get(url),
        client.get(url, headers={"X-CLI-Poll-Token": "wrong"}),
    ):
        assert rejected.status_code == 404
        assert rejected.headers["cache-control"] == "no-store"


def test_cli_auth_status_legacy_query_token_warns_without_logging_secret(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    started = client.post("/api/v1/cli-auth/start").json()

    with caplog.at_level(logging.WARNING, logger="src.api.auth.cli_auth"):
        response = client.get(
            f"/api/v1/cli-auth/status/{started['session_id']}",
            params={"poll_token": started["poll_token"]},
        )

    assert response.status_code == 200
    assert "query string" in caplog.text
    assert started["poll_token"] not in caplog.text
