"""Statement set and approval API (GOO-316): transport only; the service
commits.

A statement set (EDIT) is a versioned record of authorship, CRediT roles and
disclosures; approvals bind its exact hash. ``in_app_self`` is refused (403)
unless the caller is the author's linked user. Authorship grants no project
permission: every route resolves access through ``resolve_project`` alone.
"""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import statements_service as service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.statements_schemas import (
    ApprovalCreate,
    ApprovalResponse,
    StatementSetCreate,
    StatementSetResponse,
    StatementsListResponse,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/statements", tags=["manuscript-statements"]
)


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


@router.post(
    "",
    status_code=201,
    response_model=StatementSetResponse,
    responses={409: {"description": "The statement set changed (stale tip)"}},
)
async def version_set(
    project_id: UUID,
    request: StatementSetCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StatementSetResponse:
    """Record a new statement set version (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await service.version_set(
        db, context, _uid(current_user), request
    )
    if replayed:
        response.status_code = 200
    return result


@router.get("", response_model=StatementsListResponse)
async def list_sets(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StatementsListResponse:
    """The tip and history with missing fields, ORCID states and approvals."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_sets(db, context)


@router.post(
    "/{set_id:uuid}/approvals",
    status_code=201,
    response_model=ApprovalResponse,
    responses={
        403: {"description": "Self-approval by someone other than the author"},
        409: {"description": "The set changed, or the author already approved"},
    },
)
async def approve(
    project_id: UUID,
    set_id: UUID,
    request: ApprovalCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApprovalResponse:
    """One author's approval of the set's exact hash (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await service.approve(
        db, context, _uid(current_user), set_id, request
    )
    if replayed:
        response.status_code = 200
    return result
