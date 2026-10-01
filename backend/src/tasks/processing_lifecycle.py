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
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.models.document import Document
from src.models.processing import JobStatus, ProcessingJob


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

    Flushes first: the session runs with ``autoflush=False``, and the refresh
    below would otherwise discard the caller's pending stage changes. The
    flushed changes are only committed if the check passes. On
    ``ProcessingStopped`` the caller must roll back. The caller owns
    commit/rollback either way.
    """
    db.flush()

    job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.id == job_id)
        .populate_existing()
        .first()
    )
    if job is None:
        raise ProcessingStopped("missing")

    document_query = (
        db.query(Document)
        .filter(
            Document.id == job.document_id,
            Document.organization_id == job.organization_id,
        )
        .populate_existing()
    )
    if lock:
        document_query = document_query.with_for_update()
        document = document_query.first()
        job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.id == job_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if job is None:
            raise ProcessingStopped("missing")
    else:
        document = document_query.first()

    if job.is_deleted or document is None or document.is_deleted:
        raise ProcessingStopped("deleted")
    # A retry dispatches a new Celery task and its claim records that id, so a
    # different id means a newer attempt owns the job.
    if job.celery_task_id != expected_task_id:
        raise ProcessingStopped("superseded")
    if job.status != JobStatus.RUNNING:
        raise ProcessingStopped(job.status.value)
    return job, document
