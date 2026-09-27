"""Canonical Collection-to-research-engine mapping and decision access."""

from enum import Enum
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import Collection
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole


class ResearchAction(str, Enum):
    VIEW = "view"
    REVIEW = "review"
    ADJUDICATE = "adjudicate"
    SUPERVISE = "supervise"


_ROLES = {
    ResearchAction.VIEW: set(WorkspaceRole),
    ResearchAction.REVIEW: {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
        WorkspaceRole.EDITOR,
    },
    ResearchAction.ADJUDICATE: {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    },
    ResearchAction.SUPERVISE: {WorkspaceRole.OWNER},
}


async def require_research_project(
    db: AsyncSession,
    collection_id: UUID,
    user_id: UUID,
    action: ResearchAction = ResearchAction.VIEW,
) -> ResearchProject:
    """Resolve the canonical Collection UUID and enforce an explicit member role."""
    query = with_project_access(select(ResearchProject), user_id, action).where(
        or_(
            ResearchProject.id == collection_id,
            ResearchProject.collection_id == collection_id,
        )
    )
    project = cast(ResearchProject | None, (await db.execute(query)).scalars().first())
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return project


def project_response(project: ResearchProject) -> dict:
    """Serialize the engine project UUID and optional canonical Collection UUID."""
    return {
        "id": project.id,
        "collection_id": project.collection_id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "settings": project.settings or {},
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _with_access_joins(query: Any, user_id: UUID) -> Any:
    return (
        query.outerjoin(Collection, Collection.id == ResearchProject.collection_id)
        .outerjoin(Workspace, Workspace.id == Collection.workspace_id)
        .join(User, User.id == user_id)
        .outerjoin(
            WorkspaceMember,
            and_(
                WorkspaceMember.workspace_id == Workspace.id,
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.is_deleted.is_(False),
            ),
        )
    )


def with_project_access(query: Any, user_id: UUID, action: ResearchAction) -> Any:
    """Apply the shared legacy-owner or mapped-workspace access policy."""
    allowed = _ROLES[action]
    return _with_access_joins(query, user_id).where(
        ResearchProject.is_deleted.is_(False),
        or_(
            and_(
                ResearchProject.collection_id.is_(None),
                ResearchProject.owner_id == user_id,
            ),
            and_(
                Collection.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
                or_(
                    Workspace.owner_id == user_id,
                    and_(
                        Workspace.organization_id.is_not(None),
                        User.organization_id == Workspace.organization_id,
                        WorkspaceMember.role.in_(allowed),
                    ),
                ),
            ),
        ),
    )


async def require_blueprint(
    db: AsyncSession, blueprint_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchBlueprint:
    query = with_project_access(
        select(ResearchBlueprint).join(
            ResearchProject, ResearchProject.id == ResearchBlueprint.project_id
        ),
        user_id,
        action,
    ).where(
        ResearchBlueprint.id == blueprint_id,
        ResearchBlueprint.is_deleted.is_(False),
    )
    blueprint = cast(
        ResearchBlueprint | None, (await db.execute(query)).scalars().first()
    )
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return blueprint


async def require_run(
    db: AsyncSession, run_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchRun:
    query = with_project_access(
        select(ResearchRun)
        .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
        .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id),
        user_id,
        action,
    ).where(
        ResearchRun.id == run_id,
        ResearchRun.is_deleted.is_(False),
        ResearchBlueprint.is_deleted.is_(False),
    )
    run = cast(ResearchRun | None, (await db.execute(query)).scalars().first())
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def require_step(
    db: AsyncSession, step_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchStep:
    query = with_project_access(
        select(ResearchStep)
        .join(ResearchRun, ResearchRun.id == ResearchStep.run_id)
        .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
        .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id),
        user_id,
        action,
    ).where(
        ResearchStep.id == step_id,
        ResearchStep.is_deleted.is_(False),
        ResearchRun.is_deleted.is_(False),
        ResearchBlueprint.is_deleted.is_(False),
    )
    step = cast(ResearchStep | None, (await db.execute(query)).scalars().first())
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    return step
