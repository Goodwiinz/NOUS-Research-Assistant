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

import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.core.config import settings

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


def _memory_limiter():
    from src.middleware.rate_limiting import ApiRateLimiter

    return ApiRateLimiter(use_redis=False)


def _make_app(limiter) -> FastAPI:
    """Minimal app exercising the bucket table. The stub user middleware is
    registered after the rate limiter, so it runs first — mirroring
    MultiTenancyMiddleware ordering in main.py."""

    from src.middleware.rate_limiting import ApiRateLimitMiddleware

    app = FastAPI()

    @app.get("/api/v1/search")
    async def search():
        return {"ok": True}

    @app.get("/api/v1/documents")
    @app.post("/api/v1/documents")
    async def documents():
        return {"ok": True}

    @app.get("/api/v1/anything")
    async def anything():
        return {"ok": True}

    @app.post("/api/v1/arxiv/bulk/start")
    async def arxiv_start():
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def login():
        return {"ok": True}

    app.add_middleware(ApiRateLimitMiddleware, limiter=limiter)

    @app.middleware("http")
    async def set_user(request: Request, call_next):
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

    # Documents uploads are method-gated: POST is heavy, GET falls to default.
    upload = resolve_bucket("/api/v1/documents", "POST")
    assert (upload.name, upload.requests) == ("documents_upload", 30)
    assert resolve_bucket("/api/v1/documents", "GET") is DEFAULT_BUCKET

    # Anything else is the wide default bucket.
    default = resolve_bucket("/api/v1/threads/abc/messages", "GET")
    assert default is DEFAULT_BUCKET
    assert (default.requests, default.window) == (600, 300)


def test_skip_paths_exempt_from_limiting() -> None:
    from src.middleware.rate_limiting import is_skipped_path

    for path in (
        "/health",
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth",
        "/api/v1/cli-auth/token",
        "/docs",
        "/openapi.json",
    ):
        assert is_skipped_path(path), path
    assert not is_skipped_path("/api/v1/search")
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
        assert client.post("/api/v1/documents").status_code == 200
    assert client.post("/api/v1/documents").status_code == 429

    # GET on the same prefix uses the default bucket — still allowed.
    assert client.get("/api/v1/documents").status_code == 200


def test_auth_login_not_limited_by_this_middleware() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(70):
        response = client.post("/api/v1/auth/login")
        assert response.status_code == 200, response.status_code


def test_health_and_docs_not_limited() -> None:
    app = _make_app(_memory_limiter())

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/v1/health")
    async def api_health():
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
    assert client.get("/api/v1/search", headers={"x-test-user": "user-b"}).status_code == 200

    # ...and the anonymous fallback key (client host) is its own bucket.
    assert client.get("/api/v1/search").status_code == 200


def test_arxiv_bucket_limit_is_ten_per_minute() -> None:
    client = TestClient(_make_app(_memory_limiter()))

    for _ in range(10):
        assert client.post("/api/v1/arxiv/bulk/start").status_code == 200
    assert client.post("/api/v1/arxiv/bulk/start").status_code == 429


# --------------------------------------------------------------------------
# Redis outage semantics (I3: never silently fail open)
# --------------------------------------------------------------------------


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

    from src.models.user import UserRole
    from src.middleware.rate_limiting import ApiRateLimitMiddleware

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
