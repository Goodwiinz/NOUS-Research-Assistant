"""Research Engine step endpoints."""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.models.user import User
from src.schemas.research_engine import StepResponse
from src.services.research_engine.project_access import (
    ResearchAction,
    require_run,
    require_step,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/research-engine",
    tags=["research-engine"],
)


async def _verify_run_access(run_id: UUID, user_id: UUID, db: AsyncSession) -> None:
    """Verify user owns the project for this run via a single JOIN query."""
    await require_run(db, run_id, user_id, ResearchAction.VIEW)


@router.get(
    "/runs/{run_id}/steps",
    response_model=List[StepResponse],
)
async def list_steps(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StepResponse]:
    """List steps for a run, ordered by step_index."""
    await _verify_run_access(run_id, current_user.id, db)
    query = (
        select(ResearchStep)
        .where(
            ResearchStep.run_id == run_id,
            ResearchStep.is_deleted.is_(False),
        )
        .order_by(ResearchStep.step_index)
    )
    result = await db.execute(query)
    steps = result.scalars().all()
    return [StepResponse.model_validate(s) for s in steps]


@router.get(
    "/steps/{step_id}",
    response_model=StepResponse,
)
async def get_step(
    step_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StepResponse:
    """Get a single step with ownership verification in one query."""
    step = await require_step(db, step_id, current_user.id, ResearchAction.VIEW)
    return StepResponse.model_validate(step)
