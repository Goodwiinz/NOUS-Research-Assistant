"""P1.4 sweepers — stale agent_runs + stuck processing_jobs (X1 recovery, D7).

Contract under test:

- stale non-terminal agent runs (no status write past the threshold, no live
  lease) are marked FAILED with an explicit sweep error; terminal rows and
  recently-updated rows are untouched; live-leased rows belong to another
  worker; awaiting_confirmation gets the longer confirmable window; a run the
  LIVE job store says finished is repaired, never failed.
- stuck non-terminal processing_jobs rows past the threshold are failed;
  terminal / recent / soft-deleted rows are untouched.
- both sweepers are flag-gated by SWEEPERS_ENABLED.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.services.agent.run_event_store import append_event, has_terminal_event
from src.services.agent.run_event_types import RunEventType
from src.shared.enums import AgentOutboxStatus, JobStatus
from src.tasks import agent_run_tasks as agent_tasks

pytestmark = pytest.mark.unit

NOW = datetime.now(timezone.utc)
STALE = NOW - timedelta(hours=1)  # past the 30-min default
FRESH = NOW - timedelta(minutes=5)
VERY_STALE = NOW - timedelta(hours=3)  # past the 2h awaiting window


# ---------------------------------------------------------------------------
# sweep_stale_agent_runs
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentRunEvent.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(
    session_factory,
    *,
    status: str,
    updated_at: datetime,
    user_id: Optional[uuid.UUID] = None,
    lease_owner=None,
    lease_expires_at=None,
    cancel_requested_at: Optional[datetime] = None,
) -> str:
    job_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(
            AgentRun(
                job_id=job_id,
                user_id=user_id or uuid.uuid4(),
                status=status,
                created_at=updated_at,
                updated_at=updated_at,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                cancel_requested_at=cancel_requested_at,
            )
        )
        await db.commit()
    return job_id


async def _status(session_factory, job_id):
    async with session_factory() as db:
        run = await db.get(AgentRun, job_id)
        return run.status, run.error


async def _lease(session_factory, job_id):
    async with session_factory() as db:
        run = await db.get(AgentRun, job_id)
        return run.lease_owner, run.lease_expires_at


def _sweep(session_factory, get_job_fresh=None, set_job=None):
    return (
        patch("src.core.database.AsyncSessionLocal", session_factory),
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=get_job_fresh or AsyncMock(return_value=None),
        ),
        patch("src.services.agent.job_store.set_job", new=set_job or AsyncMock()),
    )


@pytest.mark.asyncio
async def test_stale_running_run_is_failed(session_factory):
    stale_id = await _seed(session_factory, status="running", updated_at=STALE)
    set_job = AsyncMock()
    p1, p2, p3 = _sweep(session_factory, set_job=set_job)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result["failed"] == 1
    status, error = await _status(session_factory, stale_id)
    assert status == "failed"
    assert "Swept as stale" in error
    # No live job record existed → nothing to write to the live store.
    set_job.assert_not_awaited()


async def _seed_stream_submission(
    session_factory, *, status: str, cancel_requested_at: Optional[datetime] = None
) -> str:
    """A ``/stream`` run as accept_submission leaves it: open ledger + intent."""
    job_id = await _seed(session_factory, status=status, updated_at=STALE)
    async with session_factory() as db:
        await append_event(
            db,
            run_id=job_id,
            event_type=RunEventType.RUN_CREATED,
            payload={},
            organization_id=None,
        )
        db.add(
            AgentOutbox(
                run_id=job_id,
                kind="agent.stream.execute",
                payload={},
                status=AgentOutboxStatus.PENDING.value,
            )
        )
        # The ledger append bumps updated_at; age the row back past the cutoff.
        await db.execute(
            update(AgentRun)
            .where(AgentRun.job_id == job_id)
            .values(updated_at=STALE, cancel_requested_at=cancel_requested_at)
        )
        await db.commit()
    return job_id


async def _ledger_and_outbox(session_factory, job_id):
    async with session_factory() as db:
        closed = await has_terminal_event(db, job_id)
        outbox = (
            await db.execute(
                select(AgentOutbox.status).where(AgentOutbox.run_id == job_id)
            )
        ).scalar_one()
        return closed, outbox


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_status", "cancel_marker", "expected_status", "expected_event"),
    [
        ("queued", None, "failed", RunEventType.RUN_FAILED.value),
        ("running", STALE, "cancelled", RunEventType.RUN_CANCELLED.value),
    ],
)
async def test_swept_run_closes_its_ledger_and_retires_its_outbox(
    session_factory, seed_status, cancel_marker, expected_status, expected_event
):
    """R8-C4: a sweep terminalization is a full terminalization.

    Status, terminal ledger event and outbox retirement commit together, the
    same contract ``fail_queued_submission`` keeps — otherwise the ledger
    stays open (artifact announcements keep appending to a dead run) and a
    future relay sees live dispatch intent for a failed run.

    Mutation check: drop the outbox UPDATE and ``append_event`` from
    ``agent_run_service.terminalize_stale_run`` and
    ``pytest -q backend/tests/unit/tasks/test_sweepers.py -k closes_its_ledger``
    fails on ``assert (False, 'pending') == (True, 'failed')``.
    """
    job_id = await _seed_stream_submission(
        session_factory, status=seed_status, cancel_requested_at=cancel_marker
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert (await _status(session_factory, job_id))[0] == expected_status
    assert await _ledger_and_outbox(session_factory, job_id) == (True, "failed")
    async with session_factory() as db:
        events = (
            (
                await db.execute(
                    select(AgentRunEvent.event_type)
                    .where(AgentRunEvent.run_id == job_id)
                    .order_by(AgentRunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert events == [RunEventType.RUN_CREATED.value, expected_event]


@pytest.mark.asyncio
async def test_terminal_and_recent_rows_untouched(session_factory):
    done_id = await _seed(session_factory, status="completed", updated_at=VERY_STALE)
    failed_id = await _seed(session_factory, status="failed", updated_at=VERY_STALE)
    fresh_id = await _seed(session_factory, status="running", updated_at=FRESH)

    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result["failed"] == 0
    assert (await _status(session_factory, done_id))[0] == "completed"
    assert (await _status(session_factory, failed_id))[0] == "failed"
    assert (await _status(session_factory, fresh_id))[0] == "running"


@pytest.mark.asyncio
async def test_live_leased_row_belongs_to_its_worker(session_factory):
    leased_id = await _seed(
        session_factory,
        status="running",
        updated_at=STALE,
        lease_owner="celery:other",
        # From the clock at run time, not the import-time NOW: the sweeper
        # compares against the real clock, and a full xdist run can start
        # this test more than 5 minutes after collection.
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result["failed"] == 0
    assert (await _status(session_factory, leased_id))[0] == "running"


@pytest.mark.asyncio
async def test_expired_lease_is_reapable(session_factory):
    """A crashed worker's expired execution lease must not protect the row."""
    dead_id = await _seed(
        session_factory,
        status="running",
        updated_at=STALE,
        lease_owner="celery:crashed",
        lease_expires_at=NOW - timedelta(minutes=20),
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result["failed"] == 1
    assert (await _status(session_factory, dead_id))[0] == "failed"


@pytest.mark.asyncio
async def test_expired_lease_old_stop_is_acknowledged_as_cancelled(
    session_factory: Any,
) -> None:
    stopped_id = await _seed(
        session_factory,
        status=JobStatus.STOPPING.value,
        updated_at=STALE,
        lease_owner="celery:crashed",
        lease_expires_at=NOW - timedelta(minutes=20),
        cancel_requested_at=STALE,
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    async with session_factory() as db:
        run = await db.get(AgentRun, stopped_id)
        assert run is not None
        assert run.status == JobStatus.CANCELLED.value
        assert agent_tasks._as_utc(run.cancel_requested_at) == STALE
        assert run.lease_owner is None
    assert result["cancelled"] == 1
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_stale_active_row_with_old_cancel_marker_is_acknowledged(
    session_factory: Any,
) -> None:
    marked_id = await _seed(
        session_factory,
        status=JobStatus.RUNNING.value,
        updated_at=STALE,
        cancel_requested_at=STALE,
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    async with session_factory() as db:
        run = await db.get(AgentRun, marked_id)
        assert run is not None
        assert run.status == JobStatus.CANCELLED.value
        assert agent_tasks._as_utc(run.cancel_requested_at) == STALE
        assert run.lease_owner is None
    assert result["cancelled"] == 1
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_fresh_stop_marker_keeps_sweeper_lease_without_ack(
    session_factory: Any,
) -> None:
    fresh_marker_id = await _seed(
        session_factory,
        status=JobStatus.RUNNING.value,
        updated_at=STALE,
        cancel_requested_at=FRESH,
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    async with session_factory() as db:
        run = await db.get(AgentRun, fresh_marker_id)
        assert run is not None
        assert run.status == JobStatus.RUNNING.value
        assert agent_tasks._as_utc(run.cancel_requested_at) == FRESH
        assert run.lease_owner == "sweeper:t"
    assert result["cancelled"] == 0
    assert result["failed"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_awaiting_confirmation_respects_longer_window(session_factory):
    parked_id = await _seed(
        session_factory, status="awaiting_confirmation", updated_at=STALE
    )
    expired_id = await _seed(
        session_factory, status="awaiting_confirmation", updated_at=VERY_STALE
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    # 1h-old parked confirm is still confirmable (Redis TTL 1h) → untouched.
    assert (await _status(session_factory, parked_id))[0] == "awaiting_confirmation"
    # 3h-old parked confirm can never be confirmed again → swept.
    assert (await _status(session_factory, expired_id))[0] == "failed"
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_live_store_terminal_repairs_instead_of_failing(session_factory):
    """Projection missed the terminal write (fire-and-forget lost): the run
    actually completed — repair the projection, don't fail a finished run."""
    owner_id = uuid.uuid4()
    lagged_id = await _seed(
        session_factory, status="running", updated_at=STALE, user_id=owner_id
    )
    live = {
        "status": "completed",
        "user_id": str(owner_id),
        "error": None,
        "result": {"message": "durable completion"},
    }
    set_job = AsyncMock()
    p1, p2, p3 = _sweep(
        session_factory,
        get_job_fresh=AsyncMock(return_value=live),
        set_job=set_job,
    )
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result == {
        "scanned": 1,
        "failed": 0,
        "repaired": 1,
        "skipped": 0,
        "cancelled": 0,
    }
    assert (await _status(session_factory, lagged_id))[0] == "completed"
    set_job.assert_awaited_once()
    mirrored = set_job.await_args.args[1]
    decision = set_job.await_args.kwargs["decision"]
    assert decision.job_id == lagged_id
    assert decision.effective_status is JobStatus.COMPLETED
    assert mirrored["result"] == live["result"]
    assert set_job.await_args.kwargs["project"] is False


@pytest.mark.asyncio
async def test_live_store_read_failure_keeps_sweeper_lease(session_factory):
    stale_id = await _seed(session_factory, status="running", updated_at=STALE)
    read_failed = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    p1, p2, p3 = _sweep(session_factory, get_job_fresh=read_failed)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result == {
        "scanned": 1,
        "failed": 0,
        "repaired": 0,
        "skipped": 1,
        "cancelled": 0,
    }
    assert (await _status(session_factory, stale_id))[0] == "running"
    owner, expires_at = await _lease(session_factory, stale_id)
    assert owner == "sweeper:t"
    assert expires_at is not None


@pytest.mark.asyncio
async def test_failure_is_pushed_to_live_store_when_record_exists(session_factory):
    """Pollers read Redis first — a swept run must stop them spinning."""
    owner_id = uuid.uuid4()
    stale_id = await _seed(
        session_factory, status="running", updated_at=STALE, user_id=owner_id
    )
    live = {
        "status": "running",
        "user_id": str(owner_id),
        "request": {"thread_id": None},
    }
    set_job = AsyncMock()
    p1, p2, p3 = _sweep(
        session_factory,
        get_job_fresh=AsyncMock(return_value=live),
        set_job=set_job,
    )
    with p1, p2, p3:
        await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert (await _status(session_factory, stale_id))[0] == "failed"
    written = set_job.await_args.args[1]
    assert written["status"] == JobStatus.FAILED
    assert "Swept as stale" in written["error"]
    assert written["user_id"] == str(owner_id)
    assert set_job.await_args.kwargs["decision"].effective_status is JobStatus.FAILED
    assert set_job.await_args.kwargs["project"] is False


def test_sweep_stale_agent_runs_gated_by_flag():
    with (
        patch.object(
            agent_tasks,
            "get_settings",
            return_value=SimpleNamespace(SWEEPERS_ENABLED=False),
        ),
        patch.object(agent_tasks, "run_async") as run_async_mock,
    ):
        result = agent_tasks.sweep_stale_agent_runs()
    assert result == {"skipped": "sweepers-disabled"}
    run_async_mock.assert_not_called()


# ---------------------------------------------------------------------------
# sweep_stuck_processing_jobs
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session_factory():
    from src.models.processing import ProcessingJob

    engine = create_engine("sqlite:///:memory:")
    ProcessingJob.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


def _seed_processing_job(factory, *, status, updated_at, is_deleted=False):
    from src.models.processing import JobType, ProcessingJob

    job_id = uuid.uuid4()
    with factory() as db:
        db.add(
            ProcessingJob(
                id=job_id,
                job_type=JobType.DOCUMENT_INGESTION,
                status=status,
                organization_id=uuid.uuid4(),
                created_at=updated_at,
                updated_at=updated_at,
                is_deleted=is_deleted,
            )
        )
        db.commit()
    return job_id


def _processing_settings(enabled=True, threshold=1800):
    return SimpleNamespace(
        SWEEPERS_ENABLED=enabled, PROCESSING_JOB_STUCK_AFTER_SECONDS=threshold
    )


def test_stuck_processing_jobs_swept_terminal_and_recent_untouched(
    sync_session_factory,
):
    from src.models.processing import JobStatus as PJStatus
    from src.models.processing import ProcessingJob
    from src.tasks.processing_tasks import sweep_stuck_processing_jobs

    naive_now = datetime.utcnow()
    old = naive_now - timedelta(hours=1)
    recent = naive_now - timedelta(minutes=5)

    stuck_ids = {
        _seed_processing_job(sync_session_factory, status=s, updated_at=old)
        for s in (
            PJStatus.PENDING,
            PJStatus.QUEUED,
            PJStatus.RUNNING,
            PJStatus.RETRYING,
        )
    }
    done_id = _seed_processing_job(
        sync_session_factory, status=PJStatus.COMPLETED, updated_at=old
    )
    fresh_id = _seed_processing_job(
        sync_session_factory, status=PJStatus.RUNNING, updated_at=recent
    )
    deleted_id = _seed_processing_job(
        sync_session_factory, status=PJStatus.RUNNING, updated_at=old, is_deleted=True
    )

    with (
        patch("src.tasks.processing_tasks.SessionLocal", sync_session_factory),
        patch("src.core.config.get_settings", return_value=_processing_settings()),
    ):
        result = sweep_stuck_processing_jobs()

    assert result == {"swept": 4}
    with sync_session_factory() as db:
        for job_id in stuck_ids:
            job = db.get(ProcessingJob, job_id)
            assert job.status is PJStatus.FAILED
            assert "Swept as stuck" in job.error_message
            assert job.error_type == "StuckJobSweep"
            assert job.completed_at is not None
        assert db.get(ProcessingJob, done_id).status is PJStatus.COMPLETED
        assert db.get(ProcessingJob, fresh_id).status is PJStatus.RUNNING
        assert db.get(ProcessingJob, deleted_id).status is PJStatus.RUNNING


def test_processing_sweeper_gated_by_flag(sync_session_factory):
    from src.models.processing import JobStatus as PJStatus
    from src.models.processing import ProcessingJob
    from src.tasks.processing_tasks import sweep_stuck_processing_jobs

    old = datetime.utcnow() - timedelta(hours=2)
    stuck_id = _seed_processing_job(
        sync_session_factory, status=PJStatus.RUNNING, updated_at=old
    )

    with (
        patch("src.tasks.processing_tasks.SessionLocal", sync_session_factory),
        patch(
            "src.core.config.get_settings",
            return_value=_processing_settings(enabled=False),
        ),
    ):
        result = sweep_stuck_processing_jobs()

    assert result == {"skipped": "sweepers-disabled"}
    with sync_session_factory() as db:
        assert db.get(ProcessingJob, stuck_id).status is PJStatus.RUNNING


# ---------------------------------------------------------------------------
# S2-M15: staleness floor must never undercut the live-run heartbeat margin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_after_below_heartbeat_margin_is_raised_to_floor(
    session_factory, monkeypatch
):
    """A misconfigured AGENT_RUN_STALE_AFTER_SECONDS smaller than the live-run
    heartbeat margin must not let the sweeper kill runs a live heartbeat keeps
    fresh — the effective cutoff is max(STALE_AFTER, 4x heartbeat)."""
    from types import SimpleNamespace

    sweep_now = datetime.now(timezone.utc)
    monkeypatch.setattr(agent_tasks, "_utcnow", lambda: sweep_now)

    # Updated 30s ago: past the (broken) 10s threshold, inside the 240s floor.
    recent_id = await _seed(
        session_factory,
        status="running",
        updated_at=sweep_now - timedelta(seconds=30),
    )
    monkeypatch.setattr(
        agent_tasks,
        "get_settings",
        lambda: SimpleNamespace(
            AGENT_RUN_STALE_AFTER_SECONDS=10,
            AGENT_RUN_STALE_AWAITING_AFTER_SECONDS=20,
            AGENT_RUN_HEARTBEAT_SECONDS=60,
        ),
    )
    p1, p2, p3 = _sweep(session_factory)
    with p1, p2, p3:
        result = await agent_tasks._sweep_stale_agent_runs(lease_owner="sweeper:t")

    assert result["failed"] == 0
    assert (await _status(session_factory, recent_id))[0] == "running"
