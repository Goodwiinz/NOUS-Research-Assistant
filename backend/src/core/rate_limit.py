import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import redis.asyncio as redis

from src.core.config import settings

logger = logging.getLogger(__name__)

# How often a degraded (Redis-down) rate limiter may warn.
_FALLBACK_LOG_INTERVAL_SECONDS = 60.0


class RateLimiterInterface(ABC):
    @abstractmethod
    async def is_allowed(self, identifier: str, prefix: str = "") -> bool:
        """Check if identifier is allowed to make an attempt"""
        pass

    @abstractmethod
    async def get_remaining_attempts(self, identifier: str, prefix: str = "") -> int:
        """Get remaining attempts for identifier"""
        pass

    @abstractmethod
    async def check_rate_limit(
        self, identifier: str, prefix: str = ""
    ) -> Tuple[bool, int]:
        """Read-only check: returns (allowed, retry_after_seconds)"""
        pass

    @abstractmethod
    async def record_attempt(self, identifier: str, prefix: str = "") -> None:
        """Write-only: record a failed attempt"""
        pass

    @abstractmethod
    async def close(self):
        """Cleanup resources"""
        pass


class InMemoryRateLimiter(RateLimiterInterface):
    """Simple rate limiter for authentication endpoints (In-Memory)"""

    def __init__(self, max_attempts: int = 5, window_minutes: int = 15):
        self.max_attempts = max_attempts
        self.window_minutes = window_minutes
        self.attempts: Dict[str, List[datetime]] = {}

    def _prune(self, key: str, window_start: datetime) -> List[datetime]:
        """Drop expired attempts for key; evict the key entirely once empty
        instead of leaving a dangling [] entry (R4-L16 -- self.attempts never
        shrank on its own, so every identifier that ever made one attempt
        stayed in memory for the process lifetime)."""
        if key not in self.attempts:
            return []
        pruned = [t for t in self.attempts[key] if t > window_start]
        if pruned:
            self.attempts[key] = pruned
        else:
            del self.attempts[key]
        return pruned

    async def is_allowed(self, identifier: str, prefix: str = "") -> bool:
        """Check if identifier is allowed to make an attempt"""
        key = f"{prefix}:{identifier}" if prefix else identifier
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=self.window_minutes)

        recent = self._prune(key, window_start)

        # Check if under limit
        if len(recent) >= self.max_attempts:
            return False

        # Record this attempt
        recent.append(now)
        self.attempts[key] = recent
        return True

    async def check_rate_limit(
        self, identifier: str, prefix: str = ""
    ) -> Tuple[bool, int]:
        """Read-only check: returns (allowed, retry_after_seconds). Must not
        materialize an entry for a key that has never been seen."""
        key = f"{prefix}:{identifier}" if prefix else identifier
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=self.window_minutes)

        recent = self._prune(key, window_start)

        if len(recent) >= self.max_attempts:
            oldest = recent[0]
            retry_after = int(
                (oldest + timedelta(minutes=self.window_minutes) - now).total_seconds()
            )
            return False, max(1, retry_after)

        return True, 0

    async def record_attempt(self, identifier: str, prefix: str = "") -> None:
        """Write-only: record a failed attempt"""
        key = f"{prefix}:{identifier}" if prefix else identifier
        now = datetime.now(timezone.utc)
        if key not in self.attempts:
            self.attempts[key] = []
        self.attempts[key].append(now)

    async def get_remaining_attempts(self, identifier: str, prefix: str = "") -> int:
        """Get remaining attempts for identifier"""
        key = f"{prefix}:{identifier}" if prefix else identifier
        if key not in self.attempts:
            return self.max_attempts

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=self.window_minutes)

        # Count recent attempts
        recent_attempts = [
            attempt_time
            for attempt_time in self.attempts[key]
            if attempt_time > window_start
        ]

        return max(0, self.max_attempts - len(recent_attempts))

    async def close(self):
        self.attempts.clear()


class RedisRateLimiter(RateLimiterInterface):
    """Redis-based rate limiter for distributed environments"""

    def __init__(self, max_attempts: int, window_minutes: int, redis_url: str = None):
        self.max_attempts = max_attempts
        self.window_minutes = window_minutes
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: Optional[redis.Redis] = None
        # R7-M7: a Redis outage used to fail OPEN (every request allowed).
        # Degrade to the per-process limiter instead — weaker than the shared
        # counter across workers, but still a bound.
        self._fallback = InMemoryRateLimiter(max_attempts, window_minutes)
        self._last_fallback_log = 0.0
        # Monotonic deadline until which every call skips Redis. See
        # ``_in_fallback_window``.
        self._fallback_until = 0.0
        # Lua script to atomically increment and set expire only on first use
        self._incr_expire_script = """
        local current = redis.call("INCR", KEYS[1])
        if current == 1 then
            redis.call("EXPIRE", KEYS[1], ARGV[1])
        end
        return current
        """
        # Lua script for read-only check: returns {count, ttl}
        self._check_script = """
        local current = redis.call("GET", KEYS[1])
        if current == false then
            return {0, 0}
        end
        local ttl = redis.call("TTL", KEYS[1])
        return {tonumber(current), ttl}
        """
        # Lua script for write-only record: INCR + EXPIRE on first
        self._record_script = """
        local current = redis.call("INCR", KEYS[1])
        if current == 1 then
            redis.call("EXPIRE", KEYS[1], ARGV[1])
        end
        return current
        """

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis connection"""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _in_fallback_window(self) -> bool:
        """True while a recent Redis error pins this process to ``_fallback``.

        Attempts made during an outage land only in the in-memory store. If
        Redis comes back mid-window the Redis counter knows nothing about
        them, so the caller gets a second full quota — and flapping splits a
        single window across two stores. Sticking to one store for the rest of
        the window keeps every window's count whole.
        """
        return time.monotonic() < self._fallback_until

    def _log_degraded(self, exc: Exception) -> None:
        """Warn at most once per minute — an outage must not flood the log."""
        now = time.monotonic()
        self._fallback_until = now + self.window_minutes * 60
        if now - self._last_fallback_log >= _FALLBACK_LOG_INTERVAL_SECONDS:
            self._last_fallback_log = now
            logger.warning(
                "Redis rate limit unavailable (%s); enforcing per-process "
                "in-memory limits instead",
                exc,
            )

    async def is_allowed(self, identifier: str, prefix: str = "") -> bool:
        """Check if identifier is allowed to make an attempt"""
        if self._in_fallback_window():
            return await self._fallback.is_allowed(identifier, prefix)
        try:
            client = await self._get_redis()
            key = (
                f"auth_rate_limit:{prefix}:{identifier}"
                if prefix
                else f"auth_rate_limit:{identifier}"
            )

            # Execute Lua script for atomicity and correct expiration behavior
            # ARGV[1] is expiration in seconds
            current = await client.eval(
                self._incr_expire_script, 1, key, self.window_minutes * 60
            )

            return int(current) <= self.max_attempts

        except Exception as e:
            self._log_degraded(e)
            return await self._fallback.is_allowed(identifier, prefix)

    async def check_rate_limit(
        self, identifier: str, prefix: str = ""
    ) -> Tuple[bool, int]:
        """Read-only check: returns (allowed, retry_after_seconds)"""
        if self._in_fallback_window():
            return await self._fallback.check_rate_limit(identifier, prefix)
        try:
            client = await self._get_redis()
            key = (
                f"auth_rate_limit:{prefix}:{identifier}"
                if prefix
                else f"auth_rate_limit:{identifier}"
            )

            # Execute Lua check script (GET + TTL, no writes)
            result = await client.eval(self._check_script, 1, key)
            count, ttl = int(result[0]), int(result[1])

            if count >= self.max_attempts:
                return False, max(1, ttl)

            return True, 0

        except Exception as e:
            self._log_degraded(e)
            return await self._fallback.check_rate_limit(identifier, prefix)

    async def record_attempt(self, identifier: str, prefix: str = "") -> None:
        """Write-only: record a failed attempt"""
        if self._in_fallback_window():
            await self._fallback.record_attempt(identifier, prefix)
            return
        try:
            client = await self._get_redis()
            key = (
                f"auth_rate_limit:{prefix}:{identifier}"
                if prefix
                else f"auth_rate_limit:{identifier}"
            )

            # Execute Lua record script (INCR + EXPIRE)
            await client.eval(
                self._record_script,
                1,
                key,
                self.window_minutes * 60,
            )

        except Exception as e:
            self._log_degraded(e)
            await self._fallback.record_attempt(identifier, prefix)

    async def get_remaining_attempts(self, identifier: str, prefix: str = "") -> int:
        """Get remaining attempts for identifier"""
        if self._in_fallback_window():
            return await self._fallback.get_remaining_attempts(identifier, prefix)
        try:
            client = await self._get_redis()
            key = (
                f"auth_rate_limit:{prefix}:{identifier}"
                if prefix
                else f"auth_rate_limit:{identifier}"
            )

            current = await client.get(key)
            if current is None:
                return self.max_attempts

            return max(0, self.max_attempts - int(current))

        except Exception as e:
            self._log_degraded(e)
            return await self._fallback.get_remaining_attempts(identifier, prefix)

    async def close(self):
        """Close Redis connection"""
        if self._redis:
            await self._redis.close()
            self._redis = None


def create_rate_limiter(max_attempts: int, window_minutes: int) -> RateLimiterInterface:
    """Factory to create appropriate rate limiter"""
    # If REDIS_URL is configured (and not explicitly disabled), use Redis
    if settings.REDIS_URL:
        # We assume Redis is available if URL is set.
        # RedisRateLimiter degrades to a per-process in-memory limiter when
        # Redis is unreachable (R7-M7) rather than allowing every request.
        logger.info(f"Initializing RedisRateLimiter with URL: {settings.REDIS_URL}")
        return RedisRateLimiter(max_attempts, window_minutes)

    logger.warning(
        "REDIS_URL not set. Using InMemoryRateLimiter — limits are PER PROCESS, "
        "so a multi-worker deployment enforces N x the configured budget."
    )
    return InMemoryRateLimiter(max_attempts, window_minutes)
