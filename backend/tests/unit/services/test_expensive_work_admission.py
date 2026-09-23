"""Focused tests for the shared paid-work admission failure policy."""

import pytest

from src.services import expensive_work_admission as admission
from src.shared.utils import RateLimiter


class _UnavailableRedis:
    async def incr(self, _key: str) -> int:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_general_rate_limiter_remains_fail_open_on_redis_error() -> None:
    limiter = RateLimiter("redis://unused")
    limiter._redis = _UnavailableRedis()

    allowed, _info = await limiter.is_allowed("ordinary", 5, 60, "actor")

    assert allowed is True


@pytest.mark.asyncio
async def test_expensive_work_denies_when_shared_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admission, "_local_buckets", {})
    monkeypatch.setattr(admission.time, "time", lambda: 120)
    monkeypatch.setattr(admission._limiter, "_redis", _UnavailableRedis())

    allowed = await admission.admit_expensive_work(
        user_id="user-1",
        organization_id="org-1",
    )

    assert allowed is False
