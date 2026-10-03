"""Scheduled search update beat task (GOO-319): claim every due fire, then
run each claimed or resumable execution. A no-op unless
``SEARCH_UPDATES_ENABLED``."""

import logging
from typing import Any
from uuid import UUID

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _tick() -> int:
    from src.api.research_engine.runs import _build_connectors
    from src.core.config import settings
    from src.core.database import AsyncSessionLocal
    from src.services.research_engine import search_update_service as service

    if not settings.SEARCH_UPDATES_ENABLED:
        return 0

    def connectors(organization_id: UUID | None) -> dict[str, Any]:
        org = None if organization_id is None else str(organization_id)
        return _build_connectors(org)

    done = 0
    async with AsyncSessionLocal() as db:
        claimed = await service.claim_due(db)
        pending = list(dict.fromkeys([*claimed, *await service.resumable(db)]))
        for execution_id in pending:
            try:
                await service.run_execution(db, execution_id, connectors)
                done += 1
            except Exception as error:  # noqa: BLE001 - keep running the others
                # The started attempt stays; it goes stale and is retried.
                logger.warning("scheduled search execution failed", exc_info=error)
                await db.rollback()
    return done


@celery_app.task(name="src.tasks.search_update_tasks.tick")
def tick() -> int:
    return run_async(_tick())
