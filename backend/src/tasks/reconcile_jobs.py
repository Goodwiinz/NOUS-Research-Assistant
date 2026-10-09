"""Lost-job reconciler (PR 1, Task 1.4).

The post-commit enqueue helpers (``src.tasks.enqueue``) fire a job's Celery
dispatch on ``after_commit`` — durable-then-dispatch, closing the
worker-reads-before-commit race. The residual failure mode: the broker is down
in the narrow window *after* the row commits, so the dispatch never lands. The
row is durable but no worker ever sees it — ``status=PENDING`` /
``celery_task_id IS NULL`` forever.

This beat task recovers exactly that class. Every
``LOST_JOB_RECONCILE_AFTER_MINUTES`` (default 10, well under the 30-min
stuck-job sweep so recovery runs first) it selects PENDING, non-deleted,
never-dispatched (``celery_task_id IS NULL``) rows untouched for that long and
re-enqueues each the same way its origin site did (see ``_REENQUEUE_BY_JOB_TYPE``).
Each re-enqueue bumps the attempt counter (``retry_count`` — a never-started
job's counter is otherwise untouched, so there is no collision with the worker's
own retry bookkeeping); after ``LOST_JOB_RECONCILE_MAX_ATTEMPTS`` (default 3) the
job is marked FAILED instead.

Complements ``sweep_stuck_processing_jobs`` (which mark-fails jobs that *were*
dispatched but stalled mid-flight): this one re-drives jobs that were *never*
dispatched. Idempotent across an immediate double-run — a re-enqueue bumps
``updated_at``, dropping the row out of the stale window until it goes silent
again. Re-delivery to a worker is harmless: the processing tasks guard against
redelivery (they no-op a job already past PENDING). Tenant-agnostic system task.
Flag-gated by ``LOST_JOB_RECONCILER_ENABLED`` (independent kill switch).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Mapping

from src.core.database import SessionLocal
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks.celery_app import celery_app
from src.tasks.processing_lifecycle import end_processing_job

logger = logging.getLogger(__name__)

# job_type -> (registered task name, queue). ``None`` queue means default
# routing, matching a bare ``.delay()``. Only job types produced by a
# post-commit enqueue site can end up PENDING/celery_task_id=NULL (the sync
# enqueue sites mark-fail on broker error rather than leaving such a row), so
# this map is exactly those two origins:
#   documents.py / files.py -> process_document_ingestion (default queue)
#   research/projects.py     -> kg_extract_entities_job    (entity_processing)
# ENTITY_EXTRACTION maps to kg_extract_entities_job, not the generic
# extract_entities task, because the KG auto-population site is the only producer
# of never-dispatched ENTITY_EXTRACTION rows.
_REENQUEUE_BY_JOB_TYPE: dict[JobType, tuple[str, str | None]] = {
    JobType.DOCUMENT_INGESTION: ("process_document_ingestion", None),
    JobType.ENTITY_EXTRACTION: ("kg_extract_entities_job", "entity_processing"),
}


@celery_app.task(name="src.tasks.reconcile_jobs.reconcile_lost_processing_jobs")
def reconcile_lost_processing_jobs() -> Mapping[str, object]:
    """Beat task: re-enqueue processing_jobs whose post-commit dispatch was lost."""
    from src.core.config import get_settings

    settings_local = get_settings()
    if not settings_local.LOST_JOB_RECONCILER_ENABLED:
        logger.info(
            "reconcile_lost_processing_jobs: skipped (LOST_JOB_RECONCILER_ENABLED=false)"
        )
        return {"skipped": "reconciler-disabled"}

    max_attempts = settings_local.LOST_JOB_RECONCILE_MAX_ATTEMPTS
    limit = settings_local.LOST_JOB_RECONCILE_MAX_PER_RUN
    # Naive-UTC cutoff matches BaseModel's datetime.utcnow timestamps (and the
    # stuck-job sweep's convention).
    cutoff = datetime.utcnow() - timedelta(
        minutes=settings_local.LOST_JOB_RECONCILE_AFTER_MINUTES
    )

    summary: dict[str, int] = {"reenqueued": 0, "failed": 0, "skipped": 0}
    db = SessionLocal()
    try:
        lost_jobs = (
            db.query(ProcessingJob)
            .filter(
                ProcessingJob.status == JobStatus.PENDING,
                ProcessingJob.celery_task_id.is_(None),
                ProcessingJob.is_deleted == False,  # noqa: E712
                ProcessingJob.updated_at < cutoff,
            )
            .order_by(ProcessingJob.updated_at)
            .limit(limit)
            .all()
        )

        for job in lost_jobs:
            job_id = job.id
            job_type = job.job_type.value if job.job_type else "unknown"
            attempts = job.retry_count or 0

            if attempts >= max_attempts:
                # IN-1: lock Document -> job and re-check that this is still
                # the never-dispatched PENDING row selected above (a worker may
                # have claimed it since), then fail the job and its document
                # together. A bare UPDATE overwrote a fresh claim with FAILED.
                ended = end_processing_job(
                    db,
                    job_id,
                    message=(
                        f"Lost job: post-commit dispatch never reached a worker "
                        f"after {max_attempts} reconcile attempts"
                    ),
                    error_type="LostJobReconcileGaveUp",
                    expected_statuses=(JobStatus.PENDING,),
                    expected_task_ids=(None,),
                )
                if not ended:
                    db.rollback()
                    summary["skipped"] += 1
                    continue
                logger.warning(
                    "reconcile_lost_processing_jobs: gave up on job %s "
                    "(type=%s) after %s re-enqueue attempts — marked FAILED",
                    job_id,
                    job_type,
                    max_attempts,
                )
                summary["failed"] += 1
                db.commit()
                continue

            mapping = _REENQUEUE_BY_JOB_TYPE.get(job.job_type)
            if mapping is None:
                logger.warning(
                    "reconcile_lost_processing_jobs: no re-enqueue mapping for "
                    "job %s (type=%s) — skipping",
                    job.id,
                    job_type,
                )
                summary["skipped"] += 1
                continue

            task_name, queue = mapping
            send_kwargs: dict[str, object] = {"args": [str(job.id)]}
            if queue:
                send_kwargs["queue"] = queue
            # A broker error here propagates to the outer handler (whole run
            # rolls back and retries next tick) — expected: if the broker is
            # still down there is nothing to recover to.
            celery_app.send_task(task_name, **send_kwargs)

            job.retry_count = attempts + 1
            summary["reenqueued"] += 1
            logger.info(
                "reconcile_lost_processing_jobs: re-enqueued lost job %s "
                "(type=%s, task=%s, attempt=%s/%s)",
                job.id,
                job_type,
                task_name,
                job.retry_count,
                max_attempts,
            )
            # Commit per job: bumps updated_at (idempotency — the row drops out
            # of the stale window) and preserves progress across a crash.
            db.commit()

        logger.info("reconcile_lost_processing_jobs: %s", summary)
        return summary
    except Exception:
        db.rollback()
        logger.exception("reconcile_lost_processing_jobs failed")
        raise
    finally:
        db.close()
