"""Stage-write guard for the document-ingestion task.

``claim_job_for_processing`` decides once whether a worker may start. The
pipeline then runs for minutes; meanwhile the document can be deleted, the job
cancelled or swept FAILED, or a retry can start under a newer Celery task.
The worker still holds ORM objects loaded before any of that, and committing
them would write a cancelled job back to COMPLETED or insert live entities for
a deleted document.

``require_active_ingestion`` is called right before every stage commit. It
re-reads the rows from the database and raises ``ProcessingStopped`` unless
this worker's attempt is still the live one. With ``lock=True`` the rows stay
locked (Document -> ProcessingJob, the order deletion uses) until the caller
commits, so a cancellation cannot land between the check and the commit.
Parsing, model and network work belongs outside the lock.

``end_processing_job`` serves writers that end a job from outside the stage
loop (lost-job reconciler, ``on_failure``, cancel): under the same locks it
re-checks the job, then fails or cancels it and fails its ingestion document
in one transaction.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.models.document import Document, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob

# Statuses a worker can still move forward (``not ProcessingJob.is_finished``).
NON_TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING}
)
# ``ProcessingJob.is_active``: what POST /processing/jobs/{id}/cancel accepts.
ACTIVE_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING}
)
# ``not Document.is_processing_complete``: a document an ingestion may still end.
ACTIVE_DOCUMENT_STATUSES = (
    ProcessingStatus.PENDING,
    ProcessingStatus.PROCESSING,
    ProcessingStatus.RETRYING,
)


class ProcessingStopped(Exception):
    """This worker's ingestion attempt is no longer the live one.

    Not a failure: the job was cancelled, deleted, swept or superseded, and
    whoever did that already wrote its state. ``reason`` is logged and
    returned as the task's skip reason.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_active_ingestion(
    db: Session,
    job_id: str,
    *,
    expected_task_id: str,
    lock: bool = False,
) -> tuple[ProcessingJob, Document]:
    """Return the fresh ``(job, document)`` if this attempt may still write.

    With ``lock``, takes the Document -> ProcessingJob row locks BEFORE
    flushing. Flushing first would let an UPDATE of a job-only change grab the
    job lock ahead of the Document lock and deadlock with a delete that holds
    Document and waits for the job. The lock selects read ids only, so they
    leave the in-memory objects alone.

    Then flushes, because the session runs with ``autoflush=False`` and the
    refresh would otherwise discard the caller's pending stage changes. The
    flushed changes are only committed if the check passes. On
    ``ProcessingStopped`` the caller must roll back. The caller owns
    commit/rollback either way.
    """
    if lock:
        document_id = (
            db.query(ProcessingJob.document_id)
            .filter(ProcessingJob.id == job_id)
            .scalar()
        )
        db.query(Document.id).filter(Document.id == document_id).with_for_update().all()
        db.query(ProcessingJob.id).filter(
            ProcessingJob.id == job_id
        ).with_for_update().all()

    db.flush()

    job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.id == job_id)
        .populate_existing()
        .first()
    )
    if job is None:
        raise ProcessingStopped("missing")
    document = (
        db.query(Document)
        .filter(
            Document.id == job.document_id,
            Document.organization_id == job.organization_id,
        )
        .populate_existing()
        .first()
    )

    if job.is_deleted or document is None or document.is_deleted:
        raise ProcessingStopped("deleted")
    # A retry dispatches a new Celery task and its claim records that id, so a
    # different id means a newer attempt owns the job.
    if job.celery_task_id != expected_task_id:
        raise ProcessingStopped("superseded")
    if job.status == JobStatus.QUEUED:
        # ponytail: processing_service.queue_processing_job writes QUEUED +
        # this same task id AFTER send_task, so a fast claim can be overwritten.
        # Same id means that late producer write, not a cancel or retry: keep
        # running. Drop this once GOO-355 reserves QUEUED before publishing.
        job.status = JobStatus.RUNNING  # type: ignore[assignment]
    if job.status != JobStatus.RUNNING:
        raise ProcessingStopped(job.status.value)
    return job, document


def fail_document_unless_owned(
    document: Document | None,
    ended_job: ProcessingJob,
    locked_jobs: Iterable[ProcessingJob],
    message: str | None,
) -> bool:
    """FAIL the document of an ingestion job that just ended (GOO-359, IN-1).

    The caller holds the Document -> ProcessingJob locks and passes every
    locked job. The document is left alone unless ``ended_job`` is an
    ingestion job, the document is live and still pending/processing/retrying,
    and no other live ingestion job for it is still non-terminal.
    """
    if (
        ended_job.job_type != JobType.DOCUMENT_INGESTION
        or document is None
        or document.is_deleted
        or document.processing_status not in ACTIVE_DOCUMENT_STATUSES
    ):
        return False
    if any(
        other.id != ended_job.id
        and other.document_id == document.id
        and other.organization_id == document.organization_id
        and other.job_type == JobType.DOCUMENT_INGESTION
        and not other.is_deleted
        and other.status in NON_TERMINAL_JOB_STATUSES
        for other in locked_jobs
    ):
        return False
    document.update_processing_status(ProcessingStatus.FAILED, message)
    return True


def end_processing_job(
    db: Session,
    job_id: Any,
    *,
    message: str,
    error_type: str | None = None,
    cancel: bool = False,
    expected_statuses: Collection[JobStatus] = NON_TERMINAL_JOB_STATUSES,
    expected_task_ids: Collection[str | None] | None = None,
) -> bool:
    """End one job and, for ingestion, its document in the same transaction.

    Locks Document -> ProcessingJob (the job plus the document's other live
    ingestion jobs, in id order), the order claim, stage writes, deletion and
    the stuck sweep use, then re-checks the job on the locked row: it must be
    live, associated as first read, its status in ``expected_statuses`` and,
    when given, its ``celery_task_id`` in ``expected_task_ids``. Returns False
    without writing when a check fails: someone else already moved the job.
    On True the job is FAILED (CANCELLED with ``cancel``) and its ingestion
    document FAILED unless another active ingestion owns it. The caller
    commits or rolls back; these rows are reloaded, so it must not hold
    unflushed changes to them.
    """
    with db.no_autoflush:
        key = (
            db.query(
                ProcessingJob.id,
                ProcessingJob.document_id,
                ProcessingJob.organization_id,
            )
            .filter(ProcessingJob.id == job_id)
            .first()
        )
        if key is None:
            return False
        document = None
        scope = ProcessingJob.id == key.id
        if key.document_id is not None:
            document = (
                db.query(Document)
                .filter(
                    Document.id == key.document_id,
                    Document.organization_id == key.organization_id,
                )
                .populate_existing()
                .with_for_update()
                .first()
            )
            scope = or_(
                scope,
                and_(
                    ProcessingJob.document_id == key.document_id,
                    ProcessingJob.organization_id == key.organization_id,
                    ProcessingJob.job_type == JobType.DOCUMENT_INGESTION,
                    ProcessingJob.is_deleted == False,  # noqa: E712
                ),
            )
        jobs = (
            db.query(ProcessingJob)
            .filter(scope)
            .order_by(ProcessingJob.id)
            .populate_existing()
            .with_for_update()
            .all()
        )
    job = next((row for row in jobs if row.id == key.id), None)
    if (
        job is None
        or job.is_deleted
        or job.status not in expected_statuses
        or (
            expected_task_ids is not None
            and job.celery_task_id not in expected_task_ids
        )
        or job.document_id != key.document_id
        or job.organization_id != key.organization_id
    ):
        return False
    if cancel:
        job.cancel_job()
        job.error_message = message
    else:
        job.fail_job(message, error_type=error_type)
    fail_document_unless_owned(document, job, jobs, message)
    return True
