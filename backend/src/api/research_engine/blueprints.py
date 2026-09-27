"""Research Engine blueprint endpoints."""

import logging
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.user import User
from src.schemas.research_engine import BlueprintCreate, BlueprintResponse
from src.services.research_engine.blueprints.loader import BlueprintLoader
from src.services.research_engine.project_access import (
    ResearchAction,
    require_blueprint,
    require_research_project,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/research-engine/blueprints",
    tags=["research-engine"],
)


@router.get(
    "/templates",
    response_model=List[Dict[str, Any]],
)
async def list_templates() -> List[Dict[str, Any]]:
    """List available YAML blueprint templates."""
    loader = BlueprintLoader()
    return loader.list_templates()


@router.post(
    "/projects/{project_id}",
    response_model=BlueprintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_blueprint(
    project_id: UUID,
    body: BlueprintCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlueprintResponse:
    """Create a blueprint for a project."""
    project = await require_research_project(
        db, project_id, current_user.id, ResearchAction.ADJUDICATE
    )

    blueprint = ResearchBlueprint(
        project_id=project.id,
        name=body.name,
        template_source=body.template_source,
        steps=[s.model_dump() for s in body.steps],
        parameters=body.parameters,
    )
    db.add(blueprint)
    await db.commit()
    await db.refresh(blueprint)
    return BlueprintResponse.model_validate(blueprint)


@router.get(
    "/{blueprint_id}",
    response_model=BlueprintResponse,
)
async def get_blueprint(
    blueprint_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlueprintResponse:
    """Get a single blueprint with ownership verification in one query."""
    blueprint = await require_blueprint(
        db, blueprint_id, current_user.id, ResearchAction.REVIEW
    )
    return BlueprintResponse.model_validate(blueprint)
