"""Request pacing shared by paper providers, with bounded retries."""

import asyncio
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_INTERVAL = {"openalex": 0.1, "semantic_scholar": 1.0, "pubmed": 0.34, "crossref": 1.0}
_next_slot: dict[str, float] = {}
_lock = threading.Lock()
_redis_backoff_until = 0.0
_RESERVE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, last + tonumber(ARGV[1]))
if slot - now > 15000 then return -1 end
redis.call('SET', KEYS[1], slot, 'PX', 60000)
return slot - now
"""


def local_slot(provider: str) -> float:
    """Reserve a slot across threads without binding locks to an asyncio loop."""
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(provider, now))
        if slot - now > 15:
            raise TimeoutError("Paper provider queue is full")
        _next_slot[provider] = slot + _INTERVAL[provider]
        return slot - now


async def wait_for_slot(provider: str) -> None:
    """Coordinate pods via Redis; degrade to local pacing during outages."""
    global _redis_backoff_until
    redis_url = os.getenv("REDIS_URL")
    delay = None
    if redis_url and time.monotonic() >= _redis_backoff_until:
        try:
            import redis.asyncio as redis

            async with redis.from_url(
                redis_url, socket_connect_timeout=1, socket_timeout=1
            ) as client:
                delay_ms = await client.eval(
                    _RESERVE,
                    1,
                    f"research:provider:{provider}",
                    int(_INTERVAL[provider] * 1000),
                )
                delay = float(delay_ms) / 1000
        except Exception:
            _redis_backoff_until = time.monotonic() + 60
            logger.warning(
                "Paper provider pacing using process-local fallback; Redis unavailable"
            )
    if delay is None:
        delay = local_slot(provider)
    if delay < 0:
        raise TimeoutError("Paper provider queue is full")
    if delay:
        await asyncio.sleep(delay)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return float(2**attempt)
    try:
        delay = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return float("inf")
    return max(0, delay) if math.isfinite(delay) else float("inf")


async def get(
    client: httpx.AsyncClient, url: str, *, provider: str, **kwargs: Any
) -> httpx.Response:
    for attempt in range(3):
        await wait_for_slot(provider)
        try:
            response = await client.get(url, **kwargs)
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(2**attempt)
            continue
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
            response.raise_for_status()
            return response
        delay = _retry_delay(response, attempt)
        if delay > 15:
            response.raise_for_status()
        await asyncio.sleep(delay)
    raise RuntimeError("Paper provider retry budget exhausted")
