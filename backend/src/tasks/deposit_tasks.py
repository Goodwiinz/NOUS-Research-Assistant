"""Archive deposit beat task (GOO-318): advance queued Zenodo sandbox
deposits one phase each. A no-op until the sandbox token and account label
are configured."""

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app


async def _drain() -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.research import deposit_service
    from src.services.research.archives import zenodo

    adapter = zenodo.from_settings()
    if adapter is None:
        return 0
    async with AsyncSessionLocal() as db:
        return await deposit_service.drain(db, adapter)


@celery_app.task(name="src.tasks.deposit_tasks.drain_deposits")
def drain_deposits() -> int:
    return run_async(_drain())
