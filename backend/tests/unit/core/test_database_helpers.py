"""Unit tests for ``src.core.database`` helpers not exercised elsewhere.

Coverage gap (daily audit #923, finding #4): the existing database test files
(``test_database_connections.py``, ``test_database_cascades.py``) use bare
SQLite / mock engines or ``app.dependency_overrides[get_db]``, so the real
``_env_int`` env-parsing fallback and ``get_db``'s session lifecycle
were never directly exercised. These tests target those two paths.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core import database
from src.core.database import _env_int, get_db

# ---------------------------------------------------------------------------
# _env_int — graceful integer env parsing (never raises at import time)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_int_unset_returns_default(monkeypatch):
    monkeypatch.delenv("NOUS_TEST_ENV_INT", raising=False)
    assert _env_int("NOUS_TEST_ENV_INT", 15) == 15


@pytest.mark.unit
def test_env_int_empty_string_returns_default(monkeypatch):
    # Empty string is the historical footgun: int("") raised ValueError at
    # module import and crashed FastAPI startup before loggers existed.
    monkeypatch.setenv("NOUS_TEST_ENV_INT", "")
    assert _env_int("NOUS_TEST_ENV_INT", 7) == 7


@pytest.mark.unit
def test_env_int_valid_value_is_parsed(monkeypatch):
    monkeypatch.setenv("NOUS_TEST_ENV_INT", "42")
    assert _env_int("NOUS_TEST_ENV_INT", 1) == 42


@pytest.mark.unit
def test_env_int_surrounding_whitespace_is_tolerated(monkeypatch):
    # int() strips surrounding whitespace, so "  10  " is valid.
    monkeypatch.setenv("NOUS_TEST_ENV_INT", "  10  ")
    assert _env_int("NOUS_TEST_ENV_INT", 1) == 10


@pytest.mark.unit
def test_env_int_negative_value_is_parsed(monkeypatch):
    monkeypatch.setenv("NOUS_TEST_ENV_INT", "-3")
    assert _env_int("NOUS_TEST_ENV_INT", 1) == -3


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["true", "3.5", "1_000x", "ten", "0x10"])
def test_env_int_non_numeric_falls_back_and_logs(monkeypatch, caplog, bad):
    monkeypatch.setenv("NOUS_TEST_ENV_INT", bad)
    with caplog.at_level("ERROR"):
        assert _env_int("NOUS_TEST_ENV_INT", 99) == 99
    # The bad value is logged so ops can spot the misconfig.
    assert "NOUS_TEST_ENV_INT" in caplog.text


# ---------------------------------------------------------------------------
# get_db — owns its own request-scoped session (dev D-01: never the
# middleware's, which closed under a still-streaming response body)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_db_opens_and_closes_its_own_session(monkeypatch):
    """get_db opens its own session via AsyncSessionLocal() and closes it on
    generator exit."""
    fake_session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)

    agen = get_db()
    yielded = await agen.__anext__()
    assert yielded is fake_session
    factory.assert_called_once()

    # Drive the generator to completion so the async-context-manager exits.
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    cm.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_async_session / get_db_session — @asynccontextmanager, so callers must
# use `async with`, never `async for` (Sentry JAVASCRIPT-NEXTJS-4A: iterating
# it raised TypeError and silently killed background arXiv ingestion).
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_async_session_is_context_manager_not_iterable(monkeypatch):
    fake_session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", MagicMock(return_value=cm))

    acm = database.get_async_session()
    assert not hasattr(acm, "__aiter__")  # `async for` over it raises TypeError
    async with acm as session:
        assert session is fake_session


@pytest.mark.unit
def test_no_misuse_of_session_context_managers():
    """Source scan for the two misuse modes of the @asynccontextmanager helpers:

    1. `async for db in get_db_session():` — TypeError at runtime (Sentry
       JAVASCRIPT-NEXTJS-4A, broke background arXiv ingestion).
    2. `Depends(get_async_session)` — FastAPI only enters *generator function*
       dependencies; wrapping the already-wrapped helper raises
       "'_AsyncGeneratorContextManager' object is not an async iterator"
       during dependency resolution, 500ing the endpoint on every request
       (found on 5 /api/v2/realtime routes). Endpoints must use get_db.
    """
    src_root = Path(database.__file__).resolve().parents[1]
    patterns = [
        re.compile(r"async\s+for\s+\w+\s+in\s+get_(?:db_session|async_session)\("),
        re.compile(r"Depends\(\s*get_(?:db_session|async_session)\s*\)"),
    ]
    offenders = [
        str(path.relative_to(src_root))
        for path in src_root.rglob("*.py")
        if any(
            p.search(path.read_text(encoding="utf-8", errors="ignore"))
            for p in patterns
        )
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# async engine pool liveness — Sentry JAVASCRIPT-NEXTJS-4W
# ---------------------------------------------------------------------------


def test_async_engine_has_pool_pre_ping_enabled():
    """The async pool must liveness-check connections before handing them out.

    Supabase's pooler closes idle server-side connections long before
    ``pool_recycle`` expires them, and QueuePool is FIFO by default, so a
    checkout preferentially returns the stalest connection in the pool. Without
    the ping, the transaction's ``BEGIN`` raises
    ``asyncpg.ConnectionDoesNotExistError`` and ``finalize_submission`` aborts
    *after* the agent already produced its answer — the run never reaches a
    terminal status and the thread's single-writer slot stays held.

    This regressed twice by accident: 77dfe97e disabled the ping inside an
    unrelated parameter-shadowing commit (compensating with
    ``pool_recycle=300``), then 92d114cd relaxed recycle to 1500 and removed the
    compensation without noticing. Hence an assertion on the flag itself rather
    than on any behaviour a stubbed engine could fake.
    """
    if database._is_sqlite:  # pragma: no cover - SQLite pools take no options
        pytest.skip("SQLite engine does not take pool options")
    assert database.async_engine.pool._pre_ping is True
