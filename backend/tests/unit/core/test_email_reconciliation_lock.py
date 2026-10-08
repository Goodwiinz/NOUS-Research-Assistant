from __future__ import annotations

import asyncio

import pytest

from src.core import cli_token_revocation
from src.services.security.auth_service import _hold_shared_email_lock


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        assert nx is True
        assert ex > 0
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, _script: str, count: int, key: str, token: str) -> int:
        assert count == 1
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_lock_serializes_same_subject_across_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()

    async def get_redis_client():
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
