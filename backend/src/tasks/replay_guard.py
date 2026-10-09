"""Atomic replay/idempotency claim for acks_late Celery redelivery.

``celery_app`` sets ``task_acks_late=True`` (``celery_app.py``), so a worker
killed mid-task — OOM eviction, HPA scale-down, pod restart — never acks its
message and the broker redelivers it. Redelivery can therefore hit a job that a
*previous* delivery already finished (terminal state), one that is *still*
running on another worker, or one whose worker died mid-run (a stale ``RUNNING``
row). ``claim_job_for_processing`` gives the document-processing tasks a single,
atomic decision so a replay never re-does finished work, never double-processes
a live run, and safely takes over an abandoned one.

The decision uses existing document deletion and job lifecycle fields. Claims
lock Document -> ProcessingJob, matching cancellation and stage writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.models.document import Document
from src.models.processing import JobStatus, JobType, ProcessingJob

# A RUNNING job older than this is treated as abandoned (its worker died) and is
# reclaimed; a younger RUNNING job is assumed live and skipped so two workers
# never process the same document at once. The value sits above the Celery hard
# time limit (``task_time_limit`` = 600s in celery_app.py) and below the Redis
# broker visibility timeout (default 3600s), so it reliably separates a crashed
# run from a live one.
DEFAULT_STALE_RUNNING_SECONDS = 900

_TERMINAL_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
)


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an atomic job claim.

    ``proceed`` is True when the caller owns the job and must process it;
    ``reason`` names the branch that was taken ("claimed", "reclaimed_stale",
    "deleted", "terminal", "running", "missing") for logging + the task's skip response.
    """

    proceed: bool
    reason: str

    @property
    def skipped(self) -> bool:
        return not self.proceed


def _running_age_seconds(job: ProcessingJob) -> Optional[float]:
    """Seconds since ``job`` was marked RUNNING, or None if unknown.

    ``started_at`` is a ``DateTime(timezone=True)`` column but can round-trip as
    a naive value (e.g. SQLite); treat a naive value as UTC so the comparison is
    always well defined.
    """
    started = job.started_at
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def claim_job_for_processing(
    db: Session,
    job: ProcessingJob,
    *,
    worker_id: Optional[str] = None,
    celery_task_id: Optional[str] = None,
    stale_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
) -> ClaimResult:
    """Atomically decide whether the caller may process ``job``.

    Refreshes and locks Document -> ProcessingJob so cancellation and concurrent
    redeliveries cannot both own it, then:

    * the job or its document is soft-deleted -> skip; the user deleted it.
    * terminal (COMPLETED/FAILED/CANCELLED) -> skip; the work is already done.
    * missing/foreign document or changed association -> skip; ownership is invalid.
    * RUNNING younger than ``stale_after_seconds`` -> skip; assumed live on
      another worker.
    * RUNNING older than the threshold (or with no ``started_at``) -> reclaim;
      the previous worker died mid-run.
    * PENDING/QUEUED/RETRYING -> claim; a normal fresh start.

    On a claim/reclaim the job is moved to RUNNING via ``start_job`` and the
    transaction is committed before returning, so the caller can proceed
    immediately. On a skip the lock is released (rollback) without mutating the
    job. The passed ``job`` instance is the one mutated — callers keep using it.
    """
    # ``populate_existing()`` keeps both checks tied to the just-locked database
    # rows rather than cached live/QUEUED snapshots. PostgreSQL tests prove the
    # lock behavior; SQLite only exercises the decision branches.
    # Use the same Document -> ProcessingJob order as cancellation/deletion.
    # Suppress autoflush: the caller may hold a stale ORM snapshot, whose
    # pending changes must not take a job lock before the document lock.
    with db.no_autoflush:
        candidate = (
            db.query(ProcessingJob.document_id, ProcessingJob.organization_id)
            .filter(ProcessingJob.id == job.id)
            .first()
        )
        if candidate is None:
            db.rollback()
            return ClaimResult(False, "missing")
        document = None
        if candidate.document_id is not None:
            document = (
                db.query(Document)
                .filter(
                    Document.id == candidate.document_id,
                    Document.organization_id == candidate.organization_id,
                )
                .populate_existing()
                .with_for_update()
                .first()
            )
        locked = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.id == job.id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    if locked is None:
        db.rollback()
        return ClaimResult(False, "missing")

    # Deletion commits before broker revocation, which is best effort, so the
    # message can still arrive. A document deleted with cascade=False leaves its
    # job row live, hence the document check.
    if locked.is_deleted or (document is not None and document.is_deleted):
        db.rollback()
        return ClaimResult(False, "deleted")

    if locked.status in _TERMINAL_STATUSES:
        db.rollback()
        return ClaimResult(False, "terminal")

    if (
        locked.document_id != candidate.document_id
        or locked.organization_id != candidate.organization_id
        or (
            document is None
            and (
                locked.document_id is not None
                or locked.job_type == JobType.DOCUMENT_INGESTION
            )
        )
    ):
        db.rollback()
        return ClaimResult(False, "missing")

    if locked.status == JobStatus.RUNNING:
        age = _running_age_seconds(locked)
        if age is not None and age < stale_after_seconds:
            db.rollback()
            return ClaimResult(False, "running")
        locked.start_job(worker_id=worker_id, celery_task_id=celery_task_id)
        db.commit()
        return ClaimResult(True, "reclaimed_stale")

    # PENDING / QUEUED / RETRYING -> normal fresh start.
    locked.start_job(worker_id=worker_id, celery_task_id=celery_task_id)
    db.commit()
    return ClaimResult(True, "claimed")
