"""Tests for multi-tenancy middleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_dispatch_attaches_db_session_to_request_state():
    """Middleware attaches validated db session to request.state."""
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
            "src.middleware.multi_tenancy.MultiTenancyMiddleware._should_skip_tenant_validation",
            return_value=False,
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
        patch(
            "src.middleware.multi_tenancy.verify_token",
            return_value=MagicMock(user_id="user-456", role="user"),
        ),
    ):
        from fastapi import FastAPI, Request

        from src.middleware.multi_tenancy import MultiTenancyMiddleware

        app = FastAPI()
        app.add_middleware(MultiTenancyMiddleware)

        captured = {}

        @app.get("/test-db-attach")
        async def test_endpoint(request: Request):
            captured["has_db"] = hasattr(request.state, "db")
            captured["db_is_async_mock"] = (
                request.state.db is mock_db if hasattr(request.state, "db") else False
            )
            return {"ok": True}

        client = TestClient(app)
        response = client.get(
            "/test-db-attach", headers={"Authorization": "Bearer fake-token"}
        )
        assert response.status_code == 200
        assert (
            captured.get("has_db") is True
        ), "request.state.db should have been set by middleware"
        assert (
            captured.get("db_is_async_mock") is True
        ), "request.state.db should be the same session"


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
        # auth-establishment routes (skip list)
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/refresh",
        # cli-auth device-flow router (api/auth/cli_auth.py:21) is pre-auth.
        "/api/v1/cli-auth/start",
        "/api/v1/cli-auth/status/session-123",
        "/api/v1/cli-auth/approve",
        # kubelet probes (exact-match set)
        "/health",
        "/health/readiness",
        # deliberately anonymous docs/schema pages
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        # enumerated exempt routes (_MIDDLEWARE_EXEMPT_PATH_REGEXES)
        "/",
        "/api/v1/arxiv/search",
        "/api/v1/arxiv/tracking/stats",
        "/api/v1/agent/health",
        "/api/v1/evaluation/health",
        "/api/v1/search-quality/health",
        "/api/v1/search-quality/metrics/types",
        "/api/v1/analytics/quality/health",
        "/api/v1/analytics/behavior/health",
        "/api/v1/analytics/performance/health",
        "/api/v1/analytics/recommendations/health",
        # endpoint-level API-key-auth routes (audit I7 Decision 1)
        "/api/v1/search/authenticated/hybrid",
        "/api/v1/search/authenticated/health",
    ],
)
def test_should_skip_tenant_validation_matches_mounted_paths(path):
    """The skip-list must match the *actual* mounted auth routes.

    Auth router is mounted at ``/api/v1`` + ``/auth`` => ``/api/v1/auth/...``
    (main.py:522, api/auth/auth.py:35). A ``startswith`` check against
    ``/auth/login`` never fired and forced every login/register/refresh request
    through tenant resolution (issue #1003).
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
        # Audit I7: startswith let look-alike paths bypass tenant validation.
        # Anchored regexes must NOT match these.
        "/docsX",
        "/docs/oauth2-redirectX",
        "/docsX/oauth2-redirect",
        "/redocX",
        "/openapi.jsonX",
        "/api/v1/auth/refreshXYZ",  # the exact bypass named by audit I7
        "/api/v1/authX/login",
        "/api/v1/auth/refresh/callback",
        "/api/v1/cli-authX/start",
        "/api/v2/threads",
        # _MIDDLEWARE_EXEMPT_PATH_REGEXES look-alikes: exemptions are
        # exact-route only, so sibling routes (all authenticated) keep tenant
        # validation.
        "/api/v1/arxiv/searchX",
        "/api/v1/arxiv/search/extra",
        "/api/v1/arxiv/ingest",  # authenticated sibling of the public search
        "/api/v1/arxiv/tracking/statsX",
        "/api/v1/arxiv/tracking/history",
        "/api/v1/agent/healthX",
        "/api/v1/agent/stream/health",
        "/api/v1/evaluation/healthX",
        "/api/v1/search-quality/healthX",
        "/api/v1/search-quality/metrics/types/all",
        "/api/v1/analytics/quality/healthX",
        "/api/v1/analytics/behavior/health/summary",
        # API-key-auth route look-alikes stay behind the gate too.
        "/api/v1/search/authenticated",
        "/api/v1/search/authenticated/hybridX",
        "/api/v1/search/authenticated/healthX",
        "/api/v1/search/authenticated/hybrid/extra",
    ],
)
def test_should_skip_tenant_validation_does_not_overmatch(path):
    """Skip/public lists must not swallow tenant-scoped routes or their
    authenticated siblings."""
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


# ===========================================================================
# Audit I7: fail-closed gate, exact skip list, no internal-detail leakage.
# ============================================================================


def _i7_app():
    """Fresh FastAPI app with the real MultiTenancyMiddleware mounted.

    FastAPI's built-in /docs, /redoc and /openapi.json routes are disabled so
    the public-route stubs registered by the tests are the authoritative
    handlers for those paths.
    """
    from fastapi import FastAPI

    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(MultiTenancyMiddleware)
    return app


def _mock_session_cm(db):
    """Mock the ``AsyncSessionLocal()`` async context manager around ``db``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_protected_path_without_authorization_is_401_and_never_opens_db():
    """Audit I7: no Authorization header => immediate 401.

    The old middleware passed such requests through with no tenant context,
    making tenant isolation a per-endpoint convention. The early return must
    also happen BEFORE the middleware opens its per-request DB session.
    """
    from starlette.testclient import TestClient

    db_factory = MagicMock(
        side_effect=AssertionError("must not open a DB session for a 401")
    )
    with patch("src.middleware.multi_tenancy.AsyncSessionLocal", db_factory):
        app = _i7_app()
        hits = []

        @app.get("/api/v1/threads")
        async def threads_endpoint():
            hits.append(1)
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/api/v1/threads")

    assert response.status_code == 401
    assert hits == [], "protected route handler must not run without auth"
    db_factory.assert_not_called()

    body = response.json()
    assert body["error"]["message"] == "Not authenticated"
    assert body["error"]["status_code"] == 401
    assert body["error"]["type"] == "authentication_error"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/login",
        "/api/v1/cli-auth/start",
        "/health",
    ],
)
def test_skip_list_paths_reachable_without_token(path):
    """Token-less requests to skipped paths still reach their route handlers."""
    from starlette.testclient import TestClient

    app = _i7_app()
    reached = []

    @app.get("/api/v1/auth/login")
    async def login_stub():
        reached.append("login")
        return {"ok": True}

    @app.get("/api/v1/cli-auth/start")
    async def cli_auth_start_stub():
        reached.append("cli-auth")
        return {"ok": True}

    @app.get("/health")
    async def health_stub():
        reached.append("health")
        return {"status": "ok"}

    client = TestClient(app)
    response = client.get(path)

    assert response.status_code == 200
    assert len(reached) == 1, f"{path} must reach exactly its own handler"


# (method, path) for every _MIDDLEWARE_EXEMPT_PATH_REGEXES "public metadata"
# exemption. Each public route must be reachable WITHOUT an Authorization
# header through the real middleware — no 401 from the fail-closed gate.
_PUBLIC_ROUTE_STUBS = [
    ("get", "/", "root"),
    ("get", "/docs", "docs"),
    ("get", "/docs/oauth2-redirect", "oauth2_redirect"),
    ("get", "/redoc", "redoc"),
    ("get", "/openapi.json", "openapi"),
    ("post", "/api/v1/arxiv/search", "arxiv_search"),
    ("get", "/api/v1/arxiv/tracking/stats", "tracking_stats"),
    ("get", "/api/v1/agent/health", "agent_health"),
    ("get", "/api/v1/evaluation/health", "evaluation_health"),
    ("get", "/api/v1/search-quality/health", "search_quality_health"),
    ("get", "/api/v1/search-quality/metrics/types", "metric_types"),
    ("get", "/api/v1/analytics/quality/health", "quality_health"),
    ("get", "/api/v1/analytics/behavior/health", "behavior_health"),
    ("get", "/api/v1/analytics/performance/health", "performance_health"),
    ("get", "/api/v1/analytics/recommendations/health", "recommendations_health"),
]


@pytest.mark.parametrize("method,path,label", _PUBLIC_ROUTE_STUBS)
def test_public_path_exemptions_reachable_without_token(method, path, label):
    """Every enumerated public route (Decision 1, option b) must bypass the
    fail-closed tenant gate: reachable with no token, handler actually runs.

    PROBE_EXEMPT_PATHS (/health, /health/readiness) are covered by
    ``test_skip_list_paths_reachable_without_token`` above; this list is the
    public-metadata entries of _MIDDLEWARE_EXEMPT_PATH_REGEXES only. The
    endpoint-level API-key-auth exemptions have their own dedicated tests
    below (their real handlers 401 without a key, so a token-less 200 stub
    here would misrepresent the contract).
    """
    from starlette.testclient import TestClient

    app = _i7_app()
    reached = []

    def _register(method_, path_, name_):
        def handler():
            reached.append(name_)
            return {"ok": True}

        getattr(app, method_)(path_)(handler)

    for m, p, n in _PUBLIC_ROUTE_STUBS:
        _register(m, p, n)

    client = TestClient(app)
    response = getattr(client, method)(path)

    assert response.status_code != 401, (
        f"public route {path} must not be blocked by the fail-closed gate"
    )
    assert response.status_code == 200
    assert reached == [label], f"{path} must reach exactly its own handler"


@pytest.mark.asyncio
async def test_invalid_token_on_protected_path_returns_401_never_500():
    """Audit I7: a Bearer token that fails verification must 401, not pass
    through with no tenant context (and never blow up as a 500)."""
    from starlette.testclient import TestClient

    mock_db = AsyncMock()
    with (
        patch(
            "src.middleware.multi_tenancy.AsyncSessionLocal",
            return_value=_mock_session_cm(mock_db),
        ),
        patch(
            "src.middleware.multi_tenancy.verify_token",
            return_value=None,
        ),
    ):
        app = _i7_app()
        hits = []

        @app.get("/api/v1/threads")
        async def threads_endpoint():
            hits.append(1)
            return {"ok": True}

        client = TestClient(app)
        response = client.get(
            "/api/v1/threads", headers={"Authorization": "Bearer bad-token"}
        )

    assert response.status_code == 401
    assert hits == []
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    # No internals: only the stable message is exposed.
    assert "bad-token" not in response.text


@pytest.mark.asyncio
async def test_tenant_access_db_failure_returns_403_without_internal_text():
    """Audit I7: a DB failure inside tenant validation surfaces as 403 whose
    body never contains the internal exception text."""
    from starlette.testclient import TestClient

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=RuntimeError("S3CR3T-INTERNAL-DB"))
    with (
        patch(
            "src.middleware.multi_tenancy.AsyncSessionLocal",
            return_value=_mock_session_cm(mock_db),
        ),
        patch(
            "src.middleware.multi_tenancy.MultiTenancyMiddleware._extract_tenant_info",
            new_callable=AsyncMock,
            return_value={
                "organization_id": "org-x",
                "user_id": "user-1",
                "role": "user",
            },
        ),
    ):
        app = _i7_app()

        @app.get("/api/v1/threads")
        async def threads_endpoint():
            return {"ok": True}

        client = TestClient(app)
        response = client.get(
            "/api/v1/threads", headers={"Authorization": "Bearer fake-token"}
        )

    assert response.status_code == 403
    assert "S3CR3T-INTERNAL-DB" not in response.text
    body = response.json()
    assert body["error"]["message"].startswith("Access denied")


@pytest.mark.asyncio
async def test_validate_tenant_access_failure_details_keep_only_org_id():
    """Audit I7: the PermissionDeniedException details carry organization_id
    only — the internal exception text goes to the server log, not the client."""
    import json

    from src.exceptions.analytics_exceptions import PermissionDeniedException
    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    middleware = MultiTenancyMiddleware(app=None)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("S3CR3T-INTERNAL-DB"))

    with pytest.raises(PermissionDeniedException) as exc_info:
        await middleware._validate_tenant_access("org-1", db)

    assert exc_info.value.details == {"organization_id": "org-1"}
    assert "S3CR3T" not in json.dumps(exc_info.value.details)


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_untouched():
    """BaseHTTPMiddleware passes non-http scopes (websocket/lifespan) straight
    through without tenant processing — pin that behavior."""
    from src.middleware.multi_tenancy import MultiTenancyMiddleware

    downstream = AsyncMock()
    middleware = MultiTenancyMiddleware(app=downstream)
    scope = {"type": "websocket", "path": "/ws", "headers": []}
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    downstream.assert_awaited_once_with(scope, receive, send)


# ===========================================================================
# Audit I7 Decision 1: endpoint-level API-key-auth exemptions.
#
# POST /api/v1/search/authenticated/hybrid and GET
# /api/v1/search/authenticated/health authenticate via Depends(get_api_key_data)
# (src/core/api_key_auth.py:199) — an API key carried as a Bearer credential,
# verified against hashed DB records. They are NOT JWT routes, so the
# middleware's JWT tenant resolution would 401 every valid API-key request;
# they are exempt from the middleware and rely on their own auth.
# ============================================================================

_API_KEY_ROUTE_STUBS = [
    ("post", "/api/v1/search/authenticated/hybrid", "hybrid"),
    ("get", "/api/v1/search/authenticated/health", "health"),
]


@pytest.mark.parametrize("method,path,label", _API_KEY_ROUTE_STUBS)
def test_api_key_route_with_key_passes_middleware_to_endpoint(method, path, label):
    """A request carrying an API key (Bearer scheme) must pass through the
    middleware's fail-closed gate and reach the endpoint, which performs its
    own authentication. The stub stands in for the real handler."""
    from starlette.testclient import TestClient

    app = _i7_app()
    reached = []

    def handler():
        reached.append(label)
        return {"ok": True}

    getattr(app, method)(path)(handler)

    client = TestClient(app)
    response = getattr(client, method)(
        path, headers={"Authorization": "Bearer rag_validapikey000000000000"}
    )

    assert response.status_code == 200
    assert reached == [label], f"{path} must reach its own handler"


@pytest.mark.parametrize(
    "method,path", [(m, p) for m, p, _ in _API_KEY_ROUTE_STUBS]
)
def test_api_key_route_without_key_rejected_by_endpoint_not_middleware(
    method, path
):
    """Without a key, the rejection must come from the ENDPOINT's own auth
    dependency, not the middleware tenant gate.

    The stub registers the real ``api_key_security`` HTTPBearer scheme used by
    ``get_api_key_data``. Shapes distinguish the two layers:
      * endpoint auth => FastAPI HTTPException => ``{"detail": ...}`` (401)
      * middleware gate => ``error_response`` => ``{"error": {...}}`` (401)
    """
    from fastapi import Depends
    from starlette.testclient import TestClient

    from src.core.api_key_auth import api_key_security

    app = _i7_app()
    reached = []

    def handler(
        credentials=Depends(api_key_security),  # noqa: B008 - test stub
    ):
        reached.append(path)
        return {"ok": True}

    getattr(app, method)(path)(handler)

    client = TestClient(app)
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert reached == [], "handler must not run when the API key is missing"
    body = response.json()
    assert "detail" in body, "must be the endpoint's HTTPException shape"
    assert "error" not in body, "must NOT be the middleware error_response shape"


# ===========================================================================
# Audit I7 Decision 2: families that were effectively open before the
# fail-closed gate stay behind it until endpoint-level auth lands (tracked
# separately in the audit ledger). Representative path per family:
#   * /api/v1/arxiv/bulk/*        (api/arxiv/arxiv_bulk.py:26,93)
#   * /api/v1/arxiv/llm-bulk/*    (api/arxiv/arxiv_llm_bulk.py:29,132)
#   * /api/v1/connectors/*        (api/connectors/router.py:82,116)
#   * research-engine blueprints  (api/research_engine ... templates)
# ============================================================================

_DECISION2_FAMILIES = [
    ("post", "/api/v1/arxiv/bulk/start"),
    ("post", "/api/v1/arxiv/llm-bulk/start"),
    ("post", "/api/v1/connectors/search"),
    ("get", "/api/v1/research-engine/blueprints/templates"),
]


@pytest.mark.parametrize("method,path", _DECISION2_FAMILIES)
def test_effectively_open_families_stay_behind_fail_closed_gate(method, path):
    """No token on these routes => middleware 401, handler never runs."""
    from starlette.testclient import TestClient

    app = _i7_app()
    reached = []

    def handler():
        reached.append(path)
        return {"ok": True}

    getattr(app, method)(path)(handler)

    client = TestClient(app)
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert reached == [], f"{path} handler must not run without a token"
    body = response.json()
    assert body["error"]["message"] == "Not authenticated"
    assert body["error"]["type"] == "authentication_error"
