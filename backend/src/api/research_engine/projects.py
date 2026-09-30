"""Canonical research-engine project and decision-role endpoints."""

from typing import List, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.collection import Collection
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.user import User
from src.models.workspace import WorkspaceRole
from src.schemas.research_engine import (
    LegacyProjectResponse,
    ProjectCreate,
    ProjectLink,
    ProjectResponse,
    ResearchProjectRoleCreate,
    ResearchProjectRoleResponse,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    accessible_research_workspace_ids,
    project_response,
    require_legacy_project,
    resolve_project,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine"])


async def _legacy_response(
    db: AsyncSession, project: ResearchProject
) -> LegacyProjectResponse:
    project_status = project.status
    if project.collection_id is not None:
        collection_status = (
            await db.execute(
                select(Collection.research_status).where(
                    Collection.id == project.collection_id,
                    Collection.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if collection_status is not None:
            project_status = collection_status
    return LegacyProjectResponse(
        research_engine_project_id=project.id,
        project_id=project.collection_id,
        collection_id=project.collection_id,
        name=project.name,
        description=project.description,
        status=project_status,
    )


def _role_response(
    assignment: ResearchProjectRoleAssignment,
) -> ResearchProjectRoleResponse:
    return ResearchProjectRoleResponse(
        id=assignment.id,
        project_id=assignment.collection_id,
        user_id=assignment.user_id,
        role=assignment.role.value,
        assigned_by_id=assignment.assigned_by_id,
        created_at=assignment.created_at,
    )


async def _project_response(
    db: AsyncSession, context: ProjectContext
) -> ProjectResponse:
    blueprint_id = None
    if context.engine is not None:
        blueprint_id = (
            await db.execute(
                select(ResearchBlueprint.id)
                .where(
                    ResearchBlueprint.project_id == context.engine.id,
                    ResearchBlueprint.is_deleted.is_(False),
                )
                .order_by(
                    ResearchBlueprint.version.desc(),
                    ResearchBlueprint.created_at.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    return cast(
        ProjectResponse,
        ProjectResponse.model_validate(project_response(context, blueprint_id)),
    )


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    if body.collection_id is None:
        raise HTTPException(status_code=422, detail="collection_id is required")
    context = await resolve_project(
        db, body.collection_id, current_user.id, ResearchAction.MANAGE
    )
    existing = (
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.collection_id == body.collection_id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return await _project_response(
            db,
            ProjectContext(
                context.collection,
                context.workspace,
                existing,
                context.organization_id,
                context.effective_roles,
                context.workspace_role,
            ),
        )
    project = ResearchProject(
        collection_id=context.collection.id,
        name=context.collection.name,
        description=context.collection.description,
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
    return await _project_response(
        db,
        ProjectContext(
            context.collection,
            context.workspace,
            project,
            context.organization_id,
            context.effective_roles,
            context.workspace_role,
        ),
    )


@router.get("/projects", response_model=List[ProjectResponse])
async def list_projects(
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProjectResponse]:
    workspace_ids = await accessible_research_workspace_ids(db, current_user.id)
    query = (
        select(ResearchProject.collection_id)
        .join(Collection, Collection.id == ResearchProject.collection_id)
        .where(
            Collection.workspace_id.in_(workspace_ids),
            Collection.is_deleted.is_(False),
            ResearchProject.is_deleted.is_(False),
        )
    )
    if status_filter:
        query = query.where(Collection.research_status == status_filter)
    ids = (await db.execute(query)).scalars().all()
    responses: list[ProjectResponse] = []
    for collection_id in ids:
        try:
            context = await resolve_project(
                db, collection_id, current_user.id, require_engine=True
            )
        except HTTPException:
            continue
        responses.append(await _project_response(db, context))
    return responses


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse | RedirectResponse:
    try:
        context = await resolve_project(
            db, project_id, current_user.id, require_engine=True
        )
        return await _project_response(db, context)
    except HTTPException as canonical_error:
        if canonical_error.status_code != 404:
            raise
        canonical_id_exists = (
            await db.execute(select(Collection.id).where(Collection.id == project_id))
        ).scalar_one_or_none()
        if canonical_id_exists is not None:
            raise canonical_error
    legacy = (
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.id == project_id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    if legacy is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if legacy.collection_id is None:
        if legacy.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="Project not found")
        raise HTTPException(status_code=409, detail="mapping_required")
    await resolve_project(
        db, legacy.collection_id, current_user.id, require_engine=True
    )
    return RedirectResponse(
        f"/api/v1/research-engine/projects/{legacy.collection_id}", status_code=307
    )


@router.get("/legacy-projects", response_model=List[LegacyProjectResponse])
async def list_legacy_projects(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> List[LegacyProjectResponse]:
    projects = (
        (
            await db.execute(
                select(ResearchProject).where(
                    ResearchProject.owner_id == current_user.id,
                    ResearchProject.collection_id.is_(None),
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return [await _legacy_response(db, project) for project in projects]


@router.get("/legacy-projects/{engine_id}", response_model=LegacyProjectResponse)
async def get_legacy_project(
    engine_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LegacyProjectResponse:
    return await _legacy_response(
        db, await require_legacy_project(db, engine_id, current_user.id)
    )


@router.patch("/projects/{engine_id}/collection", response_model=ProjectResponse)
async def link_project_collection(
    engine_id: UUID,
    body: ProjectLink,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    context = await resolve_project(
        db, body.collection_id, current_user.id, ResearchAction.MANAGE
    )
    await db.execute(
        select(Collection.id)
        .where(Collection.id == body.collection_id)
        .with_for_update()
    )
    project = (
        await db.execute(
            select(ResearchProject)
            .where(
                ResearchProject.id == engine_id, ResearchProject.is_deleted.is_(False)
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.collection_id == body.collection_id:
        return await _project_response(
            db,
            ProjectContext(
                context.collection,
                context.workspace,
                project,
                context.organization_id,
                context.effective_roles,
                context.workspace_role,
            ),
        )
    if project.collection_id is not None:
        raise HTTPException(status_code=409, detail="Project is already linked")
    if project.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Project not found")
    owner_org = (
        await db.execute(
            select(User.organization_id).where(User.id == project.owner_id)
        )
    ).scalar_one_or_none()
    if owner_org != context.organization_id:
        raise HTTPException(status_code=404, detail="Project not found")
    project.collection_id = body.collection_id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Project is already linked"
        ) from None
    await db.refresh(project)
    return await _project_response(
        db,
        ProjectContext(
            context.collection,
            context.workspace,
            project,
            context.organization_id,
            context.effective_roles,
            context.workspace_role,
        ),
    )


@router.get(
    "/projects/{project_id}/roles", response_model=List[ResearchProjectRoleResponse]
)
async def list_project_roles(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ResearchProjectRoleResponse]:
    await resolve_project(db, project_id, current_user.id)
    assignments = (
        (
            await db.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == project_id,
                    ResearchProjectRoleAssignment.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_role_response(assignment) for assignment in assignments]


@router.put("/projects/{project_id}/roles", response_model=ResearchProjectRoleResponse)
async def assign_project_role(
    project_id: UUID,
    body: ResearchProjectRoleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchProjectRoleResponse:
    context = await resolve_project(
        db, project_id, current_user.id, ResearchAction.MANAGE
    )
    try:
        role = ResearchProjectRole(body.role)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Invalid research project role"
        ) from None
    target_role = next(
        (
            member.role
            for member in context.workspace.members
            if not member.is_deleted and member.user_id == body.user_id
        ),
        None,
    )
    if context.workspace.owner_id == body.user_id:
        target_role = WorkspaceRole.OWNER
    target_org = (
        await db.execute(select(User.organization_id).where(User.id == body.user_id))
    ).scalar_one_or_none()
    if target_role is None or target_org != context.organization_id:
        raise HTTPException(
            status_code=422, detail="User is not an eligible project member"
        )
    existing = (
        (
            await db.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == project_id,
                    ResearchProjectRoleAssignment.user_id == body.user_id,
                    ResearchProjectRoleAssignment.role == role,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.deleted_at = None
            existing.assigned_by_id = current_user.id
            await db.commit()
            await db.refresh(existing)
        return _role_response(existing)
    assignment = ResearchProjectRoleAssignment(
        collection_id=project_id,
        user_id=body.user_id,
        role=role,
        assigned_by_id=current_user.id,
    )
    db.add(assignment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Role already assigned") from None
    await db.refresh(assignment)
    return _role_response(assignment)


@router.delete("/projects/{project_id}/roles/{user_id}/{role}", status_code=204)
async def delete_project_role(
    project_id: UUID,
    user_id: UUID,
    role: ResearchProjectRole,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await resolve_project(db, project_id, current_user.id, ResearchAction.MANAGE)
    assignment = (
        (
            await db.execute(
                select(ResearchProjectRoleAssignment)
                .where(
                    ResearchProjectRoleAssignment.collection_id == project_id,
                    ResearchProjectRoleAssignment.user_id == user_id,
                    ResearchProjectRoleAssignment.role == role,
                    ResearchProjectRoleAssignment.is_deleted.is_(False),
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Role assignment not found")
    assignment.soft_delete()
    await db.commit()
    return Response(status_code=204)
