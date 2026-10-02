"""
Async API rate limiting middleware.

I2/I3: the legacy analytics-only middleware limited just the paths
containing "analytics" and drove a sync redis client from inside an async
``dispatch``, silently failing OPEN on ``redis.RedisError``. This module
replaces it with:

- ``ApiRateLimitMiddleware`` — limits every ``/api/`` request. Heavy
  endpoint buckets are selected by longest-prefix match from
  ``API_BUCKETS``; everything else shares the wide per-identity default
  bucket. Legacy analytics requests keep their role-tier budgets.
- ``ApiRateLimiter`` — an async facade over the shared, Lua-atomic
  limiters in ``src.core.rate_limit`` (one store instance per
  ``(limit, window)`` pair). Zero sync Redis I/O.

Failure semantics (I3): a Redis outage degrades to a per-process
in-memory sliding window that still COUNTS requests — never a silent
fail-open. The transition into an outage is logged at ERROR once. Set
``RATE_LIMIT_FAIL_CLOSED=true`` to answer 503 during an outage instead of
degrading.
"""

import inspect
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.core.config import settings
from src.core.rate_limit import InMemoryRateLimiter as CoreInMemoryRateLimiter
from src.core.rate_limit import RedisRateLimiter as CoreRedisRateLimiter
from src.middleware.responses import error_response
from src.models.user import UserRole

logger = logging.getLogger(__name__)

# ponytail: opportunistic full-sweep cadence; move to a TTL cache if key
# cardinality ever matters.
_SWEEP_EVERY_N_CALLS = 1024


class InMemoryRateLimiter:
    """
    In-memory rate limiter using sliding window algorithm
    Fallback when Redis is not available
    """

    def __init__(self):
        # Structure: {key: deque of timestamps}
        self.requests: Dict[str, deque] = defaultdict(deque)
        self._calls_since_sweep = 0
        # Widest window ever seen across all callers (they share this one
        # limiter instance across limit types e.g. 300s api_calls vs 3600s
        # heavy_operations). The periodic sweep must judge staleness against
        # the widest window in use, not whichever call happened to trigger
        # it -- otherwise a 300s-window call sweeps away live 3600s-window
        # keys that are merely >300s old, resetting their hourly budget.
        self._max_window = 0

    def is_allowed(self, key: str, limit: int, window: int) -> Tuple[bool, Dict]:
        """
        Check if request is allowed using sliding window

        Args:
            key: Rate limit key (e.g., user_id, ip_address)
            limit: Max requests allowed
            window: Time window in seconds

        Returns:
            Tuple of (allowed, info_dict)
        """
        now = time.time()
        window_start = now - window
        self._max_window = max(self._max_window, window)

        # Clean old requests
        while self.requests[key] and self.requests[key][0] < window_start:
            self.requests[key].popleft()

        # ponytail: opportunistic eviction; move to TTL cache if key cardinality
        # ever matters. A key with an empty deque after pruning is dropped
        # immediately (defaultdict[key] above would otherwise resurrect it
        # forever), plus a periodic full sweep below catches keys that simply
        # stop being queried (their deque never gets pruned by the line above
        # since is_allowed is never called for them again).
        if not self.requests[key]:
            del self.requests[key]

        self._calls_since_sweep += 1
        if self._calls_since_sweep >= _SWEEP_EVERY_N_CALLS:
            self._calls_since_sweep = 0
            # Judge staleness against the widest window any caller uses, not
            # this call's window -- a short-window call must not evict a
            # longer-window key that's merely older than the short window.
            sweep_horizon = now - self._max_window
            stale_keys = [
                k
                for k, timestamps in self.requests.items()
                if not timestamps or timestamps[-1] < sweep_horizon
            ]
            for stale_key in stale_keys:
                del self.requests[stale_key]

        current_requests = len(self.requests.get(key, ()))

        info = {
            "current_requests": current_requests,
            "limit": limit,
            "window": window,
            "reset_time": now + window,
            "retry_after": None,
        }

        if current_requests >= limit:
            # Calculate when the oldest request will expire
            if self.requests[key]:
                oldest_request = self.requests[key][0]
                info["retry_after"] = int(oldest_request + window - now)
            return False, info

        # Add current request
        self.requests[key].append(now)
        return True, info


# --------------------------------------------------------------------------
# Bucket table — heavy endpoints get their own per-identity budgets.
# Longest-prefix match wins; method gate narrows a bucket to specific verbs.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BucketConfig:
    """One rate-limit bucket: ``requests`` per ``window`` seconds for
    identities whose request path starts with ``prefix`` (and, when set,
    whose method is in ``methods``)."""

    name: str
    requests: int
    window: int  # seconds
    prefix: str = ""
    methods: frozenset = field(default_factory=frozenset)


API_BUCKETS: Tuple[BucketConfig, ...] = (
    # Bulk ingestion is the heaviest abuse vector — tightest budget first.
    BucketConfig(name="arxiv", requests=10, window=60, prefix="/api/v1/arxiv"),
    BucketConfig(name="agent", requests=60, window=60, prefix="/api/v1/agent"),
    # Agent polling / SSE resume / cancel share one read budget (mirrors
    # _AGENT_READ_RPM in api/agent/execute.py) so turn traffic can never
    # starve job tracking or stream recovery. Same name => same counter.
    BucketConfig(
        name="agent_read", requests=120, window=60, prefix="/api/v1/agent/jobs/"
    ),
    BucketConfig(
        name="agent_read",
        requests=120,
        window=60,
        prefix="/api/v1/agent/stream/resume/",
    ),
    BucketConfig(
        name="agent_read",
        requests=120,
        window=60,
        prefix="/api/v1/agent/stream/cancel/",
    ),
    BucketConfig(name="chat", requests=60, window=60, prefix="/api/v1/chat"),
    BucketConfig(name="research", requests=60, window=60, prefix="/api/v1/research"),
    BucketConfig(name="search", requests=60, window=60, prefix="/api/v1/search"),
    BucketConfig(
        name="connectors", requests=60, window=60, prefix="/api/v1/connectors"
    ),
    # The ingestion upload route (api/documents/files.py); other document
    # operations share the default budget.
    BucketConfig(
        name="documents_upload",
        requests=30,
        window=60,
        prefix="/api/v1/files/upload",
        methods=frozenset({"POST"}),
    ),
)

# Per-identity default for every other /api/ route: 600 requests / 5 min.
DEFAULT_BUCKET = BucketConfig(name="default", requests=600, window=300)

# Infra paths only. Auth / CLI-auth routes are NOT skipped: apart from
# /cli-auth/start they have no dedicated limiter, so they use the default
# bucket (double-counting /cli-auth/start against 600/5min is harmless).
_SKIPPED_PREFIXES = (
    "/api/v1/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def is_skipped_path(path: str) -> bool:
    """True for paths this middleware must not count."""
    return path.startswith(_SKIPPED_PREFIXES)


def resolve_bucket(path: str, method: str) -> BucketConfig:
    """Longest-prefix bucket match; method-gated buckets only match their
    verbs (otherwise the default bucket applies)."""
    best: Optional[BucketConfig] = None
    for bucket in API_BUCKETS:
        if not path.startswith(bucket.prefix):
            continue
        if bucket.methods and method.upper() not in bucket.methods:
            continue
        if best is None or len(bucket.prefix) > len(best.prefix):
            best = bucket
    return best if best is not None else DEFAULT_BUCKET


# --------------------------------------------------------------------------
# Async limiter facade over src.core.rate_limit
# --------------------------------------------------------------------------


class _OutageLoggingRedisLimiter(CoreRedisRateLimiter):
    """Core limiter with once-per-outage ERROR logging on the transition into
    a degraded state (core's own ``_log_degraded`` warns at most once per
    minute and otherwise stays quiet)."""

    def _log_degraded(self, exc: Exception) -> None:
        was_active = self._in_fallback_window()
        super()._log_degraded(exc)
        if not was_active:
            logger.error(
                "Rate-limit store unavailable (%s); enforcing per-process "
                "in-memory limits until it recovers",
                type(exc).__name__,
            )


class ApiRateLimiter:
    """Async rate-limit facade.

    One store instance per ``(limit, window)`` pair, lazily created from
    ``src.core.rate_limit`` (Lua-atomic fixed window in Redis; per-process
    sliding-window fallback that still counts during an outage).

    ``use_redis=None`` follows ``settings.REDIS_URL``; pass ``False`` to
    force the in-memory store (tests, single-process deployments).
    """

    def __init__(self, use_redis: Optional[bool] = None):
        if use_redis is None:
            use_redis = bool(settings.REDIS_URL)
        self.use_redis = use_redis
        self._stores: Dict[Tuple[int, int], object] = {}

    def _store_for(self, limit: int, window: int):
        window_minutes = max(1, window // 60)
        cache_key = (limit, window_minutes)
        if cache_key not in self._stores:
            if self.use_redis:
                self._stores[cache_key] = _OutageLoggingRedisLimiter(
                    limit, window_minutes
                )
            else:
                self._stores[cache_key] = CoreInMemoryRateLimiter(limit, window_minutes)
        return self._stores[cache_key]

    async def is_allowed(self, key: str, limit: int, window: int) -> Tuple[bool, Dict]:
        """Check-and-record one request. Returns ``(allowed, info)`` where
        ``info`` mirrors the legacy middleware's dict plus a ``degraded``
        flag (True while this decision came from the in-memory fallback)."""
        store = self._store_for(limit, window)
        now = time.time()
        allowed = await store.is_allowed(key)
        was_degraded = bool(getattr(store, "_in_fallback_window", lambda: False)())

        retry_after: Optional[int] = None
        current_requests: int
        # Upper bound; on a block the store's real expiry is known.
        # ponytail: allowed responses still advertise now + window; exact
        # reset there needs an extra Redis TTL round trip per request.
        reset_time = now + window
        if allowed:
            remaining = await store.get_remaining_attempts(key)
            if (
                not was_degraded
                and getattr(store, "_in_fallback_window", lambda: False)()
            ):
                # A read stall after a successful Redis INCR starts a new
                # fallback window. Count this request there exactly once too.
                allowed = await store.is_allowed(key)
                remaining = await store.get_remaining_attempts(key)
            current_requests = limit - remaining
        if not allowed:
            _, retry_after = await store.check_rate_limit(key)
            current_requests = limit
            if retry_after:
                reset_time = now + retry_after

        # Sampled after EVERY store call: an outage starting between
        # is_allowed() and the follow-up lookup must still read as degraded
        # so RATE_LIMIT_FAIL_CLOSED answers 503.
        degraded = bool(getattr(store, "_in_fallback_window", lambda: False)())

        info = {
            "current_requests": current_requests,
            "limit": limit,
            "window": window,
            "reset_time": reset_time,
            "retry_after": retry_after,
            "degraded": degraded,
        }
        return allowed, info


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


class ApiRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting for API endpoints (audit I2/I3).

    Every ``/api/`` request is limited: heavy endpoint prefixes via
    ``API_BUCKETS`` (longest-prefix match), everything else via the wide
    default bucket. Analytics paths keep their legacy role-tier budgets so
    existing analytics behaviour is unchanged. Keys are per-user
    (``request.state.user_id`` from MultiTenancyMiddleware) with a
    client-host fallback.
    """

    def __init__(self, app, limiter: Optional[ApiRateLimiter] = None):
        super().__init__(app)
        self.rate_limiter = limiter if limiter is not None else ApiRateLimiter()

        # Legacy analytics role-tier configuration — numbers must not change.
        self.analytics_rate_limits = {
            # General analytics limits
            UserRole.USER: {"requests": 100, "window": 3600},  # 100 requests/hour
            UserRole.ANALYST: {"requests": 500, "window": 3600},  # 500 requests/hour
            UserRole.CONTENT_MANAGER: {
                "requests": 1000,
                "window": 3600,
            },  # 1000 requests/hour
            UserRole.ADMIN: {"requests": 2000, "window": 3600},  # 2000 requests/hour
            # Heavy operation limits (e.g., large reports)
            "heavy_operations": {
                "requests": 10,
                "window": 3600,
            },  # 10 heavy operations/hour
            # Export limits
            "exports": {"requests": 20, "window": 3600},  # 20 exports/hour
            # API call limits (smaller window)
            "api_calls": {"requests": 50, "window": 300},  # 50 requests/5 minutes
        }

    # -- helpers ----------------------------------------------------------

    async def _check(self, key: str, limit: int, window: int) -> Tuple[bool, Dict]:
        """Call the limiter, tolerating sync doubles (keeps legacy tests and
        fakes working against the async facade)."""
        result = self.rate_limiter.is_allowed(key=key, limit=limit, window=window)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _identity(self, request: Request) -> str:
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            return str(user_id)
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _is_heavy_operation(request: Request) -> bool:
        """Determine if this is a heavy analytics operation (legacy)."""
        path = request.url.path.lower()

        heavy_patterns = [
            "/reports/generate",
            "/analytics/export",
            "/quality/trends",
            "/behavior/analysis",
            "/performance/summary",
        ]

        return any(pattern in path for pattern in heavy_patterns)

    @staticmethod
    def _is_export_operation(request: Request) -> bool:
        """Determine if this is an export operation (legacy)."""
        path = request.url.path.lower()
        query = request.url.query.lower()

        export_patterns = [
            "/export",
            "/download",
            "format=csv",
            "format=excel",
            "format=pdf",
        ]

        return any(pattern in path or pattern in query for pattern in export_patterns)

    @staticmethod
    def _outage_response(window: int) -> Response:
        """503 for RATE_LIMIT_FAIL_CLOSED deployments during a Redis outage."""
        return error_response(
            503,
            "Rate limit store temporarily unavailable; request rejected in "
            "fail-closed mode.",
            error_type="service_unavailable",
            headers={"Retry-After": str(window)},
        )

    @staticmethod
    def _limited_response(info: Dict) -> Response:
        headers = {
            "X-RateLimit-Limit": str(info["limit"]),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(info["reset_time"])),
        }
        if info.get("retry_after"):
            headers["Retry-After"] = str(info["retry_after"])
        return error_response(
            429,
            f"Rate limit exceeded. Maximum {info['limit']} requests per "
            f"{info['window']} seconds.",
            error_type="rate_limit_error",
            headers=headers,
        )

    @staticmethod
    def _degrade_check(info: Dict, window: int) -> Optional[Response]:
        """503 when fail-closed is on and the decision came from the
        in-memory outage fallback."""
        if settings.RATE_LIMIT_FAIL_CLOSED and info.get("degraded"):
            return ApiRateLimitMiddleware._outage_response(window)
        return None

    # -- dispatch ---------------------------------------------------------

    async def dispatch(self, request: Request, call_next) -> Response:
        # Raw routed path, as in MultiTenancyMiddleware (audit I7): URL parsing
        # drops tab/CR/LF, so url.path could disagree with the routed path.
        path = request.scope["path"]

        # Only API routes count; CORS preflights carry no identity.
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)

        if is_skipped_path(path):
            return await call_next(request)

        if "analytics" in path:
            return await self._dispatch_analytics(request, call_next)

        bucket = resolve_bucket(path, request.method)
        identity = self._identity(request)
        rate_limit_key = f"api:{bucket.name}:{identity}"

        allowed, info = await self._check(
            key=rate_limit_key, limit=bucket.requests, window=bucket.window
        )

        degraded_response = self._degrade_check(info, bucket.window)
        if degraded_response is not None:
            return degraded_response

        log_data = {
            "path": path,
            "method": request.method,
            "bucket": bucket.name,
            "current_requests": info["current_requests"],
            "limit": info["limit"],
            "allowed": allowed,
            "user_id": getattr(request.state, "user_id", None) or identity,
        }

        if not allowed:
            logger.warning(
                "Rate limit exceeded for API request",
                extra=log_data,
            )
            return self._limited_response(info)

        response = await call_next(request)
        remaining = max(0, info["limit"] - info["current_requests"])
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(info["reset_time"]))
        return response

    async def _dispatch_analytics(self, request: Request, call_next) -> Response:
        """Legacy analytics behaviour, ported verbatim (role tiers, heavy /
        export detection, key format, headers) onto the async limiter."""
        # MultiTenancyMiddleware stores the DB-backed role on request.state.
        user_id = getattr(request.state, "user_id", None)
        user_role_value = getattr(request.state, "user_role", UserRole.USER.value)

        try:
            user_role = UserRole(user_role_value)
        except (TypeError, ValueError):
            user_role = UserRole.USER

        # Determine rate limit type and get appropriate limits
        if self._is_heavy_operation(request):
            limit_config = self.analytics_rate_limits["heavy_operations"]
            limit_type = "heavy_operations"
        elif self._is_export_operation(request):
            limit_config = self.analytics_rate_limits["exports"]
            limit_type = "exports"
        else:
            limit_config = self.analytics_rate_limits.get(
                user_role, self.analytics_rate_limits[UserRole.USER]
            )
            limit_type = "api_calls"

        # Generate rate limit key
        if user_id:
            base_key = f"analytics:{user_id}"
        else:
            client_ip = request.client.host if request.client else "unknown"
            base_key = f"analytics:ip:{client_ip}"
        rate_limit_key = f"{base_key}:{limit_type}"

        # Check rate limit
        allowed, info = await self._check(
            key=rate_limit_key,
            limit=limit_config["requests"],
            window=limit_config["window"],
        )

        # Log rate limiting attempt
        log_data = {
            "path": request.url.path,
            "method": request.method,
            "limit_type": limit_type,
            "current_requests": info["current_requests"],
            "limit": info["limit"],
            "allowed": allowed,
        }

        if user_id:
            log_data["user_id"] = user_id
        else:
            log_data["client_ip"] = request.client.host if request.client else "unknown"

        degraded_response = self._degrade_check(info, limit_config["window"])
        if degraded_response is not None:
            return degraded_response

        if not allowed:
            logger.warning(f"Rate limit exceeded for analytics request", extra=log_data)
            return self._limited_response(info)

        logger.info(f"Rate limit check passed for analytics request", extra=log_data)

        # Add rate limit headers to response
        response = await call_next(request)

        remaining_requests = max(0, info["limit"] - info["current_requests"])
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(remaining_requests)
        response.headers["X-RateLimit-Reset"] = str(int(info["reset_time"]))

        return response


# Backwards-compatible alias: existing importers/tests reference the old
# analytics-only name; it is now the same global middleware.
AnalyticsRateLimitMiddleware = ApiRateLimitMiddleware
