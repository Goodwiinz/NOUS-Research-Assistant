"""Shared CLI cutoff under deterministically reordered real Redis writes."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import jwt
import pytest
import redis.asyncio as redis

from src.core import cli_token_revocation as ctr
from src.core import security

_REDIS_URL = os.environ.get("CLI_REVOCATION_TEST_REDIS_URL")
pytestmark = [pytest.mark.integration, pytest.mark.requires_redis]


@pytest.mark.skipif(
    not _REDIS_URL, reason="CLI_REVOCATION_TEST_REDIS_URL not configured"
)
async def test_out_of_order_revocations_never_move_cutoff_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation proof: replace math.max(tonumber(previous), cutoff) with cutoff
    # in src/core/cli_token_revocation.py:51. Run with a disposable Redis URL:
    # pytest -q tests/integration/test_cli_revocation_redis.py
    # The delayed older write must not resurrect the token minted at newer.
    first = redis.from_url(str(_REDIS_URL), decode_responses=True)
    second = redis.from_url(str(_REDIS_URL), decode_responses=True)
    user_id = f"pr1788-{uuid4().hex}"
    key = ctr._KEY.format(user_id=user_id)
    older = datetime.now(timezone.utc).replace(microsecond=0)
    newer = older + timedelta(seconds=1)
    clock = Mock(wraps=datetime)
    clock.now.side_effect = [older, newer]
    monkeypatch.setattr(ctr, "datetime", clock)
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedClient:
        async def eval(self, *args: Any) -> Any:
            entered.set()
            await release.wait()
            return await first.eval(*args)

    monkeypatch.setattr(
        ctr, "_get_redis", AsyncMock(side_effect=[DelayedClient(), second, second])
    )
    delayed = asyncio.create_task(
        ctr.revoke_user_cli_tokens(user_id, require_success=True)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await ctr.revoke_user_cli_tokens(user_id, require_success=True)
        release.set()
        await asyncio.wait_for(delayed, timeout=5)
        raw_cutoff = await second.get(key)
        assert raw_cutoff is not None
        assert int(raw_cutoff) == int(newer.timestamp()) + 1
        assert 0 < await second.ttl(key) <= ctr._TTL_SECONDS
        token_clock = Mock(wraps=datetime)
        token_clock.now.return_value = newer
        monkeypatch.setattr(security, "datetime", token_clock)
        token, _ = security.create_cli_token(user_id, "test@example.test", str(uuid4()))
        claims = jwt.decode(token, options={"verify_signature": False})
        issued_at = datetime.fromtimestamp(claims["iat"], timezone.utc)
        assert issued_at == newer
        assert await ctr.is_cli_token_revoked(user_id, issued_at)
    finally:
        release.set()
        if not delayed.done():
            delayed.cancel()
        await asyncio.gather(delayed, return_exceptions=True)
        await second.delete(key)
        await first.connection_pool.disconnect()
        await second.connection_pool.disconnect()
