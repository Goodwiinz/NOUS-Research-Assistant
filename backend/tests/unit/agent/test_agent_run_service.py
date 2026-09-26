"""agent_run_service — the durable Postgres projection of agent jobs.

Covers the audit P1.2 contract on a throwaway sqlite engine (only the
``agent_runs`` table is created — the shared Base carries postgres-only
types elsewhere, so never ``create_all`` the full metadata):

- upsert: create, status update, legacy "error" normalization, tenancy
  backfill-only semantics, absorbing terminal states, ownerless-insert skip
- get_run: mandatory organization_id + user_id tenancy filter (null-safe)
- claim/release lease: atomic compare-and-claim, re-entrant renewal,
  expired-lease takeover
- list_stale_runs: sweeper candidate listing (non-terminal, stale, unleased)
- record_job_status: the log-and-continue write-through entry point
"""

import asyncio
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Update

from src.models.agent_run import AgentRun
from src.services.agent import agent_run_service as svc
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _job_id() -> str:
    return str(uuid.uuid4())


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _ThreadForeignKeyViolation(Exception):
    """Driver-shaped evidence for a missing thread FK in the wrapper test."""

    sqlstate = "23503"
    constraint_name = "agent_runs_thread_id_fkey"


# ---------------------------------------------------------------------------
# upsert_run
# ---------------------------------------------------------------------------


async def test_upsert_creates_then_updates(session_factory):
    job_id = _job_id()
    thread_uuid = uuid.uuid4()
    async with session_factory() as db:
        run = await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.RUNNING,
            organization_id=str(ORG_A),
            user_id=str(USER_A),
            thread_id=str(thread_uuid),
        )
        assert run is not None
        assert run.status == "running"
        assert run.organization_id == ORG_A
        assert run.user_id == USER_A

        run = await svc.upsert_run(db, job_id=job_id, status=JobStatus.COMPLETED)
        assert run.status == "completed"
        # Tenancy survives a replace-style write that omitted it.
        assert run.organization_id == ORG_A
        assert run.thread_id == thread_uuid


async def test_upsert_drops_non_uuid_thread_correlation(session_factory):
    """thread_id is a GUID column now — free-form legacy strings are dropped
    rather than poisoning the bind (the run row itself must still persist)."""
    job_id = _job_id()
    async with session_factory() as db:
        run = await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=str(USER_A),
            thread_id="t-1",
        )
        assert run is not None
        assert run.status == "running"
        assert run.thread_id is None


async def test_upsert_rejects_a_second_active_run_for_the_thread(session_factory):
    thread_id = uuid.uuid4()
    async with session_factory() as db:
        await svc.upsert_run(
            db,
            job_id=_job_id(),
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )
        with pytest.raises(svc.ActiveRunConflict):
            await svc.upsert_run(
                db,
                job_id=_job_id(),
                status=JobStatus.RUNNING,
                user_id=USER_A,
                thread_id=thread_id,
            )


async def test_upsert_normalizes_legacy_error_alias(session_factory):
    """A pre-collapse writer's "error" is stored as canonical "failed"."""
    job_id = _job_id()
    async with session_factory() as db:
        run = await svc.upsert_run(
            db,
            job_id=job_id,
            status="error",
            user_id=str(USER_A),
            error="boom",
        )
        assert run.status == "failed"
        assert run.error == "boom"


async def test_upsert_drops_unknown_status(session_factory):
    async with session_factory() as db:
        assert (
            await svc.upsert_run(
                db, job_id=_job_id(), status="exploded", user_id=str(USER_A)
            )
            is None
        )


async def test_upsert_skips_ownerless_insert(session_factory):
    """No row + no user_id → skip (an ownerless row is unreadable anyway)."""
    job_id = _job_id()
    async with session_factory() as db:
        assert await svc.upsert_run(db, job_id=job_id, status=JobStatus.RUNNING) is None
        assert await db.get(AgentRun, job_id) is None


async def test_terminal_states_are_absorbing(session_factory):
    """A delayed projection must not rewrite a finished run."""
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.COMPLETED, user_id=str(USER_A)
        )
        run = await svc.upsert_run(db, job_id=job_id, status=JobStatus.RUNNING)
        assert run.status == "completed"

        run = await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.FAILED, error="late failure"
        )
        assert run.status == "completed"
        assert run.error is None

        # Non-terminal ↔ non-terminal stays free-form (confirm / re-park).
        job2 = _job_id()
        await svc.upsert_run(
            db,
            job_id=job2,
            status=JobStatus.AWAITING_CONFIRMATION,
            user_id=str(USER_A),
        )
        run2 = await svc.upsert_run(db, job_id=job2, status=JobStatus.RUNNING)
        assert run2.status == "running"


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.AWAITING_CONFIRMATION,
        JobStatus.STOPPING,
    ],
)
def test_non_terminal_run_statuses(status: JobStatus) -> None:
    assert status.is_terminal is False


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
)
def test_terminal_run_statuses(status: JobStatus) -> None:
    assert status.is_terminal is True


async def test_terminal_absorbs_stopping(session_factory):
    """A delayed ``stopping`` projection must not resurrect a cancelled run."""
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.CANCELLED, user_id=str(USER_A)
        )
        run = await svc.upsert_run(db, job_id=job_id, status=JobStatus.STOPPING)
        assert run.status == "cancelled"


async def test_queued_to_running_transition(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.QUEUED, user_id=str(USER_A)
        )
        run = await svc.upsert_run(db, job_id=job_id, status=JobStatus.RUNNING)
        assert run.status == "running"


async def test_upsert_backfills_missing_org_for_same_owner(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.RUNNING, user_id=str(USER_A)
        )
        run = await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.RUNNING,
            organization_id=str(ORG_A),
            user_id=str(USER_A),
        )
        assert run.organization_id == ORG_A
        assert run.user_id == USER_A


class _PausedTransitionSession:
    """Pause a stale writer immediately before its transition reaches SQL."""

    def __init__(self, db: Any, reached: asyncio.Event, release: asyncio.Event) -> None:
        self._db = db
        self._reached = reached
        self._release = release
        self._paused = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        row = await self._db.get(*args, **kwargs)
        if row is not None and not self._paused:
            self._paused = True
            self._reached.set()
            await self._release.wait()
        return row

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update) and not self._paused:
            self._paused = True
            self._reached.set()
            await self._release.wait()
        return await self._db.execute(statement, *args, **kwargs)


async def _active_rows_for_thread(db: Any, thread_id: Any) -> Any:
    return (
        (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.thread_id == thread_id,
                    AgentRun.status.in_(svc._ACTIVE_RUN_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )


async def test_terminal_update_loses_to_completed(session_factory: Any) -> None:
    """An update that read RUNNING before a concurrent completion must lose."""
    job_id = _job_id()
    thread_id = uuid.uuid4()
    reached, release = asyncio.Event(), asyncio.Event()
    async with session_factory() as setup:
        await svc.upsert_run(
            setup,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )

    async with session_factory() as delayed_db, session_factory() as finisher_db:
        delayed = asyncio.create_task(
            svc.upsert_run(
                _PausedTransitionSession(delayed_db, reached, release),
                job_id=job_id,
                status=JobStatus.AWAITING_CONFIRMATION,
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=2)
        await svc.upsert_run(finisher_db, job_id=job_id, status=JobStatus.COMPLETED)
        release.set()
        await delayed

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert run is not None
        assert run.status == JobStatus.COMPLETED.value
        assert run.user_id == USER_A
        assert run.cancel_requested_at is None
        assert await _active_rows_for_thread(verify, thread_id) == []


async def test_terminal_update_loses_to_cancelled(session_factory: Any) -> None:
    """A stale completion cannot replace the producer's cancellation ACK."""
    job_id = _job_id()
    thread_id = uuid.uuid4()
    reached, release = asyncio.Event(), asyncio.Event()
    async with session_factory() as setup:
        await svc.upsert_run(
            setup,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )

    async with session_factory() as delayed_db, session_factory() as canceller_db:
        delayed = asyncio.create_task(
            svc.upsert_run(
                _PausedTransitionSession(delayed_db, reached, release),
                job_id=job_id,
                status=JobStatus.COMPLETED,
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=2)
        stopping = await canceller_db.get(AgentRun, job_id)
        assert stopping is not None
        stopping.status = JobStatus.STOPPING.value
        stopping.cancel_requested_at = datetime.now(timezone.utc)
        await canceller_db.commit()
        await svc.upsert_run(canceller_db, job_id=job_id, status=JobStatus.CANCELLED)
        release.set()
        await delayed

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert run is not None
        assert run.status == JobStatus.CANCELLED.value
        assert run.user_id == USER_A
        assert run.cancel_requested_at is not None
        assert await _active_rows_for_thread(verify, thread_id) == []


async def test_stopping_rejects_completed(session_factory: Any) -> None:
    """A completion racing an accepted Stop leaves the run visibly stopping."""
    job_id = _job_id()
    thread_id = uuid.uuid4()
    async with session_factory() as db:
        await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )
        run = await db.get(AgentRun, job_id)
        assert run is not None
        run.status = JobStatus.STOPPING.value
        run.cancel_requested_at = datetime.now(timezone.utc)
        await db.commit()

    async with session_factory() as stale_writer:
        await svc.upsert_run(stale_writer, job_id=job_id, status=JobStatus.COMPLETED)

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert run is not None
        assert run.status == JobStatus.STOPPING.value
        assert run.user_id == USER_A
        assert run.cancel_requested_at is not None
        assert len(await _active_rows_for_thread(verify, thread_id)) == 1


async def test_insert_race_preserves_owner_and_active_slot(
    session_factory: Any,
) -> None:
    """A duplicate job-id insert from another owner cannot mutate the winner."""
    job_id = _job_id()
    thread_id = uuid.uuid4()

    async with session_factory() as winner:
        await svc.upsert_run(
            winner,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )

    async with session_factory() as conflict_db:

        class InsertRaceView:
            """Model READ COMMITTED statements issued just before a winner commits."""

            def __init__(self, db: Any) -> None:
                self._db = db
                self._miss_update = True
                self._miss_reload = True

            def __getattr__(self, name: str) -> Any:
                return getattr(self._db, name)

            async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(statement, Update) and self._miss_update:
                    self._miss_update = False
                    return _ScalarResult(None)
                if isinstance(statement, Select) and self._miss_reload:
                    self._miss_reload = False
                    return _ScalarResult(None)
                return await self._db.execute(statement, *args, **kwargs)

        returned = await svc.upsert_run(
            InsertRaceView(conflict_db),
            job_id=job_id,
            status=JobStatus.COMPLETED,
            user_id=USER_B,
            thread_id=thread_id,
        )

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert run is not None
        assert run.status == JobStatus.RUNNING.value
        assert run.user_id == USER_A
        assert run.cancel_requested_at is None
        assert run.thread_id == thread_id
        assert returned is None
        active = await _active_rows_for_thread(verify, thread_id)
        assert len(active) == 1
        assert active[0].job_id == job_id

        with pytest.raises(svc.ActiveRunConflict):
            await svc.upsert_run(
                verify,
                job_id=_job_id(),
                status=JobStatus.RUNNING,
                user_id=USER_B,
                thread_id=thread_id,
            )


async def test_wrong_owner_cannot_backfill_legacy_organization(
    session_factory: Any,
) -> None:
    """A foreign status payload cannot change a legacy row's tenant scope."""
    job_id = _job_id()
    thread_id = uuid.uuid4()
    foreign_thread_id = uuid.uuid4()
    async with session_factory() as setup:
        await svc.upsert_run(
            setup,
            job_id=job_id,
            status=JobStatus.RUNNING,
            user_id=USER_A,
            thread_id=thread_id,
        )

    async with session_factory() as foreign_writer:
        returned = await svc.upsert_run(
            foreign_writer,
            job_id=job_id,
            status=JobStatus.COMPLETED,
            organization_id=ORG_B,
            user_id=USER_B,
            thread_id=foreign_thread_id,
            idempotency_key="foreign-owner-key",
        )

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert returned is None
        assert run is not None
        assert run.status == JobStatus.RUNNING.value
        assert run.organization_id is None
        assert run.user_id == USER_A
        assert run.thread_id == thread_id
        assert run.idempotency_key is None


async def test_dangling_thread_fallback_does_not_resurrect_completed(
    session_factory: Any,
) -> None:
    """The legacy uncorrelated retry uses the same atomic status guard."""
    job_id = _job_id()
    async with session_factory() as setup:
        await svc.upsert_run(
            setup, job_id=job_id, status=JobStatus.RUNNING, user_id=USER_A
        )

    async with session_factory() as stale_db, session_factory() as finisher_db:
        fallback_read, release = asyncio.Event(), asyncio.Event()

        class FailedCorrelationCommit:
            def __init__(self, db: Any) -> None:
                self._db = db
                self._gets = 0
                self._fail_first_commit = True
                self._fallback_paused = False

            def __getattr__(self, name: str) -> Any:
                return getattr(self._db, name)

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                self._gets += 1
                row = await self._db.get(*args, **kwargs)
                if self._gets == 2 and not self._fallback_paused:
                    self._fallback_paused = True
                    fallback_read.set()
                    await release.wait()
                return row

            async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                values = getattr(statement, "_values", {})
                writes_correlation = any(
                    getattr(column, "key", None) == "thread_id" for column in values
                )
                if (
                    isinstance(statement, Update)
                    and not writes_correlation
                    and not self._fallback_paused
                ):
                    self._fallback_paused = True
                    fallback_read.set()
                    await release.wait()
                return await self._db.execute(statement, *args, **kwargs)

            async def commit(self) -> None:
                if self._fail_first_commit:
                    self._fail_first_commit = False
                    raise IntegrityError("UPDATE", {}, _ThreadForeignKeyViolation())
                await self._db.commit()

        delayed = asyncio.create_task(
            svc.upsert_run(
                FailedCorrelationCommit(stale_db),
                job_id=job_id,
                status=JobStatus.AWAITING_CONFIRMATION,
                thread_id=uuid.uuid4(),
            )
        )
        await asyncio.wait_for(fallback_read.wait(), timeout=2)
        await svc.upsert_run(finisher_db, job_id=job_id, status=JobStatus.COMPLETED)
        release.set()
        await delayed

    async with session_factory() as verify:
        run = await verify.get(AgentRun, job_id)
        assert run is not None
        assert run.status == JobStatus.COMPLETED.value
        assert run.user_id == USER_A
        assert run.cancel_requested_at is None


# ---------------------------------------------------------------------------
# get_run — tenancy
# ---------------------------------------------------------------------------


async def test_get_run_filters_org_and_user(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.RUNNING,
            organization_id=ORG_A,
            user_id=USER_A,
        )

        found = await svc.get_run(db, job_id, organization_id=ORG_A, user_id=USER_A)
        assert found is not None and found.job_id == job_id

        # Wrong org, wrong user, or an org-less caller → fail closed.
        assert (
            await svc.get_run(db, job_id, organization_id=ORG_B, user_id=USER_A) is None
        )
        assert (
            await svc.get_run(db, job_id, organization_id=ORG_A, user_id=USER_B) is None
        )
        assert (
            await svc.get_run(db, job_id, organization_id=None, user_id=USER_A) is None
        )


async def test_confirmation_claim_races_cancellation_on_durable_status(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.AWAITING_CONFIRMATION,
            organization_id=ORG_A,
            user_id=USER_A,
        )

        assert await svc.claim_awaiting_run_for_confirmation(
            db,
            job_id,
            organization_id=ORG_A,
            user_id=USER_A,
        )
        assert not await svc.claim_awaiting_run_for_confirmation(
            db,
            job_id,
            organization_id=ORG_A,
            user_id=USER_A,
        )

        assert await svc.release_confirmation_claim(
            db,
            job_id,
            organization_id=ORG_A,
            user_id=USER_A,
        )
        run = await svc.get_run(db, job_id, organization_id=ORG_A, user_id=USER_A)
        assert run is not None and run.status == JobStatus.AWAITING_CONFIRMATION.value


async def test_get_active_run_for_thread_is_tenant_scoped_and_non_terminal(
    session_factory,
):
    job_id = _job_id()
    thread_id = uuid.uuid4()
    async with session_factory() as db:
        await svc.upsert_run(
            db,
            job_id=job_id,
            status=JobStatus.AWAITING_CONFIRMATION,
            organization_id=ORG_A,
            user_id=USER_A,
            thread_id=str(thread_id),
        )

        owned = await svc.get_active_run_for_thread(
            db,
            thread_id,
            organization_id=ORG_A,
            user_id=USER_A,
        )
        assert owned is not None
        assert owned.job_id == job_id
        assert (
            await svc.get_active_run_for_thread(
                db,
                thread_id,
                organization_id=ORG_B,
                user_id=USER_A,
            )
            is None
        )

        await svc.upsert_run(db, job_id=job_id, status=JobStatus.COMPLETED)
        assert (
            await svc.get_active_run_for_thread(
                db,
                thread_id,
                organization_id=ORG_A,
                user_id=USER_A,
            )
            is None
        )


async def test_get_run_orgless_caller_matches_only_orgless_row(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.RUNNING, user_id=USER_A
        )
        found = await svc.get_run(db, job_id, organization_id=None, user_id=USER_A)
        assert found is not None
        assert (
            await svc.get_run(db, job_id, organization_id=ORG_A, user_id=USER_A) is None
        )


# ---------------------------------------------------------------------------
# claim_lease / release_lease / list_stale_runs (sweeper API)
# ---------------------------------------------------------------------------


async def test_claim_lease_is_exclusive_and_reentrant(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.RUNNING, user_id=USER_A
        )

        assert await svc.claim_lease(db, job_id, lease_owner="sweeper-1")
        # A rival cannot steal a live lease…
        assert not await svc.claim_lease(db, job_id, lease_owner="sweeper-2")
        # …but the holder can renew (heartbeat).
        assert await svc.claim_lease(db, job_id, lease_owner="sweeper-1")


async def test_claim_lease_takes_over_expired_lease(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.RUNNING, user_id=USER_A
        )
        # Lease that expired in the past.
        assert await svc.claim_lease(
            db, job_id, lease_owner="dead-worker", lease_seconds=-5
        )
        assert await svc.claim_lease(db, job_id, lease_owner="sweeper-2")
        run = await db.get(AgentRun, job_id)
        assert run.lease_owner == "sweeper-2"


async def test_claim_lease_missing_row_is_false(session_factory):
    async with session_factory() as db:
        assert not await svc.claim_lease(db, _job_id(), lease_owner="s")


async def test_release_lease_only_for_holder(session_factory):
    job_id = _job_id()
    async with session_factory() as db:
        await svc.upsert_run(
            db, job_id=job_id, status=JobStatus.RUNNING, user_id=USER_A
        )
        await svc.claim_lease(db, job_id, lease_owner="sweeper-1")

        assert not await svc.release_lease(db, job_id, lease_owner="intruder")
        assert await svc.release_lease(db, job_id, lease_owner="sweeper-1")
        run = await db.get(AgentRun, job_id)
        assert run.lease_owner is None and run.lease_expires_at is None


async def test_list_stale_runs_selects_sweepable_candidates(session_factory):
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        stale_running = _job_id()
        await svc.upsert_run(
            db, job_id=stale_running, status=JobStatus.RUNNING, user_id=USER_A
        )
        terminal = _job_id()
        await svc.upsert_run(
            db, job_id=terminal, status=JobStatus.COMPLETED, user_id=USER_A
        )
        leased = _job_id()
        await svc.upsert_run(
            db, job_id=leased, status=JobStatus.RUNNING, user_id=USER_A
        )
        await svc.claim_lease(db, leased, lease_owner="other", lease_seconds=600)

        # Everything above was just written, so with a future cutoff the
        # stale + leased rows are "old enough"; only lease/status filter out.
        cutoff = now + timedelta(hours=1)
        found = {r.job_id for r in await svc.list_stale_runs(db, updated_before=cutoff)}
        assert stale_running in found
        assert terminal not in found  # terminal — nothing to sweep
        assert leased not in found  # live lease — another worker owns it

        # A recent-only cutoff excludes even the running row.
        past_cutoff = now - timedelta(hours=1)
        assert await svc.list_stale_runs(db, updated_before=past_cutoff) == []


# ---------------------------------------------------------------------------
# record_job_status — the write-through entry point
# ---------------------------------------------------------------------------


async def test_record_job_status_projects_job_store_payload(session_factory):
    job_id = _job_id()
    nested_thread = uuid.uuid4()
    payload = {
        "status": "completed",
        "user_id": str(USER_A),
        "organization_id": str(ORG_A),
        "thread_id": None,
        "request": {"thread_id": str(nested_thread)},
        "error": None,
    }
    with patch("src.core.database.AsyncSessionLocal", session_factory):
        decision = await svc.record_job_status(job_id, payload)

    assert decision is not None
    assert decision.job_id == job_id
    assert decision.requested_status is JobStatus.COMPLETED
    assert decision.effective_status is JobStatus.COMPLETED
    assert decision.requested_status_won is True
    assert decision.thread_id == str(nested_thread)
    with pytest.raises(FrozenInstanceError):
        setattr(decision, "effective_status", JobStatus.FAILED)

    async with session_factory() as db:
        run = await db.get(AgentRun, job_id)
        assert run is not None
        assert run.status == "completed"
        assert run.organization_id == ORG_A
        assert run.thread_id == nested_thread  # extracted from nested request


async def test_record_job_status_never_raises(session_factory):
    """Postgres failure is swallowed — Redis stays authoritative."""

    def _boom(*a, **k):
        raise RuntimeError("db down")

    with patch("src.core.database.AsyncSessionLocal", _boom):
        await svc.record_job_status(_job_id(), {"status": "running", "user_id": "u"})


async def test_record_job_status_can_fail_closed_for_terminal_publication(
    session_factory,
):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    with (
        patch("src.core.database.AsyncSessionLocal", _boom),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await svc.record_job_status(
            _job_id(),
            {"status": "completed", "user_id": "u"},
            raise_on_error=True,
        )


async def test_record_job_status_ignores_statusless_payload(session_factory):
    with patch("src.core.database.AsyncSessionLocal", session_factory):
        await svc.record_job_status(_job_id(), {"user_id": str(USER_A)})
    async with session_factory() as db:
        assert (await db.execute(AgentRun.__table__.select())).first() is None
