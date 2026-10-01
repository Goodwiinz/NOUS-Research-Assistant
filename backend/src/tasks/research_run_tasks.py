"""Sweeper for research runs wedged in RUNNING (R5-M17).

A worker death between the RUNNING transition and a terminal event left the
run bricked: no timeout existed, so it stayed RUNNING forever and could be
neither streamed nor resumed. The sweeper fails runs with no progress past a
threshold, mirroring sweep_stale_agent_runs.
"""

from datetime import datetime, timedelta
from typing import Any, Dict

import structlog

from src.core.database import SessionLocal
from src.models.research_run import ResearchRun, RunStatus
from src.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)

STALE_AFTER = timedelta(hours=2)


@celery_app.task(name="src.tasks.research_run_tasks.sweep_stale_research_runs")
def sweep_stale_research_runs() -> Dict[str, Any]:
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - STALE_AFTER
        # M2-review: conditional bulk UPDATE — a run reaching a terminal
        # state between selection and commit is never overwritten.
        from sqlalchemy import update as _sa_update

        result = db.execute(
            _sa_update(ResearchRun)
            .where(
                ResearchRun.status == RunStatus.RUNNING.value,
                ResearchRun.updated_at < cutoff,
            )
            .values(
                status=RunStatus.FAILED.value,
                error=(
                    "Swept as stale: RUNNING with no progress "
                    f"since {cutoff.isoformat()}"
                ),
            )
        )
        db.commit()
        swept = result.rowcount or 0
        if swept:
            logger.warning("sweep_stale_research_runs: failed %d wedged runs", swept)
        return {"swept": swept}
    except Exception:
        db.rollback()
        logger.exception("sweep_stale_research_runs failed")
        raise
    finally:
        db.close()


# --- GOO-313 fresh reruns --------------------------------------------------------


@celery_app.task(name="src.tasks.research_run_tasks.execute_experiment_rerun")
def execute_experiment_rerun(rerun_id: str, attempt: int) -> None:
    """One rerun attempt; enqueued after the admission or retry commit."""
    from uuid import UUID

    from src.core.database import AsyncSessionLocal
    from src.services.research_engine import rerun_service
    from src.tasks._async_utils import run_async

    run_async(rerun_service.execute_attempt(AsyncSessionLocal, UUID(rerun_id), attempt))


async def _sweep_reruns() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.research_engine import rerun_service

    async with AsyncSessionLocal() as db:
        return await rerun_service.sweep_expired(db)


@celery_app.task(name="src.tasks.research_run_tasks.sweep_expired_reruns")
def sweep_expired_reruns() -> int:
    """A rerun attempt whose lease expired without a terminal row (worker
    death) ends ``interrupted``, never ``running`` forever."""
    from src.tasks._async_utils import run_async

    swept = run_async(_sweep_reruns())
    if swept:
        logger.warning("sweep_expired_reruns: interrupted %d attempts", swept)
    return swept
