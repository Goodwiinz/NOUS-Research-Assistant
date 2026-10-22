"""Lost-job reconciler — recovers processing_jobs whose post-commit Celery
dispatch never reached the broker.

The post-commit enqueue helpers (Task 1.2/1.3) fire ``.delay`` / ``.apply_async``
*after* the row commits. If the broker is down in that narrow window the job row
is durable but never dispatched, leaving ``status=PENDING`` /
``celery_task_id IS NULL`` forever (the stuck-job sweep only mark-fails; it never
re-enqueues).

Contract under test:

- selects only PENDING + celery_task_id NULL + non-deleted rows older than the
  configured window; re-enqueues each via ``send_task`` with the task name and
  queue its origin site used (DOCUMENT_INGESTION -> process_document_ingestion,
  default routing; ENTITY_EXTRACTION -> kg_extract_entities_job, entity_processing);
- bumps the attempt counter (retry_count) on each re-enqueue;
- after MAX_ATTEMPTS re-enqueues, marks the job FAILED instead of re-enqueuing;
- leaves QUEUED/RUNNING/terminal rows, dispatched (celery_task_id set) rows,
  recently-touched rows and soft-deleted rows untouched;
- is idempotent across an immediate double-run (a re-enqueue bumps updated_at,
  dropping the row out of the stale window);
- is flag-gated by LOST_JOB_RECONCILER_ENABLED.
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.unit


@pytest.fixture
def session_factory() -> Iterator[sessionmaker]:
    from src.models.processing import ProcessingJob

    engine = create_engine("sqlite:///:memory:")
    ProcessingJob.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


def _seed(
    factory: sessionmaker,
    *,
    status: object,
    job_type: object = None,
    celery_task_id: str | None = None,
    updated_at: datetime,
    is_deleted: bool = False,
    retry_count: int = 0,
) -> uuid.UUID:
    from src.models.processing import JobType, ProcessingJob

    job_id = uuid.uuid4()
    with factory() as db:
        db.add(
            ProcessingJob(
                id=job_id,
                job_type=job_type or JobType.DOCUMENT_INGESTION,
                status=status,
                organization_id=uuid.uuid4(),
                celery_task_id=celery_task_id,
                queue_name="document_processing",
                created_at=updated_at,
                updated_at=updated_at,
                is_deleted=is_deleted,
                retry_count=retry_count,
            )
        )
        db.commit()
    return job_id


def _settings(
    enabled: bool = True,
    after_minutes: int = 10,
    max_attempts: int = 3,
    max_per_run: int = 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        LOST_JOB_RECONCILER_ENABLED=enabled,
        LOST_JOB_RECONCILE_AFTER_MINUTES=after_minutes,
        LOST_JOB_RECONCILE_MAX_ATTEMPTS=max_attempts,
        LOST_JOB_RECONCILE_MAX_PER_RUN=max_per_run,
    )


def _run(
    factory: sessionmaker, settings: SimpleNamespace
) -> "tuple[dict[str, int], MagicMock]":
    from src.tasks import reconcile_jobs

    with (
        patch("src.tasks.reconcile_jobs.datetime", _FrozenDatetime),
        patch("src.tasks.reconcile_jobs.SessionLocal", factory),
        patch("src.core.config.get_settings", return_value=settings),
        patch.object(reconcile_jobs.celery_app, "send_task") as send_task,
    ):
        result = reconcile_jobs.reconcile_lost_processing_jobs()
    return result, send_task


NAIVE_NOW = datetime.utcnow()
OLD = NAIVE_NOW - timedelta(minutes=30)
RECENT = NAIVE_NOW - timedelta(minutes=2)


class _FrozenDatetime(datetime):
    """Pins the reconciler's cutoff clock to NAIVE_NOW.

    NAIVE_NOW is taken at import. Under xdist a test can run 8+ minutes after
    collection, which would push RECENT past the 10-minute cutoff and make the
    "recent" row look lost.
    """

    @classmethod
    def utcnow(cls) -> datetime:
        return NAIVE_NOW


def test_lost_document_job_reenqueued_with_default_routing(
    session_factory: sessionmaker,
) -> None:
    from src.models.processing import JobStatus, JobType, ProcessingJob

    job_id = _seed(
        session_factory,
        status=JobStatus.PENDING,
        job_type=JobType.DOCUMENT_INGESTION,
        updated_at=OLD,
    )

    result, send_task = _run(session_factory, _settings())

    assert result["reenqueued"] == 1
    # DOCUMENT_INGESTION originally used process_document_ingestion.delay() ->
    # default routing (no queue kwarg).
    send_task.assert_called_once_with("process_document_ingestion", args=[str(job_id)])
    with session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.status is JobStatus.PENDING  # still pending; worker flips it
        assert job.retry_count == 1


def test_lost_kg_job_reenqueued_on_entity_queue(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, JobType

    job_id = _seed(
        session_factory,
        status=JobStatus.PENDING,
        job_type=JobType.ENTITY_EXTRACTION,
        updated_at=OLD,
    )

    result, send_task = _run(session_factory, _settings())

    assert result["reenqueued"] == 1
    # projects.py used apply_async(queue="entity_processing").
    send_task.assert_called_once_with(
        "kg_extract_entities_job", args=[str(job_id)], queue="entity_processing"
    )


def test_ineligible_rows_untouched(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, ProcessingJob

    dispatched = _seed(
        session_factory,
        status=JobStatus.PENDING,
        celery_task_id="task-abc",  # already dispatched
        updated_at=OLD,
    )
    queued = _seed(session_factory, status=JobStatus.QUEUED, updated_at=OLD)
    running = _seed(session_factory, status=JobStatus.RUNNING, updated_at=OLD)
    completed = _seed(session_factory, status=JobStatus.COMPLETED, updated_at=OLD)
    recent = _seed(session_factory, status=JobStatus.PENDING, updated_at=RECENT)
    deleted = _seed(
        session_factory, status=JobStatus.PENDING, updated_at=OLD, is_deleted=True
    )

    result, send_task = _run(session_factory, _settings())

    assert result["reenqueued"] == 0
    send_task.assert_not_called()
    with session_factory() as db:
        assert db.get(ProcessingJob, dispatched).status is JobStatus.PENDING
        assert db.get(ProcessingJob, queued).status is JobStatus.QUEUED
        assert db.get(ProcessingJob, running).status is JobStatus.RUNNING
        assert db.get(ProcessingJob, completed).status is JobStatus.COMPLETED
        assert db.get(ProcessingJob, recent).retry_count == 0
        assert db.get(ProcessingJob, deleted).status is JobStatus.PENDING


def test_gives_up_and_fails_after_max_attempts(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, ProcessingJob

    exhausted = _seed(
        session_factory,
        status=JobStatus.PENDING,
        updated_at=OLD,
        retry_count=3,  # == MAX_ATTEMPTS
    )

    result, send_task = _run(session_factory, _settings(max_attempts=3))

    assert result["failed"] == 1
    assert result["reenqueued"] == 0
    send_task.assert_not_called()
    with session_factory() as db:
        job = db.get(ProcessingJob, exhausted)
        assert job.status is JobStatus.FAILED
        assert job.error_type == "LostJobReconcileGaveUp"
        assert job.completed_at is not None


def test_unknown_job_type_skipped_not_failed(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, JobType, ProcessingJob

    # CLEANUP has no re-enqueue mapping (never produced by a post-commit site).
    job_id = _seed(
        session_factory,
        status=JobStatus.PENDING,
        job_type=JobType.CLEANUP,
        updated_at=OLD,
    )

    result, send_task = _run(session_factory, _settings())

    assert result["reenqueued"] == 0
    assert result["skipped"] == 1
    send_task.assert_not_called()
    with session_factory() as db:
        # Left as-is (not failed): unknown recovery path, not a give-up.
        assert db.get(ProcessingJob, job_id).status is JobStatus.PENDING


def test_double_run_is_idempotent(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, ProcessingJob

    job_id = _seed(session_factory, status=JobStatus.PENDING, updated_at=OLD)

    result1, send1 = _run(session_factory, _settings())
    result2, send2 = _run(session_factory, _settings())

    assert result1["reenqueued"] == 1
    # Re-enqueue bumped updated_at, so the row is no longer in the stale window.
    assert result2["reenqueued"] == 0
    send1.assert_called_once()
    send2.assert_not_called()
    with session_factory() as db:
        assert db.get(ProcessingJob, job_id).retry_count == 1


def test_gated_by_flag(session_factory: sessionmaker) -> None:
    from src.models.processing import JobStatus, ProcessingJob

    job_id = _seed(session_factory, status=JobStatus.PENDING, updated_at=OLD)

    result, send_task = _run(session_factory, _settings(enabled=False))

    assert result == {"skipped": "reconciler-disabled"}
    send_task.assert_not_called()
    with session_factory() as db:
        assert db.get(ProcessingJob, job_id).retry_count == 0
