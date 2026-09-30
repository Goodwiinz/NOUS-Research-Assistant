"""Artifact lifecycle beat tasks: announce committed versions, sweep reservations."""

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app


async def _drain() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.artifacts.lifecycle import drain_artifact_outbox

    async with AsyncSessionLocal() as db:
        return await drain_artifact_outbox(db)


async def _sweep() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.artifacts.lifecycle import sweep_artifact_uploads as sweep

    async with AsyncSessionLocal() as db:
        return await sweep(db)


@celery_app.task(name="src.tasks.artifact_tasks.drain_artifacts")
def drain_artifacts() -> int:
    return run_async(_drain())


@celery_app.task(name="src.tasks.artifact_tasks.sweep_artifact_uploads")
def sweep_artifact_uploads() -> int:
    return run_async(_sweep())
