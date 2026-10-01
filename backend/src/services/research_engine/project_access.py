"""Canonical Collection access for research-engine resources."""

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from src.models.collection import Collection, CollectionDocument
from src.models.document import Document
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.threads import workspace_access


class ResearchAction(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    MANAGE = "manage"
    REVIEW = "review"
    ADJUDICATE = "adjudicate"
    SUPERVISE = "supervise"
    # GOO-307: promote a draft version to verified.
    RELEASE = "release"


# Any one of the roles suffices.
_DECISION_ROLE: dict[ResearchAction, frozenset[ResearchProjectRole]] = {
    ResearchAction.REVIEW: frozenset({ResearchProjectRole.REVIEWER}),
    ResearchAction.ADJUDICATE: frozenset({ResearchProjectRole.ADJUDICATOR}),
    ResearchAction.SUPERVISE: frozenset({ResearchProjectRole.SUPERVISOR}),
    ResearchAction.RELEASE: frozenset(
        {ResearchProjectRole.ADJUDICATOR, ResearchProjectRole.SUPERVISOR}
    ),
}
_MUTATING_ACTIONS = set(_DECISION_ROLE) | {ResearchAction.EDIT, ResearchAction.MANAGE}


@dataclass(frozen=True)
class ProjectContext:
    collection: Collection
    workspace: Workspace
    engine: Optional[ResearchProject]
    organization_id: Optional[UUID]
    effective_roles: FrozenSet[ResearchProjectRole]
    workspace_role: Optional[WorkspaceRole]


def _workspace_role(workspace: Workspace, user_id: UUID) -> Optional[WorkspaceRole]:
    if workspace.owner_id == user_id:
        return WorkspaceRole.OWNER
    for member in workspace.members:
        if not member.is_deleted and member.user_id == user_id:
            return cast(WorkspaceRole, member.role)
    return None


async def accessible_research_workspace_ids(
    db: AsyncSession, user_id: UUID
) -> list[UUID]:
    """Return private-artifact workspace ids in one same-organization query."""
    owner = aliased(User)
    actor = aliased(User)
    result = await db.execute(
        select(Workspace.id)
        .join(owner, owner.id == Workspace.owner_id)
        .join(actor, actor.id == user_id)
        .outerjoin(
            WorkspaceMember,
            and_(
                WorkspaceMember.workspace_id == Workspace.id,
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.is_deleted.is_(False),
            ),
        )
        .where(
            Workspace.is_deleted.is_(False),
            or_(Workspace.owner_id == user_id, WorkspaceMember.id.is_not(None)),
            func.coalesce(
                Workspace.organization_id, owner.organization_id
            ).is_not_distinct_from(actor.organization_id),
        )
        .distinct()
    )
    return list(result.scalars().all())


def project_documents_query(project_id: UUID) -> Select[tuple[Document]]:
    """Build the canonical, organization-scoped document query for a Collection."""
    owner = aliased(User)
    return (
        select(Document)
        .join(CollectionDocument, CollectionDocument.document_id == Document.id)
        .join(Collection, Collection.id == CollectionDocument.collection_id)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .join(owner, owner.id == Workspace.owner_id)
        .where(
            Collection.id == project_id,
            Collection.is_deleted.is_(False),
            CollectionDocument.is_deleted.is_(False),
            Workspace.is_deleted.is_(False),
            Document.is_deleted.is_(False),
            Document.organization_id
            == func.coalesce(Workspace.organization_id, owner.organization_id),
        )
    )


async def require_research_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    action: ResearchAction = ResearchAction.EDIT,
) -> Workspace:
    workspace = await workspace_access.get_workspace(
        db, workspace_id, user_id, load_conversations=False, load_collections=False
    )
    if workspace is None or _workspace_role(workspace, user_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    owner_org = cast(
        Optional[UUID],
        (
            await db.execute(
                select(User.organization_id).where(User.id == workspace.owner_id)
            )
        ).scalar_one_or_none(),
    )
    effective_org = workspace.organization_id or owner_org
    user_org = cast(
        Optional[UUID],
        (
            await db.execute(select(User.organization_id).where(User.id == user_id))
        ).scalar_one_or_none(),
    )
    if effective_org != user_org:
        raise HTTPException(status_code=404, detail="Workspace not found")
    role = _workspace_role(workspace, user_id)
    if action == ResearchAction.EDIT and role not in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
        WorkspaceRole.EDITOR,
    }:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if action == ResearchAction.MANAGE and role not in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    }:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if action != ResearchAction.VIEW and workspace.is_archived:
        raise HTTPException(status_code=409, detail="Archived workspaces are read-only")
    return workspace


async def lock_active_project(db: AsyncSession, project_id: UUID) -> None:
    """Serialize a project mutation against Collection lifecycle changes."""
    locked = (
        await db.execute(
            select(Collection.id)
            .join(Workspace, Workspace.id == Collection.workspace_id)
            .where(
                Collection.id == project_id,
                Collection.is_deleted.is_(False),
                Collection.research_status != "archived",
                Workspace.is_deleted.is_(False),
                Workspace.is_archived.is_(False),
            )
            .with_for_update(of=Collection)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=409, detail="Project is not writable")


async def resolve_project(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    action: ResearchAction = ResearchAction.VIEW,
    *,
    require_engine: bool = False,
) -> ProjectContext:
    """Resolve a canonical Collection id and enforce private artifact access."""
    collection = await workspace_access.get_collection(
        db, project_id, user_id, load_documents=False
    )
    if collection is None:
        raise HTTPException(status_code=404, detail="Project not found")
    workspace = collection.workspace
    if action in _MUTATING_ACTIONS:
        # Lock order: Workspace SHARE -> Collection UPDATE -> domain aggregate.
        # Membership/lifecycle writers take Workspace UPDATE. Distinct projects
        # can still mutate concurrently, while revocation cannot race a commit.
        await db.execute(
            select(Workspace.id)
            .where(Workspace.id == collection.workspace_id)
            .with_for_update(read=True, of=Workspace)
        )
        try:
            await lock_active_project(db, project_id)
        except HTTPException:
            lifecycle = (
                await db.execute(
                    select(Collection, Workspace)
                    .join(Workspace, Workspace.id == Collection.workspace_id)
                    .where(Collection.id == project_id)
                    .execution_options(populate_existing=True)
                )
            ).first()
            if lifecycle is None or lifecycle[0].is_deleted or lifecycle[1].is_deleted:
                raise HTTPException(status_code=404, detail="Project not found")
            raise

        lifecycle = (
            await db.execute(
                select(Collection, Workspace)
                .join(Workspace, Workspace.id == Collection.workspace_id)
                .where(Collection.id == project_id)
                .options(selectinload(Workspace.members))
                .execution_options(populate_existing=True)
            )
        ).first()
        if lifecycle is None or lifecycle[0].is_deleted or lifecycle[1].is_deleted:
            raise HTTPException(status_code=404, detail="Project not found")
        collection = cast(Collection, lifecycle[0])
        workspace = cast(Workspace, lifecycle[1])
        if workspace.is_archived or collection.research_status == "archived":
            raise HTTPException(
                status_code=409, detail="Archived projects are read-only"
            )

    workspace_role = _workspace_role(workspace, user_id)
    if workspace_role is None:  # Public visibility is insufficient for artifacts.
        raise HTTPException(status_code=404, detail="Project not found")

    organization_id = workspace.organization_id
    if organization_id is None:
        organization_id = cast(
            Optional[UUID],
            (
                await db.execute(
                    select(User.organization_id).where(User.id == workspace.owner_id)
                )
            ).scalar_one_or_none(),
        )
    user_org = cast(
        Optional[UUID],
        (
            await db.execute(select(User.organization_id).where(User.id == user_id))
        ).scalar_one_or_none(),
    )
    if user_org != organization_id:
        raise HTTPException(status_code=404, detail="Project not found")

    roles = frozenset(
        cast(
            list[ResearchProjectRole],
            (
                await db.execute(
                    select(ResearchProjectRoleAssignment.role).where(
                        ResearchProjectRoleAssignment.collection_id == collection.id,
                        ResearchProjectRoleAssignment.user_id == user_id,
                        ResearchProjectRoleAssignment.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all(),
        )
    )
    if action == ResearchAction.EDIT and workspace_role not in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
        WorkspaceRole.EDITOR,
    }:
        raise HTTPException(status_code=404, detail="Project not found")
    if action == ResearchAction.MANAGE and workspace_role not in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    }:
        raise HTTPException(status_code=404, detail="Project not found")
    required = _DECISION_ROLE.get(action)
    if required is not None and required.isdisjoint(roles):
        names = " or ".join(sorted(role.value for role in required))
        raise HTTPException(status_code=403, detail=f"{names} role required")

    engine = cast(
        Optional[ResearchProject],
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.collection_id == collection.id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first(),
    )
    if require_engine and engine is None:
        raise HTTPException(status_code=409, detail="mapping_required")
    return ProjectContext(
        collection, workspace, engine, organization_id, roles, workspace_role
    )


async def require_legacy_project(
    db: AsyncSession, engine_id: UUID, user_id: UUID
) -> ResearchProject:
    project = cast(
        Optional[ResearchProject],
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.id == engine_id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first(),
    )
    if project is None or (
        project.collection_id is None and project.owner_id != user_id
    ):
        raise HTTPException(status_code=404, detail="Project not found")
    if project.collection_id is not None:
        context = await resolve_project(
            db,
            project.collection_id,
            user_id,
            ResearchAction.VIEW,
            require_engine=True,
        )
        assert context.engine is not None
        return context.engine
    return project


async def require_research_project(
    db: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    action: ResearchAction = ResearchAction.VIEW,
) -> ResearchProject:
    context = await resolve_project(
        db, project_id, user_id, action, require_engine=True
    )
    assert context.engine is not None
    return context.engine


def project_response(
    context: ProjectContext, blueprint_id: Optional[UUID] = None
) -> dict:
    engine = context.engine
    return {
        "id": context.collection.id,
        "project_id": context.collection.id,
        "collection_id": context.collection.id,
        "research_engine_project_id": engine.id if engine else None,
        "blueprint_id": blueprint_id,
        "name": context.collection.name,
        "description": context.collection.description,
        "status": context.collection.research_status,
        "settings": engine.settings if engine and engine.settings else {},
        "created_at": context.collection.created_at,
        "updated_at": context.collection.updated_at,
    }


async def resolve_engine_project_context(
    db: AsyncSession, engine_id: UUID, user_id: UUID, action: ResearchAction
) -> ProjectContext:
    project = cast(
        Optional[ResearchProject],
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.id == engine_id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first(),
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.collection_id is None:
        if project.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Project not found")
        raise HTTPException(status_code=409, detail="mapping_required")
    return await resolve_project(
        db, project.collection_id, user_id, action, require_engine=True
    )


async def require_blueprint(
    db: AsyncSession, blueprint_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchBlueprint:
    blueprint = cast(
        Optional[ResearchBlueprint],
        (
            await db.execute(
                select(ResearchBlueprint).where(
                    ResearchBlueprint.id == blueprint_id,
                    ResearchBlueprint.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first(),
    )
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    await resolve_engine_project_context(db, blueprint.project_id, user_id, action)
    return blueprint


async def require_run(
    db: AsyncSession, run_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchRun:
    row = (
        await db.execute(
            select(ResearchRun, ResearchBlueprint.project_id)
            .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
            .where(
                ResearchRun.id == run_id,
                ResearchRun.is_deleted.is_(False),
                ResearchBlueprint.is_deleted.is_(False),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = cast(ResearchRun, row[0])
    engine_id = cast(UUID, row[1])
    await resolve_engine_project_context(db, engine_id, user_id, action)
    return run


async def require_step(
    db: AsyncSession, step_id: UUID, user_id: UUID, action: ResearchAction
) -> ResearchStep:
    row = (
        await db.execute(
            select(ResearchStep, ResearchBlueprint.project_id)
            .join(ResearchRun, ResearchRun.id == ResearchStep.run_id)
            .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
            .where(
                ResearchStep.id == step_id,
                ResearchStep.is_deleted.is_(False),
                ResearchRun.is_deleted.is_(False),
                ResearchBlueprint.is_deleted.is_(False),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Step not found")
    step = cast(ResearchStep, row[0])
    engine_id = cast(UUID, row[1])
    await resolve_engine_project_context(db, engine_id, user_id, action)
    return step
