"""Shared admission budget for externally expensive background work.

The shared key includes the verified organization.  The same limiter is used
by research runs and direct ArXiv jobs so clients cannot evade the aggregate
budget by switching endpoints or actors.
"""

import threading
import time
from typing import Any, Optional

from fastapi import Depends, HTTPException, status

from src.core.config import get_settings
from src.core.dependencies import get_current_user
from src.models.user import User
from src.shared.utils import RateLimiter

EXPENSIVE_WORK_RATE_KEY = "research_expensive_work"
EXPENSIVE_WORK_LIMIT = 5
EXPENSIVE_WORK_WINDOW_SECONDS = 60

_limiter = RateLimiter(get_settings().REDIS_URL, fail_open=False)
_local_lock = threading.Lock()
_local_buckets: dict[str, tuple[int, int]] = {}


def _identity_part(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _local_admission(identifier: str, now: int) -> bool:
    """Apply a bounded per-process prefilter before shared admission.

    Redis remains the authoritative cross-worker decision. If it is
    unavailable, the fail-closed shared limiter rejects the operation.
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


async def require_expensive_work_admission(
    current_user: User = Depends(get_current_user),
) -> User:
    """Route dependency: consume one shared org-budget slot or reject.

    GOO-289: sibling arXiv routes (KG bulk ingest, feature extraction, local
    PDF extraction, change tracking) share the budget of ``/ingest`` so the
    aggregate limit cannot be bypassed by switching endpoint.
    """
    organization_id = getattr(current_user, "organization_id", None)
    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization associated with this account",
        )
    if not await admit_expensive_work(
        user_id=current_user.id, organization_id=organization_id
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many expensive research jobs; retry later",
        )
    return current_user
