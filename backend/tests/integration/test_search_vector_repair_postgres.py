"""Q-P3: ``sv01_search_vector_repair`` restores thread/message full-text search
from every drift state, and an Alembic-built schema serves the search routes.

On the live dev database every full-text route (``/api/v2/search/threads``,
``/messages``, ``/combined``) answered 500 while ``/api/v2/search/health``
reported ``search_functional: false``: ``threads.search_vector`` no longer
worked as a tsvector although ``alembic_version`` sat at head. The objects
belong to ``b2c3d4e5f6g7_add_fulltext_search_for_threads``; the repair
revision recreates them idempotently.

Each case builds a scratch *database* (not a schema: the chain hard-codes
``public`` in places) with ``alembic upgrade`` to the repair's parent, seeds
the two-account chat chains so the triggers have rows to fill, breaks the
schema the way a restore could have, runs ``alembic upgrade head`` through
the real ``env.py`` and checks the health probe, the GIN indexes, the
triggers and the backfill. The correct-schema case proves the no-op: every
row keeps its ``xmin`` and a hand-set vector survives.

The last test is the guard: the chat search routes and the health endpoint
against the migration-built schema rather than ``create_all``, so a chain
that stops producing these objects fails here before it reaches a database.

Uses the two-account PostgreSQL lane: skips unless
``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` names a disposable server whose user may
``CREATE DATABASE`` (CI's ``test`` superuser; the step rejects a skipped run).
Every scratch database is dropped in a ``finally``.

Mutation-verified (2026-10-09, PostgreSQL 14): the NULL-only backfill guard,
the two ``WHERE search_vector IS NULL`` clauses in
``backend/alembic/versions/sv01_search_vector_repair.py``, was removed;
``test_upgrade_head_leaves_a_correct_schema_untouched`` and
``test_upgrade_head_is_idempotent_after_a_repair`` then failed on the
advanced ``xmin`` of every thread, and passed again once it was restored
with ``git diff`` empty. Focused command:
``TWO_ACCOUNT_PG_TEST_DATABASE_URL=... pytest
backend/tests/integration/test_search_vector_repair_postgres.py
-c backend/pytest.ini -m integration -k "untouched or idempotent"``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Tuple, cast

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.api.threads.thread_search import search_health_check
from src.models import ChatMessage, MessageRole, Thread, User
from tests.integration.two_account.conftest import (
    _authenticated_clients,
    pg_url,
    seed_accounts,
    sid,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "rp01_thread_fk_ondelete"
REVISION = "sv01_search_vector_repair"
# Supabase's default; pinned so plainto_tsquery() stems like the 'english'
# builders whatever locale the server was initialised with.
TEXT_SEARCH_CONFIG = "pg_catalog.english"
QUERY = "ISO-CANARY"  # matches every seeded title and message

# The objects b2c3d4e5f6g7 creates on the two tables, by name.
GIN_INDEXES = {
    "idx_threads_search_vector",
    "idx_threads_title_gin",
    "idx_chat_messages_search_vector",
    "idx_chat_messages_content_gin",
}
TRIGGERS = {
    "trigger_update_thread_search_vector",
    "trigger_update_chat_message_search_vector",
}
FUNCTIONS = {"update_thread_search_vector", "update_chat_message_search_vector"}
# The exact builders the triggers and the backfill use.
THREAD_VECTOR = (
    "setweight(to_tsvector('english', COALESCE(title, '')), 'A') || "
    "setweight(to_tsvector('english', COALESCE(summary, '')), 'B')"
)
MESSAGE_VECTOR = "to_tsvector('english', COALESCE(content, ''))"

_DROP_TRIGGERS_AND_FUNCTIONS = [
    "DROP TRIGGER IF EXISTS trigger_update_thread_search_vector ON threads",
    "DROP TRIGGER IF EXISTS trigger_update_chat_message_search_vector "
    "ON chat_messages",
    "DROP FUNCTION IF EXISTS update_thread_search_vector()",
    "DROP FUNCTION IF EXISTS update_chat_message_search_vector()",
]
_DROP_EXPRESSION_INDEXES = [
    "DROP INDEX IF EXISTS idx_threads_title_gin",
    "DROP INDEX IF EXISTS idx_chat_messages_content_gin",
]
# Dropping the column takes its GIN index with it.
_DROP_COLUMNS = [
    "ALTER TABLE threads DROP COLUMN search_vector",
    "ALTER TABLE chat_messages DROP COLUMN search_vector",
]
DRIFT: Dict[str, List[str]] = {
    # (a) a restore that lost the column, its triggers and its indexes
    "column_dropped": [
        *_DROP_TRIGGERS_AND_FUNCTIONS,
        *_DROP_EXPRESSION_INDEXES,
        *_DROP_COLUMNS,
    ],
    # (b) the column came back as text (no trigger can fill it, no GIN can index it)
    "column_is_text": [
        *_DROP_TRIGGERS_AND_FUNCTIONS,
        *_DROP_EXPRESSION_INDEXES,
        *_DROP_COLUMNS,
        "ALTER TABLE threads ADD COLUMN search_vector text",
        "ALTER TABLE chat_messages ADD COLUMN search_vector text",
        "UPDATE threads SET search_vector = 'stale'",
        "UPDATE chat_messages SET search_vector = 'stale'",
    ],
    # the column is right but nothing maintains or indexes it
    "triggers_and_indexes_gone": [
        *_DROP_TRIGGERS_AND_FUNCTIONS,
        *_DROP_EXPRESSION_INDEXES,
        "DROP INDEX IF EXISTS idx_threads_search_vector",
        "DROP INDEX IF EXISTS idx_chat_messages_search_vector",
        "UPDATE threads SET search_vector = NULL",
        "UPDATE chat_messages SET search_vector = NULL",
    ],
    # (c) nothing wrong: the migration must change nothing
    "correct": [],
}

CHAT_SEARCHES: List[Tuple[str, str, Dict[str, Any], str, str]] = [
    (
        "post",
        "/api/v2/search/threads",
        {"json": {"query": QUERY}},
        "thread_id",
        "thread",
    ),
    (
        "get",
        "/api/v2/search/threads",
        {"params": {"query": QUERY}},
        "thread_id",
        "thread",
    ),
    (
        "post",
        "/api/v2/search/messages",
        {"json": {"query": QUERY}},
        "message_id",
        "msg",
    ),
    (
        "get",
        "/api/v2/search/messages",
        {"params": {"query": QUERY}},
        "message_id",
        "msg",
    ),
]


# --- scratch databases -------------------------------------------------------


def _admin_engine() -> Engine:
    return create_engine(
        pg_url("psycopg2"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )


def _create_database(name: str, template: str | None = None) -> None:
    clause = f' TEMPLATE "{template}"' if template else ""
    admin = _admin_engine()
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"{clause}')
    finally:
        admin.dispose()


def _drop_database(name: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


def _alembic(database: str, *alembic_args: str) -> str:
    """Run the real ``alembic`` CLI (``env.py`` and all) against ``database``."""
    url = make_url(pg_url("psycopg2")).set(drivername="postgresql", database=database)
    # env.py prefers SUPABASE_DB_URL: it must not point the run elsewhere.
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    env["DATABASE_URL"] = url.render_as_string(hide_password=False)
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return done.stderr


@dataclass(frozen=True)
class Scratch:
    name: str
    sync: Engine
    async_: AsyncEngine

    def session(self) -> Session:
        return sessionmaker(self.sync, autocommit=False, autoflush=False)()

    def execute(self, *statements: str) -> None:
        with self.sync.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    def scalar(self, statement: str) -> Any:
        with self.sync.connect() as connection:
            return connection.exec_driver_sql(statement).scalar()

    def rows(self, statement: str) -> List[Tuple[Any, ...]]:
        with self.sync.connect() as connection:
            return [tuple(row) for row in connection.exec_driver_sql(statement)]


@pytest.fixture(scope="module")
def parent_database() -> Iterator[str]:
    """One database at the repair's parent revision; each case clones it."""
    name = f"svr_parent_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _alembic(name, "upgrade", PARENT)
        yield name
    finally:
        _drop_database(name)


@pytest_asyncio.fixture
async def scratch(parent_database: str) -> AsyncIterator[Scratch]:
    name = f"svr_case_{uuid.uuid4().hex[:12]}"
    _create_database(name, template=parent_database)
    sync = create_engine(
        make_url(pg_url("psycopg2")).set(database=name),
        poolclass=NullPool,
        connect_args={"options": f"-cdefault_text_search_config={TEXT_SEARCH_CONFIG}"},
    )
    async_ = create_async_engine(
        make_url(pg_url("asyncpg")).set(database=name),
        poolclass=NullPool,
        connect_args={
            "server_settings": {"default_text_search_config": TEXT_SEARCH_CONFIG}
        },
    )
    try:
        yield Scratch(name=name, sync=sync, async_=async_)
    finally:
        await async_.dispose()
        sync.dispose()
        _drop_database(name)


async def _seed(scratch: Scratch, tmp_path: Path) -> None:
    factory = async_sessionmaker(scratch.async_, expire_on_commit=False)
    async with factory() as db:
        await seed_accounts(db, tmp_path)


# --- observations ------------------------------------------------------------


def _health(scratch: Scratch) -> Dict[str, Any]:
    """The real ``GET /api/v2/search/health`` handler on a fresh session."""
    with scratch.session() as db:
        body = search_health_check(db=db, current_user=cast(User, None))
    return cast(Dict[str, Any], body)


def _column_type(scratch: Scratch, table: str) -> str | None:
    return cast(
        "str | None",
        scratch.scalar(
            "SELECT udt_name FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}' "
            "AND column_name = 'search_vector'"
        ),
    )


def _gin_indexes(scratch: Scratch) -> set[str]:
    return {
        name
        for (name,) in scratch.rows(
            "SELECT c.relname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_am am ON am.oid = c.relam "
            "WHERE t.relname IN ('threads', 'chat_messages') "
            "AND am.amname = 'gin' AND i.indisvalid"
        )
    }


def _triggers(scratch: Scratch) -> set[str]:
    return {
        name
        for (name,) in scratch.rows(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid IN ('threads'::regclass, 'chat_messages'::regclass)"
        )
    }


def _functions(scratch: Scratch) -> set[str]:
    return {
        name
        for (name,) in scratch.rows(
            "SELECT proname FROM pg_proc WHERE proname IN "
            "('update_thread_search_vector', 'update_chat_message_search_vector')"
        )
    }


def _unfilled(scratch: Scratch, table: str, builder: str) -> int:
    """Rows whose vector is not what the builder would produce."""
    return cast(
        int,
        scratch.scalar(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE search_vector IS DISTINCT FROM ({builder})"
        ),
    )


def _xmins(scratch: Scratch, table: str) -> Dict[str, str]:
    return {
        str(row_id): str(xmin)
        for row_id, xmin in scratch.rows(f"SELECT id, xmin::text FROM {table}")
    }


def _assert_triggers_fill_new_rows(scratch: Scratch) -> None:
    """INSERT and UPDATE through the ORM leave the builders' vectors behind."""
    thread_id, message_id = uuid.uuid4(), uuid.uuid4()
    with scratch.session() as db:
        db.add(
            Thread(
                id=thread_id,
                title="trigger insert kangaroo",
                conversation_id=sid("a-conv"),
                created_by_id=sid("user-a"),
            )
        )
        db.commit()
        db.add(
            ChatMessage(
                id=message_id,
                thread_id=thread_id,
                user_id=sid("user-a"),
                role=MessageRole.USER,
                content="trigger insert wallaby",
            )
        )
        db.commit()
    assert scratch.scalar(
        f"SELECT search_vector @@ plainto_tsquery('english', 'kangaroo') "
        f"FROM threads WHERE id = '{thread_id}'"
    )
    assert scratch.scalar(
        f"SELECT search_vector @@ plainto_tsquery('english', 'wallaby') "
        f"FROM chat_messages WHERE id = '{message_id}'"
    )
    scratch.execute(
        f"UPDATE threads SET title = 'trigger update platypus' WHERE id = '{thread_id}'",
        "UPDATE chat_messages SET content = 'trigger update echidna' "
        f"WHERE id = '{message_id}'",
    )
    assert scratch.scalar(
        f"SELECT search_vector @@ plainto_tsquery('english', 'platypus') "
        f"AND NOT search_vector @@ plainto_tsquery('english', 'kangaroo') "
        f"FROM threads WHERE id = '{thread_id}'"
    )
    assert scratch.scalar(
        f"SELECT search_vector @@ plainto_tsquery('english', 'echidna') "
        f"FROM chat_messages WHERE id = '{message_id}'"
    )
    assert _unfilled(scratch, "threads", THREAD_VECTOR) == 0
    assert _unfilled(scratch, "chat_messages", MESSAGE_VECTOR) == 0


# --- the repair --------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(DRIFT))
async def test_upgrade_head_restores_full_text_search(
    scratch: Scratch, tmp_path: Path, state: str
) -> None:
    await _seed(scratch, tmp_path)
    assert scratch.scalar("SELECT COUNT(*) FROM threads") == 3
    assert scratch.scalar("SELECT COUNT(*) FROM chat_messages") == 3
    scratch.execute(*DRIFT[state])

    broken = _health(scratch)
    if state == "column_dropped":
        # the live symptom: the probe statement itself fails
        assert broken["search_functional"] is False, broken
    if state == "column_is_text":
        # PostgreSQL defines text @@ tsquery (implicit to_tsvector), so the
        # probe survives this state; the column type is the evidence.
        assert _column_type(scratch, "threads") == "text"
        assert _column_type(scratch, "chat_messages") == "text"
    if state != "correct":
        assert not _gin_indexes(scratch) & {
            "idx_threads_search_vector",
            "idx_chat_messages_search_vector",
        }
        assert not _triggers(scratch) & TRIGGERS

    _alembic(scratch.name, "upgrade", "head")
    assert scratch.scalar("SELECT version_num FROM alembic_version") == REVISION

    assert _column_type(scratch, "threads") == "tsvector"
    assert _column_type(scratch, "chat_messages") == "tsvector"
    assert _gin_indexes(scratch) >= GIN_INDEXES
    assert _triggers(scratch) >= TRIGGERS
    assert _functions(scratch) >= FUNCTIONS
    assert (
        scratch.scalar("SELECT COUNT(*) FROM threads WHERE search_vector IS NULL") == 0
    )
    assert (
        scratch.scalar("SELECT COUNT(*) FROM chat_messages WHERE search_vector IS NULL")
        == 0
    )
    assert _unfilled(scratch, "threads", THREAD_VECTOR) == 0
    assert _unfilled(scratch, "chat_messages", MESSAGE_VECTOR) == 0

    repaired = _health(scratch)
    assert repaired["search_functional"] is True, repaired
    assert repaired["status"] == "healthy"
    assert {index["name"] for index in repaired["gin_indexes"]} >= GIN_INDEXES
    assert repaired["index_count"] >= len(GIN_INDEXES)
    # 'canary' is a stem of every seeded title (the API's own query sanitiser
    # handles the hyphenated QUERY form; the guard test below proves that).
    assert (
        scratch.scalar(
            "SELECT COUNT(*) FROM threads "
            "WHERE search_vector @@ plainto_tsquery('english', 'canary')"
        )
        == 3
    )
    assert (
        scratch.scalar(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE search_vector @@ plainto_tsquery('english', 'canary')"
        )
        == 3
    )

    _assert_triggers_fill_new_rows(scratch)


async def test_upgrade_head_leaves_a_correct_schema_untouched(
    scratch: Scratch, tmp_path: Path
) -> None:
    """A correct schema is a no-op: no row is rewritten and a vector that is
    not what the builder would produce is kept (the backfill is NULL-only)."""
    await _seed(scratch, tmp_path)
    sentinel_thread, sentinel_message = sid("a-thread"), sid("a-msg")
    scratch.execute(
        "UPDATE threads SET search_vector = to_tsvector('simple', 'sentinel') "
        f"WHERE id = '{sentinel_thread}'",
        "UPDATE chat_messages SET search_vector = to_tsvector('simple', 'sentinel') "
        f"WHERE id = '{sentinel_message}'",
    )
    thread_xmins = _xmins(scratch, "threads")
    message_xmins = _xmins(scratch, "chat_messages")
    objects_before = (_gin_indexes(scratch), _triggers(scratch), _functions(scratch))

    _alembic(scratch.name, "upgrade", "head")
    assert scratch.scalar("SELECT version_num FROM alembic_version") == REVISION

    assert _xmins(scratch, "threads") == thread_xmins
    assert _xmins(scratch, "chat_messages") == message_xmins
    assert scratch.scalar(
        "SELECT search_vector = to_tsvector('simple', 'sentinel') "
        f"FROM threads WHERE id = '{sentinel_thread}'"
    )
    assert scratch.scalar(
        "SELECT search_vector = to_tsvector('simple', 'sentinel') "
        f"FROM chat_messages WHERE id = '{sentinel_message}'"
    )
    assert (_gin_indexes(scratch), _triggers(scratch), _functions(scratch)) == (
        objects_before
    )
    assert _health(scratch)["search_functional"] is True


async def test_upgrade_head_is_idempotent_after_a_repair(
    scratch: Scratch, tmp_path: Path
) -> None:
    """Re-running the chain (a re-deploy, ``alembic stamp`` + upgrade) after
    the repair rewrites nothing."""
    await _seed(scratch, tmp_path)
    scratch.execute(*DRIFT["column_dropped"])
    _alembic(scratch.name, "upgrade", "head")
    thread_xmins = _xmins(scratch, "threads")
    _alembic(scratch.name, "downgrade", PARENT)  # a no-op that only re-stamps
    assert _column_type(scratch, "threads") == "tsvector"
    _alembic(scratch.name, "upgrade", "head")
    assert _xmins(scratch, "threads") == thread_xmins
    assert _health(scratch)["search_functional"] is True


# --- the guard ---------------------------------------------------------------


async def test_alembic_built_schema_serves_chat_search_and_health(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The search routes and the health endpoint on a schema the migration
    chain built, not ``create_all``: the state a deploy really produces."""
    _alembic(scratch.name, "upgrade", "head")
    await _seed(scratch, tmp_path)
    async with _authenticated_clients(
        async_sessionmaker(scratch.async_, expire_on_commit=False),
        monkeypatch,
        sync_factory=sessionmaker(scratch.sync, autocommit=False, autoflush=False),
    ) as clients:
        client: AsyncClient = clients("A")
        for method, url, kwargs, field, kind in CHAT_SEARCHES:
            r = await getattr(client, method)(url, **kwargs)
            assert r.status_code == 200, (method, url, r.text)
            body = r.json()
            assert str(sid(f"a-{kind}")) in {row[field] for row in body["results"]}
            assert body["total_results"] >= 1, (method, url)
        r = await client.get("/api/v2/search/combined", params={"query": QUERY})
        assert r.status_code == 200, r.text
        assert r.json()["total_results"] >= 2

        r = await client.get("/api/v2/search/health")
        assert r.status_code == 200, r.text
        health = r.json()
        assert health["search_functional"] is True, health
        assert health["status"] == "healthy", health
        assert health["index_count"] > 0, health
        assert {index["name"] for index in health["gin_indexes"]} >= GIN_INDEXES
