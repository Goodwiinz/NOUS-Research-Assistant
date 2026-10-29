"""``delegated``: a parent run paused while a child Codex run works."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.agent.streaming import _stream_failure_message
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AGENT_RUN_STATUS_CHECK, AgentRun
from src.models.thread import Thread
from src.services.agent import agent_run_service, agent_submission_service
from src.services.agent.agent_execution_service import _clear_stale_pending_confirmation
from src.services.agent.agent_run_service import (
    ActiveRunConflict,
    HandoffInProgress,
    get_active_run_for_thread,
    get_latest_run_for_thread,
    upsert_run,
)
from src.services.agent.agent_submission_service import _ensure_thread_idle
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

THREAD = uuid.UUID("55555555-5555-5555-5555-555555555555")
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")


def test_delegated_is_active_and_constrained() -> None:
    assert JobStatus("delegated") is JobStatus.DELEGATED
    assert not JobStatus.DELEGATED.is_terminal
    assert "'delegated'" in AGENT_RUN_STATUS_CHECK
    assert "delegated" in agent_submission_service._ACTIVE_RUN_STATUSES
    assert "delegated" in agent_run_service._ACTIVE_RUN_STATUSES


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_tx(connection, _record):  # type: ignore[no-untyped-def]
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection):  # type: ignore[no-untyped-def]
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _run(status: str, *, parent: str | None = None, provider: str = "nous") -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "thread_id": THREAD,
        "status": status,
        "execution_provider": provider,
        "parent_run_id": parent,
    }


async def test_child_may_run_while_parent_delegated(db: AsyncSession) -> None:
    parent = _run("delegated")
    await db.execute(insert(AgentRun).values(**parent))
    await db.execute(
        insert(AgentRun).values(
            **_run("running", parent=parent["job_id"], provider="codex")
        )
    )
    # Passes by not raising IntegrityError: the child is exempt from the
    # active-thread index while its parent holds the slot.
    await db.commit()


async def test_second_top_level_active_run_is_refused(db: AsyncSession) -> None:
    await db.execute(insert(AgentRun).values(**_run("delegated")))
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(insert(AgentRun).values(**_run("queued")))
    await db.rollback()


async def test_outbox_allows_one_row_per_run_and_kind(db: AsyncSession) -> None:
    run = _run("completed", provider="codex")
    await db.execute(insert(AgentRun).values(**run))
    db.add(AgentOutbox(run_id=run["job_id"], kind="harness.execute", payload={}))
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    await db.commit()
    db.add(AgentOutbox(run_id=run["job_id"], kind="handoff.resume", payload={}))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# ---------------------------------------------------------------------------
# Thread-scoped lookups see top-level runs only
#
# Mutation check: drop ``top_level_run()`` from any one lookup below and its
# test fails (``MultipleResultsFound``, or the child returned as "latest").
# ---------------------------------------------------------------------------


async def _family(db: AsyncSession) -> tuple[str, str]:
    """A ``delegated`` parent and its ``running`` Codex child on one thread."""
    started = datetime(2026, 10, 28, tzinfo=timezone.utc)
    parent = {
        **_run("delegated"),
        "organization_id": ORG,
        "user_id": USER,
        "created_at": started,
        "updated_at": started,
    }
    # The child is newer on both orderings, so an unfiltered "latest" lookup
    # returns it deterministically rather than by job_id tie-break.
    later = started + timedelta(minutes=1)
    child = {
        **_run("running", parent=parent["job_id"], provider="codex"),
        "organization_id": ORG,
        "user_id": USER,
        "created_at": later,
        "updated_at": later,
    }
    await db.execute(insert(AgentRun).values(**parent))
    await db.execute(insert(AgentRun).values(**child))
    await db.commit()
    return parent["job_id"], child["job_id"]


async def test_active_thread_lookup_returns_the_parent(db: AsyncSession) -> None:
    parent, _child = await _family(db)
    active = await get_active_run_for_thread(
        db, THREAD, organization_id=ORG, user_id=USER
    )
    assert active is not None and active.job_id == parent


async def test_latest_thread_lookup_returns_the_parent(db: AsyncSession) -> None:
    parent, _child = await _family(db)
    for by_creation in (True, False):
        latest = await get_latest_run_for_thread(
            db, THREAD, organization_id=ORG, user_id=USER, by_creation=by_creation
        )
        assert latest is not None and latest.job_id == parent


async def test_upsert_insert_conflict_names_the_parent(db: AsyncSession) -> None:
    """A new top-level run hits the active-thread index; the re-read must not
    see the child too, or the conflict surfaces as ``MultipleResultsFound``."""
    await _family(db)
    with pytest.raises(ActiveRunConflict):
        await upsert_run(
            db,
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            organization_id=ORG,
            user_id=USER,
            thread_id=str(THREAD),
        )


class _ActiveThreadViolation(Exception):
    """Driver-shaped evidence for the active-thread unique index."""

    constraint_name = "uq_agent_runs_active_thread"


async def test_upsert_transition_conflict_names_the_parent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding a thread-less active run to the thread conflicts the same way.

    SQLite binds the ``coalesce`` backfill value in a different text form from
    the GUID column, so the index never fires here; the first commit raises
    the PostgreSQL-shaped violation instead and the real re-read runs.
    """
    old = {
        **_run("queued"),
        "thread_id": None,
        "organization_id": ORG,
        "user_id": USER,
    }
    await db.execute(insert(AgentRun).values(**old))
    await db.commit()
    await _family(db)

    real_commit = db.commit
    failed = False

    async def commit_hits_active_index() -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise IntegrityError("UPDATE", {}, _ActiveThreadViolation())
        await real_commit()

    monkeypatch.setattr(db, "commit", commit_hits_active_index)
    with pytest.raises(ActiveRunConflict):
        await upsert_run(
            db,
            job_id=old["job_id"],
            status=JobStatus.RUNNING,
            organization_id=ORG,
            user_id=USER,
            thread_id=str(THREAD),
        )


async def test_thread_idle_check_sees_the_parent_only(db: AsyncSession) -> None:
    await _family(db)
    with pytest.raises(ActiveRunConflict):
        await agent_submission_service._ensure_thread_idle(
            db, thread_id=THREAD, user_id=USER
        )


# ---------------------------------------------------------------------------
# A new turn during a handoff is refused with the notice, never abandoning the
# delegated parent (the owner's "blocked with a notice" decision).
#
# Mutation check: drop the ``DELEGATED`` branch in ``_ensure_thread_idle`` and
# this test fails with a plain ``ActiveRunConflict``.
# ---------------------------------------------------------------------------


async def test_new_turn_during_handoff_gets_the_notice(db: AsyncSession) -> None:
    parent, _child = await _family(db)
    with pytest.raises(HandoffInProgress) as raised:
        await _ensure_thread_idle(db, thread_id=THREAD, user_id=USER)
    assert _stream_failure_message(raised.value) == (
        "Codex is still working: cancel it or wait."
    )
    run = await db.get(AgentRun, parent)
    assert run is not None and run.status == "delegated"  # never abandoned


# ---------------------------------------------------------------------------
# A leftover handoff interrupt is cleared like any other stale interrupt.
# Every caller of the cleanup already holds the thread slot, and a new turn is
# refused while the parent is ``delegated`` (HandoffInProgress), so the cleanup
# only sees a handoff interrupt after the parent ended without resuming it.
# Keeping it alive would re-fire it and block every later turn on the thread.
#
# Mutation check: add a ``handoff_run_id`` early return to
# ``_clear_stale_pending_confirmation`` and ``aupdate_state`` never runs.
# ---------------------------------------------------------------------------


async def test_stale_confirmation_cleanup_clears_a_leftover_handoff_interrupt() -> None:
    interrupt = SimpleNamespace(value={"handoff_run_id": "child"})
    snapshot = SimpleNamespace(
        values={"messages": []},
        tasks=(SimpleNamespace(interrupts=(interrupt,)),),
    )

    class Graph:
        updated: dict | None = None

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return snapshot

        async def aupdate_state(self, _config, values):  # type: ignore[no-untyped-def]
            Graph.updated = values

    assert await _clear_stale_pending_confirmation(Graph(), {}) is not None
    assert Graph.updated is not None
    assert Graph.updated["pending_confirmation"] == {}
