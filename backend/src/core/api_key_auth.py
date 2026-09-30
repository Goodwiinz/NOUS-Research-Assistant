"""
API Key Authentication for Public Endpoints
"""

import ast
import asyncio
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.database import Base, get_db
from src.core.security import RateLimiter
from src.models.api_key import APIKey, APIKeyUsageLog  # noqa: F401 — re-export (R6-L12)

logger = logging.getLogger(__name__)

# API Key Bearer scheme
api_key_security = HTTPBearer()


class APIKeyData(BaseModel):
    """API Key data model"""

    id: str
    name: str
    key_prefix: str
    is_active: bool
    rate_limit_per_hour: int
    last_used_at: Optional[datetime]
    usage_count: int
    organization_id: Optional[str] = None


class APIKeyCreate(BaseModel):
    """API Key creation request"""

    name: str
    description: Optional[str] = None
    rate_limit_per_hour: int = 100
    expires_days: Optional[int] = None  # Days until expiration
    allowed_endpoints: Optional[list] = None


class APIKeyResponse(BaseModel):
    """API Key creation response (includes raw key)"""

    id: str
    name: str
    api_key: str  # This is shown ONLY during creation
    key_prefix: str
    rate_limit_per_hour: int
    expires_at: Optional[datetime]
    organization_id: Optional[str] = None


def hash_api_key(raw_key: str) -> str:
    """Hash the API key for secure storage and comparison"""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Generate API key and return (raw_key, hash)"""
    raw_key = f"rag_{''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(32))}"
    key_hash = hash_api_key(raw_key)
    return raw_key, key_hash


def verify_api_key(raw_key: str, key_hash: str) -> bool:
    """Verify API key against hash using constant-time comparison"""
    return secrets.compare_digest(hash_api_key(raw_key), key_hash)


def _endpoint_allowed(allowed: Optional[list[str]], path: str) -> bool:
    """Check ``path`` against a key's ``allowed_endpoints`` config.

    Semantics (I5):
    - ``None``  -> key was never scoped; every endpoint is allowed.
    - ``[]``    -> explicit empty allowlist; every endpoint is denied.
    - entries   -> each entry is an exact path or a directory prefix
      (a bare ``/x`` entry permits ``/x`` itself and ``/x/...`` but not
      ``/xy``). A trailing slash on an entry is normalized away. A
      degenerate entry (``/`` or blank) normalizes to empty and never
      matches anything (deny) — an entry must name at least one segment.
    """
    if allowed is None:
        return True
    for entry in allowed:
        norm = entry.rstrip("/")
        if not norm:
            continue
        if path == norm or path.startswith(norm + "/"):
            return True
    return False


def _parse_allowed_endpoints(
    raw: Optional[str], key_prefix: str = "unknown"
) -> Optional[list[str]]:
    """Parse the ``api_keys.allowed_endpoints`` Text column (JSON array,
    or the pre-I5 legacy Python-repr list).

    Returns ``None`` for an unscoped key (``NULL`` column). Fail CLOSED:
    malformed JSON, a non-list value, non-string entries, or blank
    entries are treated as an empty allowlist (deny all) and logged — a
    misconfigured key must never silently widen its own scope.

    ``key_prefix`` (redacted as ``<prefix>***``) identifies the affected
    key in parse-failure logs.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        # Keys created before I5 stored ``str(list)`` (a Python repr such as
        # ``"['/api/v1/search']"``). ``literal_eval`` only evaluates literals,
        # and the result still goes through the string-array checks below.
        try:
            parsed = ast.literal_eval(raw)
        except Exception:
            logger.error(
                f"Malformed allowed_endpoints JSON on API key {key_prefix}*** (deny all): {e}"
            )
            return []
    if not isinstance(parsed, list) or not all(
        isinstance(entry, str) for entry in parsed
    ):
        logger.error(
            f"Malformed allowed_endpoints on API key {key_prefix}*** "
            "(not a string array, deny all)"
        )
        return []
    if any(not entry.strip() for entry in parsed):
        logger.error(
            f"Malformed allowed_endpoints on API key {key_prefix}*** "
            "(blank entry, deny all)"
        )
        return []
    return parsed


# Rate limiter for API key endpoints
api_key_rate_limiter = RateLimiter(max_attempts=1000, window_minutes=60)


class RedisRateLimiter:
    """Redis-based rate limiter for distributed environments"""

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: Optional[redis.Redis] = None

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis connection"""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def track_usage(self, key_id: str, endpoint: str):
        """Track API key usage for rate limiting"""
        now = datetime.utcnow()
        hour_key = now.replace(minute=0, second=0, microsecond=0)

        redis_client = await self._get_redis()
        redis_key = f"rate_limit:{key_id}:{hour_key.isoformat()}"

        try:
            # Use atomic INCR operation for thread-safe counting
            current_count = await redis_client.incr(redis_key)

            # Set TTL of 1 hour only on first increment
            if current_count == 1:
                await redis_client.expire(redis_key, 3600)  # 1 hour TTL

            logger.info(
                f"API key usage tracked: key={key_id}, endpoint={endpoint}, hour={hour_key}, count={current_count}"
            )

        except Exception as e:
            logger.error(f"Failed to track usage in Redis for key {key_id}: {e}")
            # Fail gracefully - don't block API calls if Redis is down

    async def check_rate_limit(self, key_id: str, rate_limit: int) -> bool:
        """Check if API key is within rate limit"""
        now = datetime.utcnow()
        hour_key = now.replace(minute=0, second=0, microsecond=0)

        redis_client = await self._get_redis()
        redis_key = f"rate_limit:{key_id}:{hour_key.isoformat()}"

        try:
            current_usage = await redis_client.get(redis_key)
            current_usage = int(current_usage) if current_usage else 0

            return current_usage < rate_limit

        except Exception as e:
            logger.error(f"Failed to check rate limit in Redis for key {key_id}: {e}")
            # Fail open - allow request if Redis is down to avoid blocking API
            return True

    async def get_current_usage(self, key_id: str) -> int:
        """Get current hour usage for API key"""
        now = datetime.utcnow()
        hour_key = now.replace(minute=0, second=0, microsecond=0)

        redis_client = await self._get_redis()
        redis_key = f"rate_limit:{key_id}:{hour_key.isoformat()}"

        try:
            current_usage = await redis_client.get(redis_key)
            return int(current_usage) if current_usage else 0

        except Exception as e:
            logger.error(
                f"Failed to get current usage from Redis for key {key_id}: {e}"
            )
            return 0

    async def close(self):
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()


# Global Redis rate limiter instance
redis_rate_limiter = RedisRateLimiter()


class APIKeyAuth:
    """API Key authentication and rate limiting using Redis"""

    def __init__(self):
        self.rate_limiter = redis_rate_limiter

    async def track_usage(self, key_id: str, endpoint: str):
        """Track API key usage for rate limiting"""
        await self.rate_limiter.track_usage(key_id, endpoint)

    async def check_rate_limit(self, key_id: str, rate_limit: int) -> bool:
        """Check if API key is within rate limit"""
        return await self.rate_limiter.check_rate_limit(key_id, rate_limit)

    async def get_current_usage(self, key_id: str) -> int:
        """Get current hour usage for API key"""
        return await self.rate_limiter.get_current_usage(key_id)


# Global API key auth instance
api_key_auth = APIKeyAuth()


async def cleanup_api_key_auth():
    """Cleanup function for API key authentication resources"""
    await redis_rate_limiter.close()


async def get_api_key_data(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(api_key_security),
    db: AsyncSession = Depends(get_db),
) -> tuple[APIKeyData, str]:
    """
    Validate API key and return key data with endpoint tracking

    Returns:
        tuple: (APIKeyData, endpoint_path)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        raw_key = credentials.credentials
        if not raw_key or not raw_key.startswith("rag_"):
            logger.warning(f"Invalid API key format from {request.client.host}")
            raise credentials_exception

        # Extract prefix and hash the provided key
        # key_prefix stores the first 8 chars of the raw key (rag_ + 4 chars)
        key_prefix = raw_key[:8]
        target_hash = hash_api_key(raw_key)

        # Query the database directly for the exact hash, also using the prefix to be safe
        stmt = select(APIKey).where(
            APIKey.key_prefix == key_prefix,
            APIKey.key_hash == target_hash,
            APIKey.is_active == True,
        )
        result = await db.execute(stmt)
        key_record = result.scalars().first()

        api_key_record = None
        # Retain constant-time comparison to satisfy timing attack mitigation
        if key_record and verify_api_key(raw_key, key_record.key_hash):
            api_key_record = key_record

        if not api_key_record:
            logger.warning(f"API key not found or invalid from {request.client.host}")
            raise credentials_exception

        # Check if expired
        if api_key_record.expires_at and api_key_record.expires_at < datetime.utcnow():
            logger.warning(f"Expired API key used: {api_key_record.key_prefix}***")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired"
            )

        # Enforce the key's allowed_endpoints scope (I5). Placed BEFORE the
        # rate-limit check so a denied request never consumes rate-limit
        # quota, and 403 (scope denial) takes precedence over 429 for
        # keys used outside their allowlist.
        allowed_endpoints = _parse_allowed_endpoints(
            api_key_record.allowed_endpoints,
            key_prefix=api_key_record.key_prefix,
        )
        if not _endpoint_allowed(allowed_endpoints, request.url.path):
            logger.warning(
                f"API key {api_key_record.key_prefix}*** not permitted for endpoint {request.url.path}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key not permitted for this endpoint",
            )

        # Check rate limit
        if not await api_key_auth.check_rate_limit(
            api_key_record.id, api_key_record.rate_limit_per_hour
        ):
            current_usage = await api_key_auth.get_current_usage(api_key_record.id)
            logger.warning(
                f"Rate limit exceeded for API key {api_key_record.key_prefix}*** (usage: {current_usage}/{api_key_record.rate_limit_per_hour})"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Limit: {api_key_record.rate_limit_per_hour} requests per hour",
            )

        # Track usage
        endpoint = request.url.path
        await api_key_auth.track_usage(api_key_record.id, endpoint)

        # Update usage stats in database
        api_key_record.last_used_at = datetime.utcnow()
        api_key_record.usage_count += 1
        await db.commit()

        logger.info(
            f"Valid API key used: {api_key_record.name} ({api_key_record.key_prefix}***) from {request.client.host}"
        )

        return (
            APIKeyData(
                id=api_key_record.id,
                name=api_key_record.name,
                key_prefix=api_key_record.key_prefix,
                is_active=api_key_record.is_active,
                rate_limit_per_hour=api_key_record.rate_limit_per_hour,
                last_used_at=api_key_record.last_used_at,
                usage_count=api_key_record.usage_count,
                organization_id=api_key_record.organization_id,
            ),
            endpoint,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating API key: {e}")
        raise credentials_exception


# Optional API key dependency (for endpoints that can work with or without API key)
async def get_api_key_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
    db: AsyncSession = Depends(get_db),
) -> Optional[tuple[APIKeyData, str]]:
    """Optional API key validation - returns None if no key provided"""
    if not credentials:
        return None

    try:
        return await get_api_key_data(request, credentials, db)
    except HTTPException:
        # Log the attempt but don't raise exception for optional auth
        logger.warning(f"Invalid API key attempt from {request.client.host}")
        return None


def require_api_key(
    api_key_data: tuple[APIKeyData, str] = Depends(get_api_key_data),
) -> APIKeyData:
    """Require valid API key - dependency that raises exception if invalid"""
    return api_key_data[0]


def log_api_access(
    api_key_data: Optional[tuple[APIKeyData, str]],
    request: Request,
    action: str,
    details: dict = None,
):
    """Log API access for security audit"""
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    if api_key_data:
        api_key, endpoint = api_key_data
        logger.info(
            f"API Access: key={api_key.name} ({api_key.key_prefix}***), "
            f"ip={client_ip}, ua='{user_agent}', action={action}, "
            f"endpoint={endpoint}, details={details}"
        )
    else:
        logger.warning(
            f"Unauthenticated API attempt: ip={client_ip}, ua='{user_agent}', "
            f"action={action}, details={details}"
        )
