"""Integration action beat tasks: run approved actions, expire uncertain ones."""

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app


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


@celery_app.task(name="src.tasks.integration_action_tasks.drain_integration_actions")
def drain_integration_actions() -> int:
    return run_async(_drain())


@celery_app.task(name="src.tasks.integration_action_tasks.sweep_stale_actions")
def sweep_stale_actions() -> int:
    return run_async(_sweep())
