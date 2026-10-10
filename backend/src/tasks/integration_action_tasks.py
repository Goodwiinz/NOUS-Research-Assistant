"""Integration action beat tasks: run approved actions, expire uncertain ones."""

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app

# A drain stops starting rows DRAIN_BUDGET (240 s) into its run, and starts an
# ingest only while its DETACHED_TIMEOUT (120 s) still fits (both in
# services/agent/tool_actions.py). These limits sit above that budget, so
# Celery never cuts short an ingest the drain had time for.
_DRAIN_SOFT_TIME_LIMIT_SECONDS = 300
_DRAIN_TIME_LIMIT_SECONDS = 360


async def _drain() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.agent.tool_actions import drain_integration_actions

    async with AsyncSessionLocal() as db:
        return await drain_integration_actions(db)


async def _sweep() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.agent.tool_actions import sweep_stale_actions

    async with AsyncSessionLocal() as db:
        return await sweep_stale_actions(db)


@celery_app.task(
    name="src.tasks.integration_action_tasks.drain_integration_actions",
    soft_time_limit=_DRAIN_SOFT_TIME_LIMIT_SECONDS,
    time_limit=_DRAIN_TIME_LIMIT_SECONDS,
)
def drain_integration_actions() -> int:
    return run_async(_drain())


@celery_app.task(name="src.tasks.integration_action_tasks.sweep_stale_actions")
def sweep_stale_actions() -> int:
    """Beat entry point; flag-gated like the other sweepers."""
    from src.core.config import settings

    if not settings.SWEEPERS_ENABLED:
        return 0
    return run_async(_sweep())
