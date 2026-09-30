"""Database-backed dispatch and terminal-projection retry; safe across workers."""

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app


async def _tick() -> dict[str, int]:
    from src.core.database import AsyncSessionLocal
    from src.services.harness.delivery import dispatch_pending, reconcile_pending

    async with AsyncSessionLocal() as db:
        # Projection remains available when new dispatch is disabled.
        reconciled = await reconcile_pending(db)
        dispatched = await dispatch_pending(db)
        return {"dispatched": dispatched, "reconciled": reconciled}


@celery_app.task(name="src.tasks.harness_dispatch.dispatch_harness")
def dispatch_harness() -> dict[str, int]:
    return run_async(_tick())
