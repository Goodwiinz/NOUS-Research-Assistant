"""
Shared utilities for microservices
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Union

import httpx
import redis.asyncio as redis
from fastapi import Request, Response
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Middleware to add correlation IDs to requests"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())

        # Add correlation ID to request state
        request.state.correlation_id = correlation_id

        # Call next middleware/route
        response = await call_next(request)

        # Add correlation ID to response headers
        response.headers["X-Correlation-ID"] = correlation_id

        return response


def get_correlation_id(request: Request) -> str:
    """Get correlation ID from request"""
    correlation_id = getattr(request.state, "correlation_id", None)
    if correlation_id:
        return str(correlation_id)
    return str(uuid.uuid4())


def hash_string(text: str, algorithm: str = "sha256") -> str:
    """Hash a string using specified algorithm"""
    hash_obj = hashlib.new(algorithm)
    hash_obj.update(text.encode("utf-8"))
    return hash_obj.hexdigest()


def generate_cache_key(prefix: str, *args, **kwargs) -> str:
    """Generate cache key from prefix and arguments"""
    key_parts = [prefix]

    # Add positional arguments
    for arg in args:
        if isinstance(arg, (str, int, float, bool)):
            key_parts.append(str(arg))
        else:
            key_parts.append(hash_string(json.dumps(arg, sort_keys=True)))

    # Add keyword arguments (sorted for consistency)
    for k, v in sorted(kwargs.items()):
        key_parts.append(
            f"{k}:{v}"
            if isinstance(v, (str, int, float, bool))
            else f"{k}:{hash_string(json.dumps(v, sort_keys=True))}"
        )

    return ":".join(key_parts)


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
):
    """Async retry decorator with exponential backoff"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == max_attempts - 1:
                        logger.error(
                            f"Function {func.__name__} failed after {max_attempts} attempts: {e}"
                        )
                        raise

                    # Calculate delay with exponential backoff and jitter
                    delay = min(base_delay * (backoff_factor**attempt), max_delay)
                    jitter = delay * 0.1 * (hash(str(time.time())) % 10) / 10
                    total_delay = delay + jitter

                    logger.warning(
                        f"Function {func.__name__} failed (attempt {attempt + 1}/{max_attempts}), retrying in {total_delay:.2f}s: {e}"
                    )
                    await asyncio.sleep(total_delay)

            # This should never be reached
            raise last_exception

        return wrapper

    return decorator


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
    expected_exception: tuple = (Exception,),
):
    """Circuit breaker decorator"""

    def decorator(func: Callable) -> Callable:
        # Circuit breaker state
        state = {"failure_count": 0, "last_failure_time": None, "is_open": False}

        @wraps(func)
        async def wrapper(*args, **kwargs):
            now = time.time()

            # Check if circuit should be half-open
            if (
                state["is_open"]
                and state["last_failure_time"]
                and now - state["last_failure_time"] >= recovery_timeout
            ):
                state["is_open"] = False
                state["failure_count"] = 0
                logger.info(
                    f"Circuit breaker for {func.__name__} entering half-open state"
                )

            # Fail fast if circuit is open
            if state["is_open"]:
                raise Exception(f"Circuit breaker for {func.__name__} is open")

            try:
                result = await func(*args, **kwargs)

                # Reset failure count on success
                if state["failure_count"] > 0:
                    state["failure_count"] = 0
                    logger.info(
                        f"Circuit breaker for {func.__name__} reset after success"
                    )

                return result

            except expected_exception as e:
                state["failure_count"] += 1
                state["last_failure_time"] = now

                if state["failure_count"] >= failure_threshold:
                    state["is_open"] = True
                    logger.warning(
                        f"Circuit breaker for {func.__name__} opened after {state['failure_count']} failures"
                    )

                raise

        return wrapper

    return decorator


class AsyncCache:
    """Async cache wrapper using Redis"""

    def __init__(self, redis_url: str, default_ttl: int = 3600):
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self._redis = None

    async def _get_redis(self):
        """Get Redis connection"""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        try:
            redis_client = await self._get_redis()
            value = await redis_client.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Cache get error for key {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set value in cache"""
        try:
            redis_client = await self._get_redis()
            ttl = ttl or self.default_ttl
            serialized_value = json.dumps(value, default=str)
            await redis_client.setex(key, ttl, serialized_value)
            return True
        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from cache"""
        try:
            redis_client = await self._get_redis()
            await redis_client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Cache delete error for key {key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern"""
        try:
            redis_client = await self._get_redis()
            keys = await redis_client.keys(pattern)
            if keys:
                return await redis_client.delete(*keys)
            return 0
        except Exception as e:
            logger.error(f"Cache delete pattern error for pattern {pattern}: {e}")
            return 0

    async def exists(self, key: str) -> bool:
        """Check if key exists"""
        try:
            redis_client = await self._get_redis()
            return bool(await redis_client.exists(key))
        except Exception as e:
            logger.error(f"Cache exists error for key {key}: {e}")
            return False

    async def close(self):
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()


class RateLimiter:
    """Async rate limiter using Redis"""

    def __init__(self, redis_url: str, *, fail_open: bool = True):
        self.redis_url = redis_url
        self.fail_open = fail_open
        self._redis = None

    async def _get_redis(self):
        """Get Redis connection"""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def is_allowed(
        self, key: str, limit: int, window: int, identifier: Optional[str] = None
    ) -> tuple[bool, Dict[str, Any]]:
        """
        Check if request is allowed

        Args:
            key: Rate limit key (e.g., "api_requests")
            limit: Number of allowed requests
            window: Time window in seconds
            identifier: Unique identifier (e.g., IP address, user ID)

        Returns:
            Tuple of (is_allowed, info_dict)
        """
        try:
            redis_client = await self._get_redis()
            current_time = int(time.time())

            # Create full key
            full_key = (
                f"rate_limit:{key}:{identifier or 'anonymous'}:{current_time // window}"
            )

            # Get current count
            count = await redis_client.incr(full_key)

            # Set expiry on first request in window
            if count == 1:
                await redis_client.expire(full_key, window)

            remaining = max(0, limit - count)
            reset_time = (current_time // window + 1) * window

            info = {
                "limit": limit,
                "remaining": remaining,
                "reset_time": reset_time,
                "retry_after": reset_time - current_time if remaining == 0 else 0,
            }

            return count <= limit, info

        except Exception as e:
            logger.error(f"Rate limiter error: {e}")
            return self.fail_open, {
                "limit": limit,
                "remaining": limit if self.fail_open else 0,
                "reset_time": int(time.time()) + window,
            }

    async def close(self):
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()


class EventLogger:
    """Async event logger with structured logging"""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.logger = logging.getLogger(f"{service_name}.events")

    async def log_event(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Log structured event"""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service_name": self.service_name,
            "event_type": event_type,
            "event_data": event_data,
            "user_id": user_id,
            "organization_id": organization_id,
            "correlation_id": correlation_id,
            "metadata": metadata or {},
        }

        # Log as JSON for structured logging
        self.logger.info(json.dumps(event, default=str))

        # Could also send to external event store here
        # await self._send_to_event_store(event)

    async def log_error(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ):
        """Log error event"""
        event_data = {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context or {},
        }

        await self.log_event(
            event_type="error",
            event_data=event_data,
            user_id=user_id,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )


class HealthChecker:
    """Service health checker"""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.checks: Dict[str, Callable] = {}

    def add_check(self, name: str, check_func: Callable[[], bool]):
        """Add health check function"""
        self.checks[name] = check_func

    async def check_health(self) -> Dict[str, Any]:
        """Run all health checks"""
        results = {}
        overall_healthy = True

        for name, check_func in self.checks.items():
            try:
                start_time = time.time()
                is_healthy = (
                    await check_func()
                    if asyncio.iscoroutinefunction(check_func)
                    else check_func()
                )
                duration = time.time() - start_time

                results[name] = {
                    "status": "healthy" if is_healthy else "unhealthy",
                    "duration_ms": duration * 1000,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                if not is_healthy:
                    overall_healthy = False

            except Exception as e:
                results[name] = {
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                overall_healthy = False

        return {
            "service": self.service_name,
            "status": "healthy" if overall_healthy else "unhealthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": results,
        }


class MetricsCollector:
    """Simple metrics collector"""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}
        self.histograms: Dict[str, List[float]] = {}

    def increment_counter(
        self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None
    ):
        """Increment counter metric"""
        key = self._make_key(name, labels)
        self.counters[key] = self.counters.get(key, 0) + value

    def set_gauge(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ):
        """Set gauge metric"""
        key = self._make_key(name, labels)
        self.gauges[key] = value

    def record_histogram(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ):
        """Record histogram metric"""
        key = self._make_key(name, labels)
        if key not in self.histograms:
            self.histograms[key] = []
        self.histograms[key].append(value)

    def _make_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        """Create metric key with labels"""
        if not labels:
            return name

        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def get_metrics(self) -> Dict[str, Any]:
        """Get all metrics"""
        return {
            "service": self.service_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counters": self.counters.copy(),
            "gauges": self.gauges.copy(),
            "histograms": {
                key: {
                    "count": len(values),
                    "sum": sum(values),
                    "min": min(values) if values else 0,
                    "max": max(values) if values else 0,
                    "avg": sum(values) / len(values) if values else 0,
                }
                for key, values in self.histograms.items()
            },
        }


async def make_http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    retries: int = 3,
) -> Dict[str, Any]:
    """Make HTTP request with retries"""

    @retry_async(max_attempts=retries)
    async def _request():
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method=method, url=url, headers=headers, json=json_data, params=params
            )
            response.raise_for_status()
            return response.json()

    return await _request()


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for storage"""
    import re

    # Replace path separators one-for-one to preserve character count.
    filename = re.sub(r"[/\\]", "_", filename)
    # Remove or replace dangerous characters
    filename = re.sub(r'[<>:"|?*]', "_", filename)
    # Remove control characters
    filename = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", filename)
    # Limit length
    if len(filename) > 255:
        name, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        filename = name[: 255 - len(ext) - 1] + "." + ext if ext else name[:255]
    return filename


def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format"""
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024.0 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1

    return f"{size_bytes:.1f} {size_names[i]}"


def validate_mime_type(file_content: bytes, expected_mime: str) -> bool:
    """Validate file MIME type from content"""
    import magic

    try:
        detected_mime = magic.from_buffer(file_content, mime=True)
        return detected_mime == expected_mime or detected_mime.startswith(
            expected_mime.split("/")[0] + "/"
        )
    except Exception:
        # Fallback to basic validation if magic library not available
        return True


async def paginate_query(
    query: Any, page: int, limit: int, db: AsyncSession
) -> tuple[List[Any], Dict[str, int]]:
    """Paginate database query"""
    offset = (page - 1) * limit

    # Get total count
    total_query = query.statement.with_only_columns([func.count()])
    total_result = await db.execute(total_query)
    total = total_result.scalar()

    # Get paginated results
    paginated_query = query.offset(offset).limit(limit)
    results = await db.execute(paginated_query)
    items = results.scalars().all()

    pagination_info = {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": (total + limit - 1) // limit,
        "has_next": page * limit < total,
        "has_prev": page > 1,
    }

    return list(items), pagination_info
