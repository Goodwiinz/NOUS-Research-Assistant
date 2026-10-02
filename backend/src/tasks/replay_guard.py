"""Atomic replay/idempotency claim for acks_late Celery redelivery.

``celery_app`` sets ``task_acks_late=True`` (``celery_app.py``), so a worker
killed mid-task — OOM eviction, HPA scale-down, pod restart — never acks its
message and the broker redelivers it. Redelivery can therefore hit a job that a
*previous* delivery already finished (terminal state), one that is *still*
running on another worker, or one whose worker died mid-run (a stale ``RUNNING``
row). ``claim_job_for_processing`` gives the document-processing tasks a single,
atomic decision so a replay never re-does finished work, never double-processes
a live run, and safely takes over an abandoned one.

No new infrastructure: the decision reads only the existing
``ProcessingJob.status`` + ``started_at`` columns, and the claim is serialized
with ``SELECT ... FOR UPDATE`` on the job row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.models.document import Document
from src.models.processing import JobStatus, ProcessingJob

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

    Re-reads the job under a row lock (``SELECT ... FOR UPDATE``) so concurrent
    redeliveries cannot both claim it, then:

    * the job or its document is soft-deleted -> skip; the user deleted it.
    * terminal (COMPLETED/FAILED/CANCELLED) -> skip; the work is already done.
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
    # Re-read under a row lock. In the same Session this returns the identity-map
    # instance (i.e. the passed ``job``) while still emitting FOR UPDATE, which
    # serializes the claim against other workers. FOR UPDATE is a harmless no-op
    # on SQLite (unit tests).
    #
    # ``populate_existing()`` is mandatory: without it SQLAlchemy hands back the
    # already-cached (and un-expired) identity-map instance and *discards* the
    # freshly-locked row's column values, so ``locked.status`` would reflect the
    # pre-lock in-memory state. Two concurrent redeliveries could then both see a
    # stale QUEUED after the other committed RUNNING and both claim the job —
    # the exact double-processing this guard exists to prevent. Forcing a refresh
    # of the locked row makes the status check see the just-locked DB state.
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
    if locked.is_deleted or (
        locked.document_id is not None
        and db.query(Document.id)
        .filter(Document.id == locked.document_id, Document.is_deleted == True)
        .first()
        is not None
    ):
        db.rollback()
        return ClaimResult(False, "deleted")

    if locked.status in _TERMINAL_STATUSES:
        db.rollback()
        return ClaimResult(False, "terminal")

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
