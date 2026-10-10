"""CLI-token revocation via a per-user "revoked before" timestamp.

CLI tokens are long-lived (CLI_TOKEN_EXPIRE_DAYS, default 30) and otherwise
only signature+exp validated, so a leaked token of an *active* user could not
be invalidated without deactivating the user. This module records, per user, a
Redis timestamp before which all CLI tokens are considered revoked; the auth
chokepoint (get_current_user) rejects a CLI token whose issued-at predates it.

Design: revoke-all-for-user (no per-jti tracking). Availability semantics on a
revocation-store failure are configurable (audit D7):

* MISS (store reachable, no revoked-before cutoff for the user) -> ALLOW,
  always. A plain miss must never break CLI auth platform-wide.
* ERROR (store unreachable / Redis raises / no client) -> governed by
  ``CLI_TOKEN_REVOCATION_FAIL_CLOSED``. Default False keeps the historical
  FAIL-OPEN (allow) so a Redis outage never breaks CLI auth; flip to True to
  FAIL-CLOSED (deny) so an outage or failover can no longer silently
  un-revoke every revoked CLI token. Either way the unavailability is logged
  loudly.

A Redis *flush* wipes the cutoff key and thus reads as a MISS (not an error),
so fail-closed alone cannot catch it; exposure from a lost cutoff is instead
bounded by the CLI-token max-age cap enforced at the auth chokepoint
(``get_current_user_token``) — the token ages out within the configured
lifetime rather than staying valid forever.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis

from src.core.config import settings

logger = logging.getLogger(__name__)

_KEY = "cli_revoked_before:{user_id}"
# Keep the marker at least as long as a CLI token can live, so a revoked-before
# cutoff outlives every token it must reject.
_TTL_SECONDS = settings.CLI_TOKEN_EXPIRE_DAYS * 24 * 3600

# A delayed older request must not undo a newer cutoff. All callers use the
# same atomic write, including legacy best-effort revocation routes.
_ADVANCE_CUTOFF = """
local previous = redis.call('GET', KEYS[1])
local cutoff = tonumber(ARGV[1])
if previous then
    cutoff = math.max(tonumber(previous), cutoff)
end
redis.call('SET', KEYS[1], cutoff, 'EX', ARGV[2])
return 1
"""

_client: Optional["redis.Redis"] = None


async def _get_redis() -> Optional["redis.Redis"]:
    global _client
    if _client is not None:
        return _client
    url = getattr(settings, "REDIS_URL", None)
    if not url:
        return None
    try:
        # Socket timeouts (repo's 5s standard) so a degraded/hung Redis fails
        # fast and the auth path fails OPEN instead of stalling get_current_user.
        _client = redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        return _client
    except Exception as e:  # noqa: BLE001 - fail open
        logger.warning("CLI revocation: Redis unavailable (%s)", e)
        return None


async def get_redis_client() -> Optional["redis.Redis"]:
    """Return the shared async Redis client for security coordination."""
    return await _get_redis()


def _reset_client() -> None:
    """Drop the cached client so the next call reconnects. Prevents a client
    bound to a now-dead event loop from failing every subsequent call (which
    would silently defeat revocation forever — fail-open with no recovery)."""
    global _client
    _client = None


def reset_redis_client() -> None:
    """Reset the shared Redis client after a failed security operation."""
    _reset_client()


class CliTokenRevocationUnavailable(Exception):
    """The shared store could not confirm a CLI revocation write."""


async def revoke_user_cli_tokens(
    user_id: str, *, require_success: bool = False
) -> None:
    """Revoke CLI tokens up to now; optionally require a confirmed shared write."""
    client = await _get_redis()
    if client is None:
        logger.warning("CLI revocation skipped (no Redis) for user %s", user_id)
        if require_success:
            raise CliTokenRevocationUnavailable()
        return
    try:
        # Store cutoff = next whole second so a token minted in the SAME second
        # as the revoke (iat is whole-second from create_cli_token) is still
        # caught by the `iat < cutoff` check ("revoke up to now" inclusive).
        cutoff = int(datetime.now(timezone.utc).timestamp()) + 1
        written = await client.eval(
            _ADVANCE_CUTOFF, 1, _KEY.format(user_id=user_id), cutoff, _TTL_SECONDS
        )
        if require_success and written != 1:
            raise CliTokenRevocationUnavailable()
    except Exception as e:  # noqa: BLE001 - preserve best-effort callers
        logger.warning("CLI revocation write failed for user %s: %s", user_id, e)
        _reset_client()
        if require_success:
            raise CliTokenRevocationUnavailable() from e


def _store_unavailable(user_id: str, reason: str) -> bool:
    """Decide the verdict when the revocation store is UNREACHABLE (an error,
    not a miss) and log the unavailability loudly.

    Returns the value ``is_cli_token_revoked`` should yield: ``True`` (treat as
    revoked / deny) when ``CLI_TOKEN_REVOCATION_FAIL_CLOSED`` is set, else
    ``False`` (fail-open / allow). Kept separate from a plain miss so a missing
    key never denies. (audit D7)
    """
    fail_closed = bool(getattr(settings, "CLI_TOKEN_REVOCATION_FAIL_CLOSED", False))
    if fail_closed:
        logger.error(
            "CLI revocation store unavailable (%s) for user %s; failing CLOSED "
            "(denying token) per CLI_TOKEN_REVOCATION_FAIL_CLOSED",
            reason,
            user_id,
        )
        return True
    logger.warning(
        "CLI revocation store unavailable (%s) for user %s; failing OPEN "
        "(allowing token). A revoked token would NOT be rejected while the "
        "store is down — set CLI_TOKEN_REVOCATION_FAIL_CLOSED=true to harden.",
        reason,
        user_id,
    )
    return False


async def is_cli_token_revoked(user_id: str, issued_at: Optional[datetime]) -> bool:
    """True iff the CLI token (minted at ``issued_at``) should be rejected.

    * ``issued_at is None`` -> False (nothing to compare; not revoked).
    * MISS (store reachable, no cutoff key) -> False (not revoked).
    * Token minted before the user's revoked-before cutoff -> True (revoked).
    * ERROR (no Redis client / Redis raises) -> fail-open (False) by default,
      or fail-closed (True) when ``CLI_TOKEN_REVOCATION_FAIL_CLOSED`` is set.
    """
    if issued_at is None:
        return False
    client = await _get_redis()
    if client is None:
        return _store_unavailable(user_id, "no Redis client")
    try:
        raw = await client.get(_KEY.format(user_id=user_id))
    except Exception as e:  # noqa: BLE001 - error, not a miss
        _reset_client()
        return _store_unavailable(user_id, f"read failed: {e}")
    if raw is None:
        return False  # miss: no cutoff for this user -> not revoked
    try:
        cutoff = int(raw)
    except (TypeError, ValueError) as e:
        # A corrupt cutoff value is a store fault, not a miss: treat as error.
        _reset_client()
        return _store_unavailable(user_id, f"corrupt cutoff {raw!r}: {e}")
    iat = int(issued_at.replace(tzinfo=issued_at.tzinfo or timezone.utc).timestamp())
    return iat < cutoff
