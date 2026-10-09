"""CLI-token revocation: per-user revoked-before cutoff, fail-open on Redis loss."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.core import cli_token_revocation as ctr


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_issued_before_cutoff_is_revoked() -> None:
    now = datetime.now(timezone.utc)
    client = AsyncMock()
    client.get = AsyncMock(return_value=str(_epoch(now)))  # revoked-before = now
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        revoked = await ctr.is_cli_token_revoked(
            "user-1", now - timedelta(hours=1)  # issued before the cutoff
        )
    assert revoked is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_issued_after_cutoff_is_valid() -> None:
    now = datetime.now(timezone.utc)
    client = AsyncMock()
    client.get = AsyncMock(return_value=str(_epoch(now - timedelta(hours=1))))
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        revoked = await ctr.is_cli_token_revoked("user-1", now)  # issued after
    assert revoked is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_cutoff_means_not_revoked() -> None:
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)  # no revoked-before key
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc))
            is False
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_open_when_redis_unavailable() -> None:
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=None)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc))
            is False
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_open_on_redis_error() -> None:
    # Default (flag off) preserves historical fail-open on a store ERROR.
    client = AsyncMock()
    client.get = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc))
            is False
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_closed_on_redis_error_when_flag_on(monkeypatch) -> None:
    # Redis read raises -> DENY when CLI_TOKEN_REVOCATION_FAIL_CLOSED is set.
    monkeypatch.setattr(ctr.settings, "CLI_TOKEN_REVOCATION_FAIL_CLOSED", True)
    client = AsyncMock()
    client.get = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc)) is True
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_closed_when_no_client_and_flag_on(monkeypatch) -> None:
    # No Redis client at all (store unreachable) -> DENY when fail-closed.
    monkeypatch.setattr(ctr.settings, "CLI_TOKEN_REVOCATION_FAIL_CLOSED", True)
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=None)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc)) is True
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_miss_still_allows_even_when_flag_on(monkeypatch) -> None:
    # A plain MISS (store reachable, no cutoff key) must ALLOW regardless of the
    # fail-closed flag — else every CLI call breaks after a Redis flush.
    monkeypatch.setattr(ctr.settings, "CLI_TOKEN_REVOCATION_FAIL_CLOSED", True)
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc))
            is False
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_corrupt_cutoff_fails_closed_when_flag_on(monkeypatch) -> None:
    # A non-integer cutoff is a store fault (not a miss): deny when fail-closed.
    monkeypatch.setattr(ctr.settings, "CLI_TOKEN_REVOCATION_FAIL_CLOSED", True)
    client = AsyncMock()
    client.get = AsyncMock(return_value="not-an-int")
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        assert (
            await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc)) is True
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_issued_at_is_not_revoked() -> None:
    # Never call Redis if the token has no issued_at.
    get_redis = AsyncMock()
    with patch.object(ctr, "_get_redis", get_redis):
        assert await ctr.is_cli_token_revoked("user-1", None) is False
    get_redis.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revoke_writes_cutoff_with_ttl() -> None:
    client = AsyncMock()
    client.eval = AsyncMock(return_value=1)
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        await ctr.revoke_user_cli_tokens("user-1")
    client.eval.assert_awaited_once()
    args = client.eval.call_args.args
    assert args[1:3] == (1, "cli_revoked_before:user-1")
    assert args[4] == ctr._TTL_SECONDS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_revoke_remains_best_effort_on_write_failure() -> None:
    client = AsyncMock()
    client.eval.side_effect = RuntimeError("redis unavailable")
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=client)):
        await ctr.revoke_user_cli_tokens("user-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_revoke_persists_same_second_cutoff_across_clients() -> None:
    # Different API client objects share only the Redis value, not local state.
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    first, second = AsyncMock(), AsyncMock()
    first.eval.return_value = 1
    clock = type("Clock", (), {"now": staticmethod(lambda _tz: issued_at)})
    with (
        patch.object(ctr, "_get_redis", AsyncMock(return_value=first)),
        patch.object(ctr, "datetime", clock),
    ):
        await ctr.revoke_user_cli_tokens("user-1", require_success=True)
    _, _, key, cutoff, _ = first.eval.call_args.args
    assert key == "cli_revoked_before:user-1"
    assert cutoff == _epoch(issued_at) + 1
    second.get.return_value = str(cutoff)
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=second)):
        assert await ctr.is_cli_token_revoked("user-1", issued_at)
        assert not await ctr.is_cli_token_revoked(
            "user-1", issued_at + timedelta(seconds=1)
        )
        second.get.assert_awaited_with(key)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revoke_noop_without_redis() -> None:
    with patch.object(ctr, "_get_redis", AsyncMock(return_value=None)):
        await ctr.revoke_user_cli_tokens("user-1")  # must not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_client_reset_after_error() -> None:
    """A client error resets the cached client so the next call reconnects
    (prevents a loop-bound dead client from defeating revocation forever)."""
    broken = AsyncMock()
    broken.get = AsyncMock(side_effect=RuntimeError("loop closed"))
    ctr._client = broken
    try:
        revoked = await ctr.is_cli_token_revoked("user-1", datetime.now(timezone.utc))
        assert revoked is False  # fail-open
        assert ctr._client is None  # cached client dropped for reconnect
    finally:
        ctr._client = None
