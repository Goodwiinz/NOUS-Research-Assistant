"""Shared admission budget for externally expensive background work.

The shared key includes the verified organization.  The same limiter is used
by research runs and direct ArXiv jobs so clients cannot evade the aggregate
budget by switching endpoints or actors.
"""

import threading
import time
from typing import Any, Optional

from src.core.config import get_settings
from src.shared.utils import RateLimiter

EXPENSIVE_WORK_RATE_KEY = "research_expensive_work"
EXPENSIVE_WORK_LIMIT = 5
EXPENSIVE_WORK_WINDOW_SECONDS = 60

_limiter = RateLimiter(get_settings().REDIS_URL)
_local_lock = threading.Lock()
_local_buckets: dict[str, tuple[int, int]] = {}


def _identity_part(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _local_admission(identifier: str, now: int) -> bool:
    """Keep a bounded per-process fallback when Redis is unavailable.

    Redis remains the shared source across workers; this fallback prevents an
    outage from turning an expensive endpoint into an unlimited sink within a
    single process.
    """
    window = now // EXPENSIVE_WORK_WINDOW_SECONDS
    with _local_lock:
        count, bucket = _local_buckets.get(identifier, (0, window))
        if bucket != window:
            count = 0
        if count >= EXPENSIVE_WORK_LIMIT:
            _local_buckets[identifier] = (count, window)
            return False
        _local_buckets[identifier] = (count + 1, window)
        return True


async def admit_expensive_work(*, user_id: Any, organization_id: Any) -> bool:
    """Admit one expensive operation for a verified actor and organization."""
    organization = _identity_part(organization_id)
    user = _identity_part(user_id)
    if not organization or not user:
        return False

    # Keep the shared bucket organization-keyed so multiple users cannot
    # multiply the paid-work allowance by distributing requests across actors.
    identifier = f"org:{organization}"
    now = int(time.time())
    if not _local_admission(identifier, now):
        return False

    allowed, _info = await _limiter.is_allowed(
        EXPENSIVE_WORK_RATE_KEY,
        EXPENSIVE_WORK_LIMIT,
        EXPENSIVE_WORK_WINDOW_SECONDS,
        identifier=identifier,
    )
    return bool(allowed)
