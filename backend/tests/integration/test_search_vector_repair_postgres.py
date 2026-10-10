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
the real ``env.py`` and checks the health probe, the indexes, the triggers
and the backfill. The correct-schema case proves the no-op: every row keeps
its ``xmin``, a hand-set vector survives, every trigger and index keeps its
OID, and the repair takes no AccessExclusiveLock on any relation.

The restore was ``pg_restore --no-owner``, so the live objects may belong to
another role; two cases run the chain as a scratch non-owner role and expect
the migration's own error, naming the object, its owner, the runner and the
``ALTER ... OWNER TO`` remedy, before any DDL.

The last test is the guard: the chat search routes and the health endpoint
against the migration-built schema rather than ``create_all``, so a chain
that stops producing these objects fails here before it reaches a database.

Uses the two-account PostgreSQL lane: skips unless
``TWO_ACCOUNT_PG_TEST_DATABASE_URL`` names a disposable server whose user may
``CREATE DATABASE`` and ``CREATE ROLE`` (CI's ``test`` superuser; the step
rejects a skipped run). Every scratch database and role is dropped in a
``finally``.

Mutation-verified (2026-10-09, PostgreSQL 14), each guard in
``backend/alembic/versions/sv01_search_vector_repair.py`` removed in turn,
the focused test observed failing on the named defect, the guard restored
with ``git diff`` empty and the test passing again. Focused command:
``TWO_ACCOUNT_PG_TEST_DATABASE_URL=... pytest
backend/tests/integration/test_search_vector_repair_postgres.py
-c backend/pytest.ini -m integration -k "<expr>"``.

- the ``WHERE search_vector IS NULL`` of each backfill UPDATE (NULL-only
  rewrite): ``-k mixed`` fails on the hand-set vector being rewritten.
- the ``IF column_added OR EXISTS (...)`` around each backfill (no UPDATE
  statement on a correct schema): ``-k untouched`` fails on the
  RowExclusiveLock the UPDATE takes on threads.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Tuple, cast

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError
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
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "sv01_search_vector_repair.py"
PARENT = "rp01_thread_fk_ondelete"
REVISION = "sv01_search_vector_repair"
# Supabase's default; pinned so plainto_tsquery() stems like the 'english'
# builders whatever locale the server was initialised with.
TEXT_SEARCH_CONFIG = "pg_catalog.english"
QUERY = "ISO-CANARY"  # matches every seeded title and message
SCRATCH_PASSWORD = "svr-scratch-not-a-secret"

# The objects b2c3d4e5f6g7 creates, by name.
GIN_INDEXES = {
    "idx_threads_search_vector",
    "idx_threads_title_gin",
    "idx_chat_messages_search_vector",
    "idx_chat_messages_content_gin",
}
BTREE_INDEXES = {"idx_threads_conversation_status", "idx_chat_messages_thread_role"}
CITATIONS_INDEX = "idx_citations_snippet_gin"
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
        "DROP INDEX IF EXISTS idx_threads_conversation_status",
        "DROP INDEX IF EXISTS idx_chat_messages_thread_role",
        "DROP INDEX IF EXISTS idx_citations_snippet_gin",
        "UPDATE threads SET search_vector = NULL",
        "UPDATE chat_messages SET search_vector = NULL",
    ],
    # namesakes that cannot serve: a btree under a GIN name, and an INVALID
    # index (an interrupted CONCURRENTLY build; added by _apply_drift)
    "unusable_indexes": [
        "DROP INDEX idx_threads_title_gin",
        "CREATE INDEX idx_threads_title_gin ON threads USING btree (title)",
        "DROP INDEX idx_chat_messages_search_vector",
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


def _admin_user() -> str:
    return cast(str, make_url(pg_url("psycopg2")).username)


def _admin(*statements: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    finally:
        admin.dispose()


def _create_database(name: str, template: str | None = None) -> None:
    clause = f' TEMPLATE "{template}"' if template else ""
    _admin(f'CREATE DATABASE "{name}"{clause}')


def _drop_database(name: str) -> None:
    _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _alembic_run(url: URL, *alembic_args: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``alembic`` CLI (``env.py`` and all) against ``url``."""
    # env.py prefers SUPABASE_DB_URL: it must not point the run elsewhere.
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    env["DATABASE_URL"] = url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _alembic(database: str, *alembic_args: str) -> str:
    url = make_url(pg_url("psycopg2")).set(database=database)
    done = _alembic_run(url, *alembic_args)
    assert done.returncode == 0, done.stderr[-4000:]
    return done.stderr


def _script_head() -> str:
    """The chain's single head. Later migrations stack on REVISION, so
    ``upgrade head`` ends there, with REVISION applied on the way."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    chain = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert REVISION in chain
    return heads[0]


def _repair_sql() -> List[str]:
    """The migration's three DO blocks, for running outside alembic."""
    spec = importlib.util.spec_from_file_location("sv01_repair_under_test", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [module._THREADS, module._CHAT_MESSAGES, module._CITATIONS]


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


def _apply_drift(scratch: Scratch, state: str) -> None:
    scratch.execute(*DRIFT[state])
    if state == "unusable_indexes":
        # Leave an INVALID GIN idx_chat_messages_search_vector behind: a
        # CONCURRENTLY build whose (immutable) expression fails on the first
        # row dies after the catalog entry exists, like an interrupted deploy.
        with scratch.sync.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            with pytest.raises(DBAPIError, match="division by zero"):
                connection.exec_driver_sql(
                    "CREATE INDEX CONCURRENTLY idx_chat_messages_search_vector "
                    "ON chat_messages USING GIN "
                    "(to_tsvector('english', (length(content) / 0)::text))"
                )
        assert (
            scratch.scalar(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = 'idx_chat_messages_search_vector'::regclass"
            )
            is False
        )


# --- observations ------------------------------------------------------------


def _health(scratch: Scratch) -> Dict[str, Any]:
    """The real ``GET /api/v2/search/health`` handler on a fresh session."""
    with scratch.session() as db:
        body = search_health_check(db=db, current_user=cast(User, None))
    return cast(Dict[str, Any], body)


def _health_index_names(scratch: Scratch) -> set[str]:
    return {index["name"] for index in _health(scratch)["gin_indexes"]}


def _column_type(scratch: Scratch, table: str) -> str | None:
    return cast(
        "str | None",
        scratch.scalar(
            "SELECT udt_name FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}' "
            "AND column_name = 'search_vector'"
        ),
    )


def _indexes(scratch: Scratch, access_method: str) -> set[str]:
    """Valid indexes of ``access_method`` on the three b2c3d4e5f6g7 tables."""
    return {
        name
        for (name,) in scratch.rows(
            "SELECT c.relname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_am am ON am.oid = c.relam "
            "WHERE t.relname IN ('threads', 'chat_messages', 'citations') "
            f"AND am.amname = '{access_method}' AND i.indisvalid"
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


def _object_identity(scratch: Scratch) -> Dict[str, Tuple[str, ...]]:
    """OID (and row xmin) of every trigger and index the repair owns: a
    recreated object gets a new OID, a touched catalog row a new xmin."""
    identity: Dict[str, Tuple[str, ...]] = {}
    for name, oid, xmin in scratch.rows(
        "SELECT tgname, oid, xmin::text FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgrelid IN ('threads'::regclass, 'chat_messages'::regclass)"
    ):
        identity[f"trigger:{name}"] = (str(oid), xmin)
    for name, oid in scratch.rows(
        "SELECT c.relname, c.oid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "WHERE t.relname IN ('threads', 'chat_messages', 'citations')"
    ):
        identity[f"index:{name}"] = (str(oid),)
    return identity


def _locks_taken_by_repair(scratch: Scratch) -> List[Tuple[str, str]]:
    """(relation, mode) of every lock stronger than AccessShare the repair
    holds inside its own transaction (inspected before rollback). On a correct
    schema there must be none: no DDL (AccessExclusive) and no UPDATE
    (RowExclusive)."""
    with scratch.sync.connect() as connection:
        transaction = connection.begin()
        try:
            for statement in _repair_sql():
                connection.execute(text(statement))
            rows = connection.execute(
                text(
                    "SELECT relation::regclass::text, mode FROM pg_locks "
                    "WHERE pid = pg_backend_pid() AND locktype = 'relation' "
                    "AND mode <> 'AccessShareLock' "
                    "AND relation::regclass::text NOT LIKE 'pg_%'"
                )
            ).all()
        finally:
            transaction.rollback()
    return sorted((str(row[0]), str(row[1])) for row in rows)


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


def _assert_repaired(scratch: Scratch) -> None:
    assert scratch.scalar("SELECT version_num FROM alembic_version") == _script_head()
    assert _column_type(scratch, "threads") == "tsvector"
    assert _column_type(scratch, "chat_messages") == "tsvector"
    assert _indexes(scratch, "gin") >= GIN_INDEXES | {CITATIONS_INDEX}
    assert _indexes(scratch, "btree") >= BTREE_INDEXES
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


# --- the repair --------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(DRIFT))
async def test_upgrade_head_restores_full_text_search(
    scratch: Scratch, tmp_path: Path, state: str
) -> None:
    await _seed(scratch, tmp_path)
    assert scratch.scalar("SELECT COUNT(*) FROM threads") == 3
    assert scratch.scalar("SELECT COUNT(*) FROM chat_messages") == 3
    _apply_drift(scratch, state)

    broken = _health(scratch)
    if state == "column_dropped":
        # the live symptom: the probe statement itself fails
        assert broken["search_functional"] is False, broken
    if state == "column_is_text":
        # PostgreSQL defines text @@ tsquery (implicit to_tsvector), so the
        # probe survives this state; the column type is the evidence.
        assert _column_type(scratch, "threads") == "text"
        assert _column_type(scratch, "chat_messages") == "text"
    if state == "unusable_indexes":
        # the btree namesake is not a GIN index and the INVALID one serves
        # no query: neither may count as healthy
        assert "idx_threads_title_gin" not in _health_index_names(scratch)
        assert "idx_chat_messages_search_vector" not in _health_index_names(scratch)
    if state not in ("correct", "unusable_indexes"):
        assert not _indexes(scratch, "gin") & {
            "idx_threads_search_vector",
            "idx_chat_messages_search_vector",
        }
        assert not _triggers(scratch) & TRIGGERS

    _alembic(scratch.name, "upgrade", "head")
    _assert_repaired(scratch)
    if state == "unusable_indexes":
        assert "USING gin (search_vector)" in scratch.scalar(
            "SELECT pg_get_indexdef('idx_chat_messages_search_vector'::regclass)"
        )
        assert "USING gin (to_tsvector(" in scratch.scalar(
            "SELECT pg_get_indexdef('idx_threads_title_gin'::regclass)"
        )
    _assert_triggers_fill_new_rows(scratch)


async def test_upgrade_head_leaves_a_correct_schema_untouched(
    scratch: Scratch, tmp_path: Path
) -> None:
    """A correct schema is a no-op: no row is rewritten, a vector that is
    not what the builder would produce is kept (the backfill is NULL-only),
    every trigger and index keeps its OID, and no AccessExclusiveLock is
    taken on any relation."""
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
    identity = _object_identity(scratch)
    assert {"trigger:" + name for name in TRIGGERS} <= set(identity)
    assert {"index:" + name for name in GIN_INDEXES | BTREE_INDEXES} <= set(identity)

    assert _locks_taken_by_repair(scratch) == []

    _alembic(scratch.name, "upgrade", "head")
    assert scratch.scalar("SELECT version_num FROM alembic_version") == _script_head()

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
    assert _object_identity(scratch) == identity
    assert _functions(scratch) >= FUNCTIONS
    assert _health(scratch)["search_functional"] is True


async def test_backfill_fills_only_null_vectors_in_a_mixed_table(
    scratch: Scratch, tmp_path: Path
) -> None:
    """When only some rows lost their vector, the backfill fills exactly
    those and leaves every other row (even one whose vector is not what the
    builder would produce) with its value and its xmin."""
    await _seed(scratch, tmp_path)
    kept_thread, kept_message = sid("a-thread"), sid("a-msg")
    lost_thread, lost_message = sid("b-thread"), sid("b-msg")
    scratch.execute(
        "UPDATE threads SET search_vector = to_tsvector('simple', 'sentinel') "
        f"WHERE id = '{kept_thread}'",
        "UPDATE chat_messages SET search_vector = to_tsvector('simple', 'sentinel') "
        f"WHERE id = '{kept_message}'",
        f"UPDATE threads SET search_vector = NULL WHERE id = '{lost_thread}'",
        f"UPDATE chat_messages SET search_vector = NULL WHERE id = '{lost_message}'",
    )
    thread_xmins = _xmins(scratch, "threads")
    message_xmins = _xmins(scratch, "chat_messages")

    _alembic(scratch.name, "upgrade", "head")

    assert scratch.scalar(
        "SELECT search_vector = to_tsvector('simple', 'sentinel') "
        f"FROM threads WHERE id = '{kept_thread}'"
    )
    assert scratch.scalar(
        "SELECT search_vector = to_tsvector('simple', 'sentinel') "
        f"FROM chat_messages WHERE id = '{kept_message}'"
    )
    assert scratch.scalar(
        f"SELECT search_vector = ({THREAD_VECTOR}) FROM threads "
        f"WHERE id = '{lost_thread}'"
    )
    assert scratch.scalar(
        f"SELECT search_vector = ({MESSAGE_VECTOR}) FROM chat_messages "
        f"WHERE id = '{lost_message}'"
    )
    # only the two refilled rows were written
    assert {
        k for k, v in _xmins(scratch, "threads").items() if v != thread_xmins[k]
    } == {str(lost_thread)}
    assert {
        k for k, v in _xmins(scratch, "chat_messages").items() if v != message_xmins[k]
    } == {str(lost_message)}


async def test_upgrade_head_is_idempotent_after_a_repair(
    scratch: Scratch, tmp_path: Path
) -> None:
    """Re-running the chain (a re-deploy, ``alembic stamp`` + upgrade) after
    the repair rewrites nothing and recreates nothing."""
    await _seed(scratch, tmp_path)
    _apply_drift(scratch, "column_dropped")
    _alembic(scratch.name, "upgrade", "head")
    thread_xmins = _xmins(scratch, "threads")
    identity = _object_identity(scratch)
    _alembic(scratch.name, "downgrade", PARENT)  # a no-op that only re-stamps
    assert _column_type(scratch, "threads") == "tsvector"
    _alembic(scratch.name, "upgrade", "head")
    assert _xmins(scratch, "threads") == thread_xmins
    assert _object_identity(scratch) == identity
    assert _health(scratch)["search_functional"] is True


# --- ownership ---------------------------------------------------------------


@pytest.mark.parametrize("foreign", ["table", "function"])
async def test_runner_without_ownership_fails_by_name_before_any_ddl(
    scratch: Scratch, tmp_path: Path, foreign: str
) -> None:
    """``pg_restore --no-owner`` can leave the objects owned by another role.
    A runner that holds GRANT ALL but not ownership must get the migration's
    own message (object, owner, runner, ``ALTER ... OWNER TO`` remedy), not
    PostgreSQL's mid-block "must be owner of ...", and the chain must stay at
    the parent. ``foreign=function``: the tables are the runner's, only the
    trigger functions belong to someone else."""
    await _seed(scratch, tmp_path)
    runner = f"svr_runner_{uuid.uuid4().hex[:12]}"
    owner = _admin_user()
    _admin(
        f"CREATE ROLE \"{runner}\" LOGIN PASSWORD '{SCRATCH_PASSWORD}'",
        f'GRANT CONNECT ON DATABASE "{scratch.name}" TO "{runner}"',
    )
    try:
        scratch.execute(
            f'GRANT USAGE, CREATE ON SCHEMA public TO "{runner}"',
            f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{runner}"',
            f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{runner}"',
        )
        if foreign == "function":
            scratch.execute(
                *(
                    f'ALTER TABLE {table} OWNER TO "{runner}"'
                    for table in ("threads", "chat_messages", "citations")
                )
            )
            expected = (
                f"function update_thread_search_vector() is owned by {owner}",
                f"ALTER FUNCTION update_thread_search_vector() OWNER TO {runner};",
            )
        else:
            expected = (
                f"table threads is owned by {owner}",
                f"ALTER TABLE threads OWNER TO {runner};",
            )

        url = make_url(pg_url("psycopg2")).set(
            database=scratch.name, username=runner, password=SCRATCH_PASSWORD
        )
        done = _alembic_run(url, "upgrade", "head")

        assert done.returncode != 0
        for fragment in (*expected, f"this migration runs as {runner}"):
            assert fragment in done.stderr, done.stderr[-3000:]
        assert "must be owner of" not in done.stderr
        assert scratch.scalar("SELECT version_num FROM alembic_version") == PARENT
    finally:
        scratch.execute(
            f'REASSIGN OWNED BY "{runner}" TO "{owner}"',
            f'DROP OWNED BY "{runner}"',
        )
        _admin(f'DROP ROLE "{runner}"')


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
