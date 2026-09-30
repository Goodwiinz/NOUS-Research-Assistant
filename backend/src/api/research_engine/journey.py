"""Plan-to-write journey API (GOO-308).

Transport only: VIEW through ``resolve_project`` after the service opens one
read-only snapshot; the route never commits and records no ledger event.
"""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import JourneyResponse, JourneyStage
from src.services.research_engine import journey
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-journey"])
PROJECT = "/projects/{project_id}"


@router.get(PROJECT + "/journey", response_model=JourneyResponse)
async def journey_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JourneyResponse:
    """Stage facts and derived status; reports state, never enforces order."""
    user_id = cast(UUID, current_user.id)  # before the snapshot expires it
    await journey.begin_read_snapshot(db)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    stages = journey.derive_stages(await journey.facts(db, context))
    return JourneyResponse(
        stages=[
            JourneyStage(
                key=cast(journey.StageKey, s.key),
                status=s.status,
                facts=s.facts,
                blockers=s.blockers,
            )
            for s in stages
        ],
        current=cast(journey.StageKey | None, journey.current_stage(stages)),
    )
