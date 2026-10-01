"""PR #1722 review follow-ups.

P2: the tenancy gate's own 401/500 responses must carry CORS headers for an
allowed Origin. The browser calls the API cross-origin; without
``Access-Control-Allow-Origin`` an expired token surfaces as an opaque
"Failed to fetch" and the frontend's refresh/logout-on-401 never runs. These
tests drive the real ``src.main`` middleware stack, so they pin the
registration order (CORSMiddleware outside MultiTenancyMiddleware).

P3: skip/probe matching uses the raw ``scope["path"]`` that Starlette routes
on, not ``request.url.path`` (which drops tab/CR/LF), so a control-character
look-alike of an exempt path is never treated as exempt.
"""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from src.core.config import settings
from src.middleware import multi_tenancy
from src.middleware.multi_tenancy import MultiTenancyMiddleware

PROTECTED = "/api/v1/workspaces"
DISALLOWED_ORIGIN = "https://evil.example"


@pytest.fixture
def client(test_app: FastAPI) -> Iterator[TestClient]:
    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, raise_server_exceptions=False) as c:
            yield c
    finally:
        test_app.router.lifespan_context = original


def _allowed_origin() -> str:
    return settings.cors_origins_list[0]


def _assert_cors_for(response: Any, origin: str) -> None:
    assert response.headers.get("access-control-allow-origin") == origin
    assert "origin" in response.headers.get("vary", "").lower()


@pytest.mark.parametrize("auth", [None, "Basic dXNlcjpwYXNz", "Bearer not-a-valid-jwt"])
def test_gate_401_carries_cors_for_allowed_origin(
    client: TestClient, auth: str | None
) -> None:
    origin = _allowed_origin()
    headers = {"Origin": origin}
    if auth:
        headers["Authorization"] = auth

    response = client.get(PROTECTED, headers=headers)

    assert response.status_code == 401
    _assert_cors_for(response, origin)


def test_gate_401_has_no_cors_for_disallowed_origin(client: TestClient) -> None:
    assert DISALLOWED_ORIGIN not in settings.cors_origins_list
    response = client.get(PROTECTED, headers={"Origin": DISALLOWED_ORIGIN})

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


def test_gate_500_lookup_outage_carries_cors_for_allowed_origin(
    client: TestClient,
    test_auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("S3CR3T-DB-DOWN"))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        multi_tenancy, "AsyncSessionLocal", MagicMock(return_value=session_cm)
    )
    origin = _allowed_origin()

    response = client.get(PROTECTED, headers={**test_auth_headers, "Origin": origin})

    assert response.status_code == 500
    assert "S3CR3T" not in response.text
    _assert_cors_for(response, origin)


def test_true_preflight_still_answered_without_auth(client: TestClient) -> None:
    origin = _allowed_origin()
    response = client.options(
        PROTECTED,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin


def _request(raw_path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


@pytest.mark.parametrize(
    "raw_path",
    [
        "/api/v1/agent/heal\tth",  # look-alike of a _MIDDLEWARE_EXEMPT path
        "/api/v1/auth/log\nin",  # look-alike of a _SKIP path
        "/heal\rth",  # look-alike of a PROBE_EXEMPT path
        "/do\tcs",
    ],
)
def test_control_character_lookalikes_are_not_exempt(raw_path: str) -> None:
    request = _request(raw_path)
    # Precondition: the URL view drops the control character, which is
    # exactly why matching must use the raw scope path.
    assert "\t" not in request.url.path and "\n" not in request.url.path
    assert "\r" not in request.url.path

    middleware = MultiTenancyMiddleware(app=MagicMock())
    assert middleware._should_skip_tenant_validation(request) is False


@pytest.mark.parametrize(
    "raw_path", ["/api/v1/agent/health", "/api/v1/auth/login", "/health", "/docs"]
)
def test_exact_exempt_paths_still_exempt_on_raw_path(raw_path: str) -> None:
    middleware = MultiTenancyMiddleware(app=MagicMock())
    assert middleware._should_skip_tenant_validation(_request(raw_path)) is True
