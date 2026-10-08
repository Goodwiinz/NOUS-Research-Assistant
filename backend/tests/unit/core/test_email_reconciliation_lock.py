from __future__ import annotations

import asyncio

import pytest

from src.core import cli_token_revocation
from src.services.security.auth_service import (
    _CHECK_EMAIL_LOCK_SCRIPT,
    _RELEASE_EMAIL_LOCK_SCRIPT,
    _RENEW_EMAIL_LOCK_SCRIPT,
    _hold_shared_email_lock,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.renewals = 0

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        assert nx is True
        assert ex > 0
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(
        self, _script: str, count: int, key: str, token: str, _ttl: int | None = None
    ) -> int:
        assert count == 1
        if self.values.get(key) != token:
            return 0
        if _script == _RENEW_EMAIL_LOCK_SCRIPT:
            self.renewals += 1
            return 1
        if _script == _CHECK_EMAIL_LOCK_SCRIPT:
            return 1
        assert _script == _RELEASE_EMAIL_LOCK_SCRIPT
        del self.values[key]
        return 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_lock_serializes_same_subject_across_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()

    async def get_redis_client() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cli_token_revocation, "get_redis_client", get_redis_client)
    second_entered = asyncio.Event()

    async def second_worker() -> None:
        async with _hold_shared_email_lock("subject-1") as acquired:
            assert acquired
            second_entered.set()

    first = _hold_shared_email_lock("subject-1")
    assert await first.__aenter__()
    waiter = asyncio.create_task(second_worker())
    await asyncio.sleep(0.1)
    assert not second_entered.is_set()

    await first.__aexit__(None, None, None)
    await asyncio.wait_for(waiter, timeout=1)
    assert second_entered.is_set()
    assert redis.values == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lease_ownership_check_rejects_a_successor_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()

    async def get_redis_client() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cli_token_revocation, "get_redis_client", get_redis_client)
    async with _hold_shared_email_lock("subject-1") as lease:
        assert lease is not None
        key = next(iter(redis.values))
        redis.values[key] = "successor-owner"
        assert not await lease.still_owned()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_lease_renews_before_expiration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()

    async def get_redis_client() -> _FakeRedis:
        return redis

    monkeypatch.setattr(cli_token_revocation, "get_redis_client", get_redis_client)
    monkeypatch.setattr("src.services.security.auth_service._EMAIL_LOCK_TTL_SECONDS", 1)
    async with _hold_shared_email_lock("subject-1") as lease:
        assert lease is not None
        await asyncio.sleep(0.4)
        assert redis.renewals >= 1
