"""Pipeline API endpoints for Research Pipeline wizard workflow.

Provides CRUD operations for pipeline state so users can resume
their research workflow across sessions.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models import User
from src.models.workspace import WorkspaceRole
from src.services.research.pipeline_service import PipelineService
from src.services.research_engine.project_access import ResearchAction, resolve_project

logger = get_logger()
router = APIRouter(
    prefix="/api/v1/research/projects",
    tags=["research-pipeline"],
)


# --- Schemas ---


class PipelineResponse(BaseModel):
    id: str
    project_id: str
    current_step: int
    completed_steps: List[int]
    skipped_steps: List[int]
    step_data: Dict[str, Any]
    invalidated_steps: List[int]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UpdatePipelineRequest(BaseModel):
    current_step: Optional[int] = Field(None, ge=0, le=4)
    completed_steps: Optional[List[int]] = None
    skipped_steps: Optional[List[int]] = None
    step_data: Optional[Dict[str, Any]] = None
    invalidated_steps: Optional[List[int]] = None


# --- Helpers ---


async def _ensure_project_access(
    project_id: UUID,
    current_user: User,
    db: AsyncSession,
    action: ResearchAction = ResearchAction.VIEW,
) -> None:
    """Apply the canonical project authorization and lifecycle boundary."""
    await resolve_project(db, project_id, current_user.id, action)


def _pipeline_to_response(pipeline) -> PipelineResponse:
    return PipelineResponse(
        id=str(pipeline.id),
        project_id=str(pipeline.project_id),
        current_step=pipeline.current_step,
        completed_steps=list(pipeline.completed_steps or []),
        skipped_steps=list(pipeline.skipped_steps or []),
        step_data=dict(pipeline.step_data or {}),
        invalidated_steps=list(pipeline.invalidated_steps or []),
        created_at=pipeline.created_at.isoformat() if pipeline.created_at else None,
        updated_at=pipeline.updated_at.isoformat() if pipeline.updated_at else None,
    )


# --- Endpoints ---


@router.get(
    "/{project_id}/pipeline",
    response_model=PipelineResponse,
)
async def get_pipeline(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get or create pipeline state for a project."""
    context = await resolve_project(db, project_id, current_user.id)
    can_create = (
        context.workspace_role
        in {WorkspaceRole.OWNER, WorkspaceRole.ADMIN, WorkspaceRole.EDITOR}
        and not context.workspace.is_archived
        and context.collection.research_status != "archived"
    )
    try:
        pipeline = await PipelineService.get_or_create(
            db, project_id, create_if_missing=can_create
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Pipeline not found") from exc
    await db.commit()
    return _pipeline_to_response(pipeline)


@router.patch(
    "/{project_id}/pipeline",
    response_model=PipelineResponse,
)
async def update_pipeline(
    project_id: UUID,
    body: UpdatePipelineRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update pipeline state (step navigation, completions, data)."""
    await _ensure_project_access(project_id, current_user, db, ResearchAction.EDIT)
    try:
        pipeline = await PipelineService.update_pipeline(
            db,
            project_id,
            current_step=body.current_step,
            completed_steps=body.completed_steps,
            skipped_steps=body.skipped_steps,
            step_data=body.step_data,
            invalidated_steps=body.invalidated_steps,
        )
        await db.commit()
        return _pipeline_to_response(pipeline)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/{project_id}/pipeline/reset",
    response_model=PipelineResponse,
)
async def reset_pipeline(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset pipeline to initial state (step 0, no completions)."""
    await _ensure_project_access(project_id, current_user, db, ResearchAction.EDIT)
    try:
        pipeline = await PipelineService.reset_pipeline(db, project_id)
        await db.commit()
        return _pipeline_to_response(pipeline)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
