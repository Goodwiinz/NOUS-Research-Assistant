"""I2 + I3: global async API rate limiting for all /api/ endpoints.

The legacy AnalyticsRateLimitMiddleware only limited paths containing
"analytics" (I2) and used a sync redis client inside an async dispatch,
failing OPEN on Redis errors (I3). This module pins the replacement:

- ApiRateLimitMiddleware limits every /api/ path (heavy buckets by
  longest-prefix match, default per-user bucket otherwise).
- All Redis I/O is async (via core.rate_limit limiter instances); a Redis
  outage degrades to a per-process in-memory sliding window that still
  COUNTS (never silently fails open). RATE_LIMIT_FAIL_CLOSED=true turns an
  outage into 503 instead.
- evidence/router.py and threads.py must not construct sync redis clients
  either.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from starlette.responses import Response
from starlette.testclient import TestClient

from src.core.config import settings

if TYPE_CHECKING:
    from src.middleware.rate_limiting import ApiRateLimiter

pytestmark = pytest.mark.unit

MIDDLEWARE_SRC = (
    Path(__file__).resolve().parents[3] / "src" / "middleware" / "rate_limiting.py"
)
EVIDENCE_SRC = (
    Path(__file__).resolve().parents[3] / "src" / "api" / "evidence" / "router.py"
)
THREADS_SRC = (
    Path(__file__).resolve().parents[3] / "src" / "api" / "threads" / "threads.py"
)

SEARCH_LIMIT = 60  # per minute, per the bucket table


def _memory_limiter() -> ApiRateLimiter:
    from src.middleware.rate_limiting import ApiRateLimiter

    return ApiRateLimiter(use_redis=False)


def _make_app(limiter: Any) -> FastAPI:
    """Minimal app exercising the bucket table. The stub user middleware is
    registered after the rate limiter, so it runs first — mirroring
    MultiTenancyMiddleware ordering in main.py."""

    from src.middleware.rate_limiting import ApiRateLimitMiddleware

    app = FastAPI()

    @app.get("/api/v1/search")
    async def search() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/documents")
    @app.post("/api/v1/documents")
    async def documents() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/files/upload")
    async def upload() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/agent/execute")
    async def agent_execute() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/agent/jobs/j1")
    async def agent_job() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/auth/cleanup")
    async def auth_cleanup() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/anything")
    async def anything() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/arxiv/bulk/start")
    async def arxiv_start() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(ApiRateLimitMiddleware, limiter=limiter)

    @app.middleware("http")
    async def set_user(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        user_id = request.headers.get("x-test-user")
        if user_id:
            request.state.user_id = user_id
        return await call_next(request)

    return app


# --------------------------------------------------------------------------
# Bucket table / path matching
# --------------------------------------------------------------------------


def test_bucket_table_longest_prefix_and_method_match() -> None:
    from src.middleware.rate_limiting import DEFAULT_BUCKET, resolve_bucket

    search = resolve_bucket("/api/v1/search", "GET")
    assert (search.name, search.requests, search.window) == ("search", 60, 60)

    arxiv = resolve_bucket("/api/v1/arxiv/bulk/start", "POST")
    assert (arxiv.name, arxiv.requests) == ("arxiv", 10)

    for path in ("/api/v1/agent/execute", "/api/v1/agent/thread/t1/stream"):
        bucket = resolve_bucket(path, "POST")
        assert (bucket.name, bucket.requests) == ("agent", 60)

    assert resolve_bucket("/api/v1/chat", "POST").name == "chat"
    assert resolve_bucket("/api/v1/research/writer", "POST").name == "research"
    assert resolve_bucket("/api/v1/connectors", "GET").name == "connectors"

    # Codex P1: the upload bucket targets the real upload route
    # (POST /api/v1/files/upload), not unrelated /documents POSTs.
    upload = resolve_bucket("/api/v1/files/upload", "POST")
    assert (upload.name, upload.requests) == ("documents_upload", 30)
    assert resolve_bucket("/api/v1/documents", "POST") is DEFAULT_BUCKET
    assert resolve_bucket("/api/v1/documents/search", "POST") is DEFAULT_BUCKET
    assert resolve_bucket("/api/v1/documents", "GET") is DEFAULT_BUCKET

    # Codex P2: agent polling/resume/cancel keep their own 120/min budget
    # (mirrors _AGENT_READ_RPM in api/agent/execute.py).
    for method, path in (
        ("GET", "/api/v1/agent/jobs/j1"),
        ("GET", "/api/v1/agent/stream/resume/t1"),
        ("POST", "/api/v1/agent/stream/cancel/t1"),
    ):
        read = resolve_bucket(path, method)
        assert (read.name, read.requests, read.window) == ("agent_read", 120, 60)

    # Anything else is the wide default bucket.
    default = resolve_bucket("/api/v1/threads/abc/messages", "GET")
    assert default is DEFAULT_BUCKET
    assert (default.requests, default.window) == (600, 300)


def test_skip_paths_exempt_from_limiting() -> None:
    from src.middleware.rate_limiting import is_skipped_path

    for path in (
        "/health",
        "/api/v1/health",
        "/docs",
        "/openapi.json",
    ):
        assert is_skipped_path(path), path
    assert not is_skipped_path("/api/v1/search")
    # Codex P2: auth/CLI-auth routes have no dedicated limiter (except
    # /cli-auth/start), so they must count against the default bucket.
    for path in (
        "/api/v1/auth",
        "/api/v1/auth/cleanup",
        "/api/v1/auth/me",
        "/api/v1/cli-auth/status/s1",
        "/api/v1/cli-auth/approve",
    ):
        assert not is_skipped_path(path), path
    assert not is_skipped_path("/api/v1/authentication-helper")  # prefix safety


# --------------------------------------------------------------------------
# Middleware behaviour over HTTP
# --------------------------------------------------------------------------


def test_search_bucket_429_with_retry_after_and_error_body() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(SEARCH_LIMIT):
        assert client.get("/api/v1/search").status_code == 200

    response = client.get("/api/v1/search")
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert response.headers["X-RateLimit-Limit"] == str(SEARCH_LIMIT)

    body = response.json()
    assert body["error"]["status_code"] == 429
    assert body["error"]["type"] == "rate_limit_error"
    assert "message" in body["error"]


def test_default_bucket_independent_of_heavy_buckets() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(SEARCH_LIMIT):
        client.get("/api/v1/search")
    assert client.get("/api/v1/search").status_code == 429

    # The exhausted search bucket must not bleed into the default bucket.
    assert client.get("/api/v1/anything").status_code == 200


def test_documents_upload_bucket_method_gated() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(30):
        assert client.post("/api/v1/files/upload").status_code == 200
    assert client.post("/api/v1/files/upload").status_code == 429

    # Other document operations use the default bucket — still allowed.
    assert client.post("/api/v1/documents").status_code == 200
    assert client.get("/api/v1/documents").status_code == 200


def test_agent_turns_do_not_consume_polling_budget() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(60):
        assert client.post("/api/v1/agent/execute").status_code == 200
    assert client.post("/api/v1/agent/execute").status_code == 429

    # Exhausted turn bucket must not block job polling.
    assert client.get("/api/v1/agent/jobs/j1").status_code == 200


def test_auth_routes_count_against_default_bucket() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    response = client.post("/api/v1/auth/cleanup")
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "600"


@pytest.mark.parametrize("_repeat", range(2))
def test_shared_app_limits_requests_without_leaking_between_tests(
    test_client: TestClient, _repeat: int
) -> None:
    """Each test gets a fresh budget; requests within one test still count."""
    path = "/api/v1/research-engine/capabilities"
    for _ in range(60):
        assert test_client.get(path).status_code == 200

    response = test_client.get(path)
    assert response.status_code == 429
    assert response.headers["X-RateLimit-Limit"] == "60"
    assert response.json()["error"]["type"] == "rate_limit_error"


def test_health_and_docs_not_limited() -> None:
    app = _make_app(_memory_limiter())

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/health")
    async def api_health() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    for _ in range(70):
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_bucket_keyed_by_user_id_when_set_else_client_host() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    # User A exhausts their search budget...
    for _ in range(SEARCH_LIMIT):
        client.get("/api/v1/search", headers={"x-test-user": "user-a"})
    assert (
        client.get("/api/v1/search", headers={"x-test-user": "user-a"}).status_code
        == 429
    )

    # ...while user B (same client host) still has a full budget...
    assert (
        client.get("/api/v1/search", headers={"x-test-user": "user-b"}).status_code
        == 200
    )

    # ...and the anonymous fallback key (client host) is its own bucket.
    assert client.get("/api/v1/search").status_code == 200


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "bEaReR"])
def test_bearer_scheme_casing_cannot_create_a_second_quota(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    from types import SimpleNamespace

    from fastapi import Depends
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    from src.core.security import create_cli_token, verify_token
    from src.middleware import multi_tenancy as tenancy
    from src.middleware.rate_limiting import ApiRateLimitMiddleware
    from src.models.organization import Organization
    from src.models.user import User, UserRole

    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "scheme-test-" + "x" * 32)
    user = SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), role=UserRole.USER
    )
    organization = SimpleNamespace(id=user.organization_id, is_active=True)
    db = AsyncMock()

    async def execute(statement: Any) -> Mock:
        entity = statement.column_descriptions[0]["entity"]
        assert entity in (User, Organization)
        result = Mock()
        result.scalars.return_value.first.return_value = (
            user if entity is User else organization
        )
        return result

    db.execute.side_effect = execute
    session = AsyncMock()
    session.__aenter__.return_value = db
    monkeypatch.setattr(tenancy, "AsyncSessionLocal", Mock(return_value=session))
    monkeypatch.setattr(tenancy, "ensure_user_and_org", AsyncMock(return_value=None))
    token, _ = create_cli_token(
        str(user.id), "scheme@example.test", str(uuid.uuid4()), role="admin"
    )
    limiter = _memory_limiter()
    app = FastAPI()
    app.add_middleware(ApiRateLimitMiddleware, limiter=limiter)
    app.add_middleware(tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/search")
    async def search(
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    ) -> dict[str, str]:
        verified = verify_token(credentials.credentials)
        assert verified is not None and verified.user_id == str(user.id)
        return {
            "user_id": getattr(request.state, "user_id", "missing"),
            "organization_id": getattr(request.state, "tenant_id", "missing"),
            "role": getattr(request.state, "user_role", "missing"),
        }

    client = TestClient(app)
    for _ in range(SEARCH_LIMIT):
        response = client.get(
            "/api/v1/search", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json() == {
            "user_id": str(user.id),
            "organization_id": str(user.organization_id),
            "role": "user",  # DB context wins over the token's admin/org claims.
        }

    assert (
        client.get(
            "/api/v1/search", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 429
    )
    assert (
        client.get(
            "/api/v1/search", headers={"Authorization": f"{scheme} {token}"}
        ).status_code
        == 429
    )
    assert set(limiter._store_for(60, 60).attempts) == {f"api:search:{user.id}"}


def test_main_registers_rate_limiter_inside_tenancy_inside_cors() -> None:
    """Pin the real ``src.main`` stack (outermost first): CORS wraps the
    tenancy gate (so its 401s and the limiter's 429s carry CORS headers) and
    the tenancy gate wraps the limiter (so ``request.state.user_id`` is set
    before the bucket key is chosen)."""
    from fastapi.middleware.cors import CORSMiddleware

    from src.main import app
    from src.middleware.multi_tenancy import MultiTenancyMiddleware
    from src.middleware.rate_limiting import ApiRateLimitMiddleware

    order: list[Any] = [m.cls for m in app.user_middleware]
    assert (
        order.index(CORSMiddleware)
        < order.index(MultiTenancyMiddleware)
        < order.index(ApiRateLimitMiddleware)
    )


def test_arxiv_bucket_limit_is_ten_per_minute() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(10):
        assert client.post("/api/v1/arxiv/bulk/start").status_code == 200
    assert client.post("/api/v1/arxiv/bulk/start").status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("use_redis", [False, True])
async def test_facade_evicts_expired_identities_without_dropping_active_keys(
    monkeypatch: pytest.MonkeyPatch, use_redis: bool
) -> None:
    """Exercise the active async store, including Redis's counted fallback."""
    from src.core import rate_limit as core
    from src.middleware.rate_limiting import ApiRateLimiter

    clock = Mock()
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    clock.now.return_value = start
    monkeypatch.setattr(core, "datetime", clock)
    monkeypatch.setattr(
        core.RedisRateLimiter,
        "_get_redis",
        AsyncMock(side_effect=ConnectionError("offline test store")),
    )
    limiter = ApiRateLimiter(use_redis=use_redis)
    expired = {f"expired-{i}" for i in range(1000)}
    for key in expired:
        allowed, _ = await limiter.is_allowed(key, limit=2, window=60)
        assert allowed

    clock.now.return_value = start + timedelta(seconds=50)
    assert (await limiter.is_allowed("active", limit=2, window=60))[0]
    clock.now.return_value = start + timedelta(seconds=61)
    fresh = {f"fresh-{i}" for i in range(1024)}
    for key in fresh:
        assert (await limiter.is_allowed(key, limit=2, window=60))[0]

    store = limiter._store_for(2, 60)
    memory = store._fallback if use_redis else store
    assert expired.isdisjoint(memory.attempts), "idle expired identities leaked"
    assert set(memory.attempts) == fresh | {"active"}
    assert (await limiter.is_allowed("active", limit=2, window=60))[0]
    assert not (await limiter.is_allowed("active", limit=2, window=60))[0]


# --------------------------------------------------------------------------
# Redis outage semantics (I3: never silently fail open)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_client_has_finite_socket_timeouts() -> None:
    from src.core.rate_limit import RedisRateLimiter

    store = RedisRateLimiter(2, 1, redis_url="redis://unused")
    client = await store._get_redis()  # Constructs the client without connecting.
    options = client.connection_pool.connection_kwargs
    for name in ("socket_connect_timeout", "socket_timeout"):
        assert options.get(name) is not None, f"{name} is unbounded"
        assert 0 < options[name] <= 1
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "stall_at"),
    [
        ("is_allowed", "eval"),
        ("check_rate_limit", "eval"),
        ("record_attempt", "eval"),
        ("get_remaining_attempts", "get"),
        ("is_allowed", "acquire"),
    ],
)
async def test_stalled_redis_operations_have_a_complete_deadline(
    monkeypatch: pytest.MonkeyPatch, operation: str, stall_at: str
) -> None:
    from src.core.rate_limit import RedisRateLimiter

    store = RedisRateLimiter(1, 1, redis_url="redis://unused")
    store.operation_timeout = 0.01
    cancelled = asyncio.Event()

    async def stall(*_args: object, **_kwargs: object) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    stalled = AsyncMock(side_effect=stall)
    store._redis = Mock(eval=stalled, get=stalled)
    if stall_at == "acquire":
        monkeypatch.setattr(store, "_get_redis", stalled)

    # A generous watchdog catches a missing deadline; no elapsed-time tolerance.
    async with asyncio.timeout(1):
        await getattr(store, operation)("identity")
    assert cancelled.is_set(), "the stalled operation was not cancelled"
    assert stalled.await_count == 1, "the outage must not enter a retry wait"
    assert store._in_fallback_window()
    if operation in ("check_rate_limit", "get_remaining_attempts"):
        await store.record_attempt("identity")
    assert not await store.is_allowed("identity"), "fallback must count attempts"


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_at", ["eval", "get"])
async def test_facade_read_stall_counts_the_current_request(
    stall_at: str,
) -> None:
    limiter = _memory_limiter()
    from src.middleware.rate_limiting import _OutageLoggingRedisLimiter

    store = _OutageLoggingRedisLimiter(2, 1, redis_url="redis://unused")
    store.operation_timeout = 0.01
    cancelled = asyncio.Event()

    async def stall(*_args: object, **_kwargs: object) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = Mock(eval=AsyncMock(return_value=1), get=AsyncMock(return_value="1"))
    setattr(client, stall_at, AsyncMock(side_effect=stall))
    store._redis = client
    limiter._stores[(2, 1)] = store

    async with asyncio.timeout(1):
        allowed, info = await limiter.is_allowed("identity", limit=2, window=60)
    assert cancelled.is_set()
    assert allowed and info["degraded"]
    assert info["current_requests"] == 1
    assert (await limiter.is_allowed("identity", limit=2, window=60))[0]
    assert not (await limiter.is_allowed("identity", limit=2, window=60))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_closed", [False, True])
@pytest.mark.parametrize("stall_at", ["eval", "get"])
async def test_read_stall_returns_counted_fallback_or_fail_closed_503(
    monkeypatch: pytest.MonkeyPatch, fail_closed: bool, stall_at: str
) -> None:
    from httpx import ASGITransport, AsyncClient

    from src.middleware.rate_limiting import _OutageLoggingRedisLimiter

    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", fail_closed)
    store = _OutageLoggingRedisLimiter(60, 1, redis_url="redis://unused")
    store.operation_timeout = 0.01

    async def stall(*_args: object, **_kwargs: object) -> Any:
        await asyncio.Event().wait()

    redis = Mock(eval=AsyncMock(return_value=1), get=AsyncMock(return_value="1"))
    stalled = AsyncMock(side_effect=stall)
    setattr(redis, stall_at, stalled)
    store._redis = redis
    limiter = _memory_limiter()
    limiter._stores[(60, 1)] = store
    transport = ASGITransport(app=_make_app(limiter))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with asyncio.timeout(1):
            first = await client.get("/api/v1/search")
            second = await client.get("/api/v1/search")
    assert stalled.await_count == 1
    if fail_closed:
        assert first.status_code == second.status_code == 503
        assert first.json()["error"]["type"] == "service_unavailable"
        assert first.headers["Retry-After"] == "60"
    else:
        assert first.status_code == second.status_code == 200
        assert first.headers["X-RateLimit-Remaining"] == "59"
        assert second.headers["X-RateLimit-Remaining"] == "58"


@pytest.mark.asyncio
async def test_outage_logs_redact_store_exception_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.middleware.rate_limiting import _OutageLoggingRedisLimiter

    sentinel = "never-expose-store-credential"
    store = _OutageLoggingRedisLimiter(1, 1, redis_url="redis://unused")
    monkeypatch.setattr(
        store, "_get_redis", AsyncMock(side_effect=ConnectionError(sentinel))
    )
    with caplog.at_level("WARNING"):
        assert await store.is_allowed("identity")
        assert not await store.is_allowed("identity")
    assert sentinel not in caplog.text
    assert "ConnectionError" in caplog.text
    assert sum(record.levelname == "ERROR" for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_redis_outage_falls_back_to_counting_in_memory_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.middleware.rate_limiting import ApiRateLimiter

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    limiter = ApiRateLimiter(use_redis=True)

    key = f"api:search:{uuid.uuid4()}"
    allowed, info = await limiter.is_allowed(key, limit=2, window=60)
    assert allowed is True
    assert info["degraded"] is True  # Redis unavailable, in-memory fallback used

    allowed, _ = await limiter.is_allowed(key, limit=2, window=60)
    assert allowed is True

    allowed, info = await limiter.is_allowed(key, limit=2, window=60)
    assert allowed is False, "fallback must still ENFORCE the limit (no fail-open)"
    assert info["retry_after"] is not None and info["retry_after"] > 0


@pytest.mark.asyncio
async def test_fail_closed_flag_returns_503_on_redis_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.middleware.rate_limiting import ApiRateLimiter

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", True)

    limiter = ApiRateLimiter(use_redis=True)
    client = TestClient(_make_app(limiter))

    response = client.get("/api/v1/anything")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["status_code"] == 503


@pytest.mark.asyncio
async def test_fail_open_default_allows_request_during_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.middleware.rate_limiting import ApiRateLimiter

    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_CLOSED", False)

    limiter = ApiRateLimiter(use_redis=True)
    client = TestClient(_make_app(limiter))

    assert client.get("/api/v1/anything").status_code == 200


class _FlakyStore:
    """Redis store double: first call succeeds, the follow-up call hits an
    outage and pins the store into its in-memory fallback."""

    def __init__(self, allowed: bool, ttl: int = 0) -> None:
        self._allowed = allowed
        self._ttl = ttl
        self._fallback = False

    def _in_fallback_window(self) -> bool:
        return self._fallback

    async def is_allowed(self, key: str) -> bool:
        return self._allowed

    async def get_remaining_attempts(self, key: str) -> int:
        self._fallback = True  # outage begins between the two calls
        return 1

    async def check_rate_limit(self, key: str) -> tuple[bool, int]:
        return False, self._ttl


@pytest.mark.asyncio
async def test_degraded_sampled_after_every_store_call() -> None:
    """Codex P2: an outage that starts between is_allowed() and the
    remaining-attempts lookup must still surface as degraded (so
    RATE_LIMIT_FAIL_CLOSED answers 503)."""
    from src.middleware.rate_limiting import ApiRateLimiter

    limiter = ApiRateLimiter(use_redis=False)
    limiter._stores[(5, 1)] = _FlakyStore(allowed=True)

    allowed, info = await limiter.is_allowed("k", limit=5, window=60)
    assert allowed is True
    assert info["degraded"] is True


@pytest.mark.asyncio
async def test_blocked_reset_time_matches_store_ttl() -> None:
    """Codex P2: on a 429 the X-RateLimit-Reset must agree with the fixed
    window's real expiry (Retry-After), not now + full window."""
    import time

    from src.middleware.rate_limiting import ApiRateLimiter

    limiter = ApiRateLimiter(use_redis=False)
    limiter._stores[(5, 1)] = _FlakyStore(allowed=False, ttl=10)

    before = time.time()
    allowed, info = await limiter.is_allowed("k", limit=5, window=60)
    assert allowed is False
    assert info["retry_after"] == 10
    assert info["reset_time"] <= time.time() + 10
    assert info["reset_time"] >= before + 10


# --------------------------------------------------------------------------
# Regression guards
# --------------------------------------------------------------------------


def test_no_sync_redis_clients_in_rate_limit_paths() -> None:
    """I3 regression guard: none of the async request paths may construct a
    sync redis client (import redis / redis.Redis / redis.from_url)."""

    for path in (MIDDLEWARE_SRC, EVIDENCE_SRC, THREADS_SRC):
        source = path.read_text()
        assert not re.search(r"^import redis\b", source, re.MULTILINE), path
        assert "redis.Redis(" not in source, path
        assert "redis.from_url(" not in source, path


@pytest.mark.asyncio
async def test_threads_bulk_rate_limit_429_semantics_preserved() -> None:
    from fastapi import HTTPException

    from src.api.threads.threads import check_bulk_rate_limit
    from src.models.user import User

    request = Request({"type": "http", "method": "POST", "url": "http://t/x"})
    user = User(id=uuid.uuid4(), email="u@test.dev", role="user")

    assert (
        await check_bulk_rate_limit(
            request, user, limit=1, window=60, operation="bulk_operation"
        )
        is True
    )

    with pytest.raises(HTTPException) as exc_info:
        await check_bulk_rate_limit(
            request, user, limit=1, window=60, operation="bulk_operation"
        )

    assert exc_info.value.status_code == 429
    headers = exc_info.value.headers or {}
    assert headers.get("X-RateLimit-Limit") == "1"
    assert "Retry-After" in headers


@pytest.mark.asyncio
async def test_evidence_rate_limit_429_semantics_preserved() -> None:
    from fastapi import HTTPException

    from src.api.evidence.router import EvidenceRateLimiter

    limiter = EvidenceRateLimiter(max_requests=2, window_minutes=1)
    key = f"evidence:ip:10.0.0.1:{uuid.uuid4()}"

    assert await limiter.check_rate_limit(key) is True
    assert await limiter.check_rate_limit(key) is True

    with pytest.raises(HTTPException) as exc_info:
        await limiter.check_rate_limit(key)

    assert exc_info.value.status_code == 429
    headers = exc_info.value.headers or {}
    assert headers.get("X-RateLimit-Limit") == "2"
    assert "Retry-After" in headers


# --------------------------------------------------------------------------
# Analytics regression (legacy role-tier behaviour must survive the rewrite)
# --------------------------------------------------------------------------


def test_analytics_role_tiers_survive_rewrite() -> None:
    """The role-tier analytics numbers (user/analyst/content_manager/admin)
    and heavy/export buckets must be unchanged by the rewrite."""

    from src.middleware.rate_limiting import ApiRateLimitMiddleware
    from src.models.user import UserRole

    middleware = ApiRateLimitMiddleware(MockApp())
    limits = middleware.analytics_rate_limits

    assert limits[UserRole.USER] == {"requests": 100, "window": 3600}
    assert limits[UserRole.ANALYST] == {"requests": 500, "window": 3600}
    assert limits[UserRole.CONTENT_MANAGER] == {"requests": 1000, "window": 3600}
    assert limits[UserRole.ADMIN] == {"requests": 2000, "window": 3600}
    assert limits["heavy_operations"] == {"requests": 10, "window": 3600}
    assert limits["exports"] == {"requests": 20, "window": 3600}
    assert limits["api_calls"] == {"requests": 50, "window": 300}


class MockApp:
    """BaseHTTPMiddleware only stores the app; tests never dial it."""
