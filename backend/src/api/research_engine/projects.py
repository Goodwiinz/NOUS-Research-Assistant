"""Research Engine project endpoints."""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.collection import Collection
from src.models.research_project import ResearchProject
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.research_engine import ProjectCreate, ProjectLink, ProjectResponse
from src.services.research_engine.project_access import (
    ResearchAction,
    project_response,
    require_research_project,
    with_project_access,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/research-engine/projects",
    tags=["research-engine"],
)


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    body: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a new research project."""
    project_count = (
        await db.execute(
            select(func.count(ResearchProject.id)).where(
                ResearchProject.owner_id == current_user.id,
                ResearchProject.is_deleted.is_(False),
            )
        )
    ).scalar() or 0
    cap = getattr(settings, "MAX_PROJECTS_PER_WORKSPACE", 200)
    if project_count >= cap:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Project limit reached ({cap})",
        )

    collection = None
    if body.collection_id is not None:
        collection = (
            await db.execute(
                select(Collection)
                .join(Workspace, Workspace.id == Collection.workspace_id)
                .where(
                    Collection.id == body.collection_id,
                    Collection.is_deleted.is_(False),
                    Workspace.is_deleted.is_(False),
                    Workspace.owner_id == current_user.id,
                )
            )
        ).scalar_one_or_none()
        if collection is None:
            raise HTTPException(status_code=404, detail="Project not found")
    project = ResearchProject(
        collection_id=collection.id if collection else None,
        name=body.name,
        description=body.description,
        owner_id=current_user.id,
        settings=body.settings,
    )
    db.add(project)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Project is already linked"
        ) from None
    await db.refresh(project)
    return ProjectResponse.model_validate(project_response(project))


@router.get(
    "",
    response_model=List[ProjectResponse],
)
async def list_projects(
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProjectResponse]:
    """List research projects for the current user."""
    query = with_project_access(
        select(ResearchProject), current_user.id, ResearchAction.VIEW
    ).distinct()
    if status_filter:
        query = query.where(ResearchProject.status == status_filter)
    query = query.order_by(ResearchProject.updated_at.desc())

    result = await db.execute(query)
    projects = result.scalars().all()
    return [ProjectResponse.model_validate(project_response(p)) for p in projects]


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
)
async def get_project(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a single research project."""
    project = await require_research_project(
        db, project_id, current_user.id, ResearchAction.VIEW
    )
    return ProjectResponse.model_validate(project_response(project))


@router.patch("/{project_id}/collection", response_model=ProjectResponse)
async def link_project_collection(
    project_id: UUID,
    body: ProjectLink,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Owner-only, one-time explicit mapping for a historical engine project."""
    project = (
        await db.execute(
            select(ResearchProject)
            .where(
                ResearchProject.id == project_id,
                ResearchProject.owner_id == current_user.id,
                ResearchProject.is_deleted.is_(False),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    collection = (
        await db.execute(
            select(Collection)
            .join(Workspace, Workspace.id == Collection.workspace_id)
            .where(
                Collection.id == body.collection_id,
                Collection.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
                Workspace.owner_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if project is None or collection is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.collection_id == collection.id:
        return ProjectResponse.model_validate(project_response(project))
    if project.collection_id is not None:
        raise HTTPException(status_code=409, detail="Project is already linked")
    conflict = (
        await db.execute(
            select(ResearchProject.id).where(
                ResearchProject.collection_id == collection.id,
                ResearchProject.id != project.id,
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(status_code=409, detail="Project is already linked")
    project.collection_id = collection.id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Project is already linked"
        ) from None
    await db.refresh(project)
    return ProjectResponse.model_validate(project_response(project))
