"""mh01 on PostgreSQL: child runs, delegated slot, per-kind outbox key.

Requires ORCHESTRATION_TEST_DATABASE_URL (skips otherwise: NOT RUN).
Mutation checks: dropping ``parent_run_id IS NULL`` from the predicate fails
test_child_and_parent_share_a_thread; leaving 'delegated' out of the CHECK
fails it too (CheckViolation); skipping _drop_index_if_invalid in the swap
fails test_upgrade_repairs_an_interrupted_build; dropping the downgrade's
several-outbox-rows guard fails test_downgrade_refuses_a_second_outbox_row,
and its delegated guard fails test_downgrade_refuses_a_delegated_run;
deleting either VALIDATE fails test_child_and_parent_share_a_thread.

The lock tests hold ACCESS SHARE on agent_runs from a second connection and
shorten the lock_timeout/backoff so the retry loop finishes in about a second.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Union
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_PATH = Path(__file__).resolve().parents[2] / "alembic/versions/mh01_machine_handoff.py"
_INDEXES = (
    "uq_agent_runs_active_thread",
    "idx_agent_runs_parent",
    "uq_agent_runs_handoff_call",
    "uq_agent_outbox_run_kind",
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "mh01_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with scoped.begin() as conn:
            await conn.execute(text("CREATE TABLE threads (id UUID PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE agent_runs (job_id VARCHAR(36) PRIMARY KEY, "
                    "thread_id UUID REFERENCES threads(id), status VARCHAR(32) NOT NULL, "
                    "CONSTRAINT ck_agent_runs_status CHECK (status IN ('queued','running',"
                    "'awaiting_confirmation','stopping','recovering','completed','failed',"
                    "'cancelled')))"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_agent_runs_active_thread ON agent_runs (thread_id) "
                    "WHERE thread_id IS NOT NULL AND status IN ('queued','running',"
                    "'awaiting_confirmation','stopping','recovering')"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE agent_outbox (id UUID PRIMARY KEY, run_id VARCHAR(36) "
                    "NOT NULL, kind VARCHAR(64) NOT NULL)"
                )
            )
            await conn.execute(
                text("CREATE UNIQUE INDEX uq_agent_outbox_run ON agent_outbox (run_id)")
            )
        yield scoped
    finally:
        await scoped.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("mh01_test", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Fast lock settings for the lock tests: ~1 s for all five attempts.
_FAST_LOCKS = {"_LOCK_TIMEOUT": "100ms", "_LOCK_BACKOFF_S": 0.05}


async def _run(
    engine: AsyncEngine, step: Union[str, Callable[[Any], None]], **overrides: Any
) -> list[str]:
    migration, executed = _migration(), []
    for name, value in overrides.items():
        assert hasattr(migration, name), name
        setattr(migration, name, value)

    def record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        executed.append(" ".join(statement.split()))

    def migrate(connection: Any) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            if isinstance(step, str):
                getattr(migration, step)()
            else:
                step(migration)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(migrate)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    return executed


async def _index_validity(engine: AsyncEngine) -> dict[str, bool]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT c.relname, i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema()"
            )
        )
        return {name: valid for name, valid in rows}


async def _index_def(engine: AsyncEngine, name: str) -> str:
    async with engine.connect() as db:
        return str(
            await db.scalar(
                text("SELECT pg_get_indexdef(to_regclass(:n))"), {"n": name}
            )
        )


async def _validated(engine: AsyncEngine) -> dict[str, bool]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid = 'agent_runs'::regclass"
            )
        )
        return {name: valid for name, valid in rows}


def _swap(predicate: str) -> Callable[[Any], None]:
    def step(migration: Any) -> None:
        with migration.op.get_context().autocommit_block():
            migration._swap_active_thread_index(getattr(migration, predicate))

    return step


async def test_child_and_parent_share_a_thread(engine: AsyncEngine) -> None:
    first = await _run(engine, "upgrade")
    second = await _run(engine, "upgrade")  # idempotent re-run
    assert sum("CREATE UNIQUE INDEX CONCURRENTLY" in s for s in first) == 3
    # The re-run finds the slot index already narrowed and does not rebuild it.
    assert not any("uq_agent_runs_active_thread_new" in s for s in second)
    validated = await _validated(engine)
    assert validated["fk_agent_runs_parent_run_id"] is True
    assert validated["ck_agent_runs_status"] is True
    indexes = await _index_validity(engine)
    assert all(indexes[name] for name in _INDEXES)
    assert "uq_agent_outbox_run" not in indexes
    assert "uq_agent_runs_active_thread_new" not in indexes
    thread, parent = uuid4(), str(uuid4())
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO threads VALUES (:t)"), {"t": thread})
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'delegated',NULL)"),
            {"j": parent, "t": thread},
        )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'running',:p)"),
            {"j": str(uuid4()), "t": thread, "p": parent},
        )
        with pytest.raises(IntegrityError, match="uq_agent_runs_active_thread"):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_runs VALUES (:j,:t,'queued',NULL)"),
                    {"j": str(uuid4()), "t": thread},
                )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'agent.stream.execute')"),
            {"i": uuid4(), "r": parent},
        )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'handoff.resume')"),
            {"i": uuid4(), "r": parent},
        )
        with pytest.raises(IntegrityError, match="uq_agent_outbox_run_kind"):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_outbox VALUES (:i,:r,'handoff.resume')"),
                    {"i": uuid4(), "r": parent},
                )


async def test_parent_delete_unlinks_child(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    parent, child = str(uuid4()), str(uuid4())
    async with engine.begin() as db:
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,NULL,'completed',NULL)"),
            {"j": parent},
        )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,NULL,'completed',:p)"),
            {"j": child, "p": parent},
        )
        await db.execute(
            text("DELETE FROM agent_runs WHERE job_id = :j"), {"j": parent}
        )
        linked = await db.scalar(
            text("SELECT parent_run_id FROM agent_runs WHERE job_id = :j"), {"j": child}
        )
    assert linked is None


async def test_upgrade_repairs_an_interrupted_build(engine: AsyncEngine) -> None:
    # A failed CREATE UNIQUE INDEX CONCURRENTLY leaves its index INVALID under
    # the temporary name; IF NOT EXISTS alone would swap it in unenforced.
    async with engine.begin() as db:
        for _ in range(2):
            await db.execute(
                text("INSERT INTO agent_runs VALUES (:j,NULL,'completed')"),
                {"j": str(uuid4())},
            )
    async with engine.connect() as raw:
        auto = await raw.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(DBAPIError):
            await auto.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY uq_agent_runs_active_thread_new "
                    "ON agent_runs (status)"
                )
            )
    assert (await _index_validity(engine))["uq_agent_runs_active_thread_new"] is False

    await _run(engine, "upgrade")

    indexes = await _index_validity(engine)
    assert all(indexes[name] for name in _INDEXES)
    assert "parent_run_id IS NULL" in await _index_def(
        engine, "uq_agent_runs_active_thread"
    )
    thread = uuid4()
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO threads VALUES (:t)"), {"t": thread})
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'delegated',NULL)"),
            {"j": str(uuid4()), "t": thread},
        )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_runs VALUES (:j,:t,'queued',NULL)"),
                    {"j": str(uuid4()), "t": thread},
                )


async def test_downgrade_refuses_while_children_exist(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    thread = uuid4()
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO threads VALUES (:t)"), {"t": thread})
        parent = str(uuid4())
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'completed',NULL)"),
            {"j": parent, "t": thread},
        )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,:t,'completed',:p)"),
            {"j": str(uuid4()), "t": thread, "p": parent},
        )
    with pytest.raises(Exception, match="handoff"):
        await _run(engine, "downgrade")


async def test_downgrade_refuses_a_second_outbox_row(engine: AsyncEngine) -> None:
    # A child whose parent was deleted has parent_run_id NULL but may still
    # own a dispatch row and a handoff.resume row; uq_agent_outbox_run would
    # fail to build (leaving an INVALID index) instead of refusing cleanly.
    await _run(engine, "upgrade")
    run = str(uuid4())
    async with engine.begin() as db:
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,NULL,'completed',NULL)"),
            {"j": run},
        )
        for kind in ("agent.stream.execute", "handoff.resume"):
            await db.execute(
                text("INSERT INTO agent_outbox VALUES (:i,:r,:k)"),
                {"i": uuid4(), "r": run, "k": kind},
            )
    with pytest.raises(Exception, match="handoff"):
        await _run(engine, "downgrade")
    assert "uq_agent_outbox_run" not in await _index_validity(engine)


async def test_downgrade_restores_the_pre_mh01_schema(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    await _run(engine, "downgrade")
    await _run(engine, "downgrade")  # idempotent re-run

    indexes = await _index_validity(engine)
    assert indexes["uq_agent_runs_active_thread"] is True
    assert indexes["uq_agent_outbox_run"] is True
    for gone in (
        "idx_agent_runs_parent",
        "uq_agent_outbox_run_kind",
        "uq_agent_runs_handoff_call",
    ):
        assert gone not in indexes
    assert "parent_run_id" not in await _index_def(
        engine, "uq_agent_runs_active_thread"
    )
    async with engine.connect() as db:
        columns = await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_schema = current_schema() AND table_name = 'agent_runs' "
                "AND column_name IN ('parent_run_id', 'handoff_tool_call_id')"
            )
        )
        validated = await db.scalar(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_agent_runs_status' "
                "AND conrelid = 'agent_runs'::regclass"
            )
        )
    assert columns == 0
    assert validated is True
    run = str(uuid4())
    async with engine.begin() as db:
        with pytest.raises(IntegrityError, match="ck_agent_runs_status"):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_runs VALUES (:j,NULL,'delegated')"),
                    {"j": str(uuid4())},
                )
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,NULL,'completed')"), {"j": run}
        )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'agent.stream.execute')"),
            {"i": uuid4(), "r": run},
        )
        with pytest.raises(IntegrityError, match="uq_agent_outbox_run"):
            async with db.begin_nested():
                await db.execute(
                    text("INSERT INTO agent_outbox VALUES (:i,:r,'handoff.resume')"),
                    {"i": uuid4(), "r": run},
                )

    await _run(engine, "upgrade")  # round trip
    indexes = await _index_validity(engine)
    assert all(indexes[name] for name in _INDEXES)


async def test_downgrade_refuses_a_delegated_run(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    run = str(uuid4())
    async with engine.begin() as db:
        await db.execute(
            text("INSERT INTO agent_runs VALUES (:j,NULL,'delegated',NULL)"),
            {"j": run},
        )
        await db.execute(
            text("INSERT INTO agent_outbox VALUES (:i,:r,'agent.stream.execute')"),
            {"i": uuid4(), "r": run},
        )
    with pytest.raises(Exception, match="handoff"):
        await _run(engine, "downgrade")
    assert "uq_agent_outbox_run" not in await _index_validity(engine)


async def test_opening_step_times_out_cleanly_under_a_held_lock(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as locker:
        await locker.execute(text("LOCK TABLE agent_runs IN ACCESS SHARE MODE"))
        started = time.monotonic()
        with pytest.raises(DBAPIError, match="re-run the migration") as caught:
            await asyncio.wait_for(_run(engine, "upgrade", **_FAST_LOCKS), 30)
        elapsed = time.monotonic() - started
        await locker.rollback()
    assert getattr(caught.value.orig, "pgcode", None) == "55P03"
    assert elapsed >= 0.5  # five 100 ms waits: it retried
    async with engine.connect() as db:
        added = await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_schema = current_schema() AND table_name = 'agent_runs' "
                "AND column_name = 'parent_run_id'"
            )
        )
    assert added == 0


async def test_swap_times_out_and_retries_instead_of_hanging(
    engine: AsyncEngine,
) -> None:
    await _run(engine, "upgrade")
    swap = _swap("_LEGACY_ACTIVE_PREDICATE")
    async with engine.connect() as locker:
        await locker.execute(text("LOCK TABLE agent_runs IN ACCESS SHARE MODE"))
        started = time.monotonic()
        with pytest.raises(DBAPIError, match="re-run the migration") as caught:
            await asyncio.wait_for(_run(engine, swap, **_FAST_LOCKS), 30)
        elapsed = time.monotonic() - started
        await locker.rollback()
    assert getattr(caught.value.orig, "pgcode", None) == "55P03"
    assert elapsed >= 0.5
    # Nothing was swapped: the slot index is unchanged and the build waits.
    assert "parent_run_id IS NULL" in await _index_def(
        engine, "uq_agent_runs_active_thread"
    )
    assert (await _index_validity(engine))["uq_agent_runs_active_thread_new"]

    # A lock released mid-retry lets a later attempt swap.
    async with engine.connect() as locker:
        await locker.execute(text("LOCK TABLE agent_runs IN ACCESS SHARE MODE"))
        task = asyncio.ensure_future(_run(engine, swap, **_FAST_LOCKS))
        await asyncio.sleep(0.25)
        assert not task.done()
        await locker.rollback()
        await asyncio.wait_for(task, 30)
    assert "parent_run_id" not in await _index_def(
        engine, "uq_agent_runs_active_thread"
    )
    assert "uq_agent_runs_active_thread_new" not in await _index_validity(engine)


async def test_one_child_per_parent_tool_call(engine: AsyncEngine) -> None:
    # Mutation check: drop the uq_agent_runs_handoff_call build from mh01 and
    # the second "call-1" child inserts.
    await _run(engine, "upgrade")
    assert (await _index_validity(engine))["uq_agent_runs_handoff_call"] is True
    parent = str(uuid4())
    insert_child = text(
        "INSERT INTO agent_runs (job_id, status, parent_run_id, handoff_tool_call_id) "
        "VALUES (:j, 'completed', :p, :c)"
    )
    async with engine.begin() as db:
        await db.execute(
            text("INSERT INTO agent_runs (job_id, status) VALUES (:j, 'delegated')"),
            {"j": parent},
        )
        for call in ("call-1", "call-2", None, None):
            await db.execute(insert_child, {"j": str(uuid4()), "p": parent, "c": call})
        with pytest.raises(IntegrityError, match="uq_agent_runs_handoff_call"):
            async with db.begin_nested():
                await db.execute(
                    insert_child, {"j": str(uuid4()), "p": parent, "c": "call-1"}
                )
        # Another parent may reuse the call id.
        other = str(uuid4())
        await db.execute(
            text("INSERT INTO agent_runs (job_id, status) VALUES (:j, 'completed')"),
            {"j": other},
        )
        await db.execute(insert_child, {"j": str(uuid4()), "p": other, "c": "call-1"})
