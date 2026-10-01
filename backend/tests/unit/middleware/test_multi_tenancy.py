"""Tests for multi-tenancy middleware."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _TrackedSession:
    """Stand-in AsyncSession that fails like SQLAlchemy does when it is closed
    while another task still has a query in flight on it."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.closed = False

    async def query(self) -> None:
        if self.closed:
            raise RuntimeError("session used after close")
        self.in_flight += 1
        try:
            await asyncio.sleep(0.05)
        finally:
            self.in_flight -= 1

    async def __aenter__(self) -> "_TrackedSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await asyncio.sleep(0.01)
        if self.in_flight:
            raise RuntimeError("close() while a query is in progress")
        self.closed = True
        return False


def test_streaming_body_does_not_share_the_middleware_session():
    """Dev D-01: GET /research-engine/runs/{id}/stream returned 500 "Internal
    server error during tenant validation" and left the claimed run RUNNING.

    The middleware handed its ``async with`` session to routes via get_db. With
    BaseHTTPMiddleware, ``call_next`` returns as soon as response headers are
    sent, so the middleware closed that session while the StreamingResponse
    body was still querying it; the close raised inside the middleware's
    try/except and became the 500.

    Mutation check (2026-09-30): restoring ``request.state.db = db`` in
    MultiTenancyMiddleware.dispatch (src/middleware/multi_tenancy.py) plus the
    reuse branch in ``get_db`` (src/core/database.py) makes this test fail with
    ``500 != 200``. Command:
    pytest -p no:cacheprovider -q backend/tests/unit/middleware/test_multi_tenancy.py
    -k streaming_body
    """
    from fastapi import Depends, FastAPI
    from fastapi.responses import StreamingResponse
    from starlette.testclient import TestClient

    from src.core.database import get_db
    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    sessions: list[_TrackedSession] = []

    def factory() -> _TrackedSession:
        sessions.append(_TrackedSession())
        return sessions[-1]

    with (
        patch("src.middleware.multi_tenancy.AsyncSessionLocal", side_effect=factory),
        patch("src.core.database.AsyncSessionLocal", side_effect=factory),
        patch.object(
            MultiTenancyMiddleware,
            "_extract_tenant_info",
            new=AsyncMock(
                return_value={
                    "organization_id": "org-123",
                    "user_id": "user-456",
                    "role": "user",
                }
            ),
        ),
        patch.object(
            MultiTenancyMiddleware,
            "_validate_tenant_access",
            new=AsyncMock(return_value=True),
        ),
    ):
        app = FastAPI()
        app.add_middleware(MultiTenancyMiddleware)

        @app.get("/api/v1/research-engine/runs/r1/stream")
        async def stream(db=Depends(get_db)):
            async def body():
                await db.query()
                yield "event: step_complete\ndata: {}\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        response = TestClient(app).get(
            "/api/v1/research-engine/runs/r1/stream",
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 200, response.text
    assert "step_complete" in response.text
    assert all(s.closed for s in sessions)


@pytest.mark.asyncio
async def test_dispatch_skipped_paths_do_not_set_db():
    """Middleware skips tenant validation for paths like /health."""
    with patch(
        "src.middleware.multi_tenancy.MultiTenancyMiddleware._should_skip_tenant_validation",
        return_value=True,
    ):
        from fastapi import FastAPI, Request

        from src.middleware.multi_tenancy import MultiTenancyMiddleware

        app = FastAPI()
        app.add_middleware(MultiTenancyMiddleware)

        captured = {}

        @app.get("/health")
        async def health_endpoint(request: Request):
            captured["has_db"] = hasattr(request.state, "db")
            return {"status": "ok"}

        from starlette.testclient import TestClient

        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert (
            captured.get("has_db") is False
        ), "request.state.db should NOT be set for skipped paths"


@pytest.mark.asyncio
async def test_agent_stream_path_sets_tenant_context():
    """Agent routes need tenant context for downstream services."""
    from starlette.testclient import TestClient

    mock_db = AsyncMock()
    mock_db.is_active = True

    with (
        patch(
            "src.middleware.multi_tenancy.AsyncSessionLocal",
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_db),
                __aexit__=AsyncMock(return_value=False),
            ),
        ),
        patch(
            "src.middleware.multi_tenancy.MultiTenancyMiddleware._extract_tenant_info",
            new_callable=AsyncMock,
            return_value={
                "organization_id": "org-123",
                "user_id": "user-456",
                "role": "user",
            },
        ),
        patch(
            "src.middleware.multi_tenancy.MultiTenancyMiddleware._validate_tenant_access",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        from fastapi import FastAPI

        from src.middleware.multi_tenancy import (
            MultiTenancyMiddleware,
            get_current_tenant_id,
        )

        app = FastAPI()
        app.add_middleware(MultiTenancyMiddleware)

        @app.get("/api/v1/agent/stream/probe")
        async def probe_endpoint():
            return {"tenant_id": get_current_tenant_id()}

        client = TestClient(app)
        response = client.get(
            "/api/v1/agent/stream/probe",
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"tenant_id": "org-123"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/refresh",
        # a sub-path of a skipped route still skips (startswith match)
        "/api/v1/auth/refresh/callback",
        "/health",
        "/health/readiness",
        "/docs",
        "/redoc",
        "/openapi.json",
    ],
)
def test_should_skip_tenant_validation_matches_mounted_paths(path):
    """The skip-list must match the *actual* mounted auth routes.

    Auth router is mounted at ``/api/v1`` + ``/auth`` => ``/api/v1/auth/...``
    (main.py:522, api/auth/auth.py:35). A ``startswith`` check against
    ``/auth/login`` never fires, so every login/register/refresh request
    needlessly opens a DB session and runs JWT verification. Regression
    guard for issue #1003.
    """
    from unittest.mock import MagicMock

    from fastapi import Request

    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    middleware = MultiTenancyMiddleware(app=None)
    mock_request = MagicMock(spec=Request)
    mock_request.url.path = path
    assert (
        middleware._should_skip_tenant_validation(mock_request) is True
    ), f"expected skip for {path!r}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/documents",
        "/api/v1/agent/stream",
        "/api/v1/analytics",
        "/health/detailed",
        "/api/v1/sentry-debug",
        "/",  # root must NOT skip — tenant scope applies
    ],
)
def test_should_skip_tenant_validation_does_not_overmatch(path):
    """Skip-list must not swallow tenant-scoped routes or the bare root."""
    from unittest.mock import MagicMock

    from fastapi import Request

    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    middleware = MultiTenancyMiddleware(app=None)
    mock_request = MagicMock(spec=Request)
    mock_request.url.path = path
    assert (
        middleware._should_skip_tenant_validation(mock_request) is False
    ), f"expected NO skip for {path!r}"


def _fast_path_session(db_user):
    """A mock AsyncSessionLocal() context manager whose execute() resolves to
    db_user (or None)."""
    from unittest.mock import AsyncMock

    class _Res:
        def scalars(self):
            return self

        def first(self):
            return db_user

    prov_db = AsyncMock()
    prov_db.execute = AsyncMock(return_value=_Res())
    prov_db.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=prov_db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_fast_path_validates_db_user_and_uses_db_role():
    """Fast path (org embedded in token) now resolves + validates the live DB
    user; the role comes from the DB, not the token claim."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import Request

    from src.middleware.multi_tenancy import MultiTenancyMiddleware
    from src.models.user import UserRole

    middleware = MultiTenancyMiddleware(app=None)
    middleware._should_skip_tenant_validation = lambda _: False

    token_data_mock = MagicMock()
    token_data_mock.user_id = "user-456"
    token_data_mock.organization_id = "org-embedded"
    token_data_mock.role = "admin"  # token CLAIMS admin

    db_user = SimpleNamespace(
        id="user-456", organization_id="org-embedded", role=UserRole.USER
    )

    with (
        patch(
            "src.middleware.multi_tenancy.verify_token", return_value=token_data_mock
        ),
        patch(
            "src.middleware.multi_tenancy.AsyncSessionLocal",
            return_value=_fast_path_session(db_user),
        ),
        patch(
            "src.middleware.multi_tenancy.ensure_user_and_org",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_request = MagicMock(spec=Request)
        mock_request.headers.get.return_value = "Bearer fake-token"
        result = await middleware._extract_tenant_info(mock_request, db=None)

    assert result is not None
    assert result["organization_id"] == "org-embedded"
    assert result["user_id"] == "user-456"
    assert result["role"] == "user"  # DB role wins over token's "admin"


@pytest.mark.asyncio
async def test_fast_path_denies_inactive_user():
    """Fast path returns no context when the DB user is inactive/deleted (the
    active filter excludes the row, so the lookup resolves to None)."""
    from unittest.mock import AsyncMock

    from fastapi import Request

    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    middleware = MultiTenancyMiddleware(app=None)
    middleware._should_skip_tenant_validation = lambda _: False

    token_data_mock = MagicMock()
    token_data_mock.user_id = "user-456"
    token_data_mock.organization_id = "org-embedded"
    token_data_mock.role = "admin"

    with (
        patch(
            "src.middleware.multi_tenancy.verify_token", return_value=token_data_mock
        ),
        patch(
            "src.middleware.multi_tenancy.AsyncSessionLocal",
            return_value=_fast_path_session(None),
        ),
        patch(
            "src.middleware.multi_tenancy.ensure_user_and_org",
            new=AsyncMock(return_value=None),
        ),
    ):
        mock_request = MagicMock(spec=Request)
        mock_request.headers.get.return_value = "Bearer fake-token"
        result = await middleware._extract_tenant_info(mock_request, db=None)

    assert result is None
