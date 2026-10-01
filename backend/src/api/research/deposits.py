"""Archive deposit API (GOO-318): transport only; the service commits.

Deposits one exact verified manuscript release to the Zenodo sandbox. Every
write is RELEASE (adjudicator or supervisor); an approval binds the exact
release package, account label and action, and the requester may not be the
approver. ``POST`` answers 202: the beat worker advances the deposit phase
by phase. The DOI appears only once the read-back verified it.
"""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import deposit_service as service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.deposit_schemas import (
    DepositApprovalCreate,
    DepositApprovalResponse,
    DepositApprovalRevoke,
    DepositCreate,
    DepositListResponse,
    DepositResponse,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/deposits", tags=["archive-deposits"]
)
RELEASE = ResearchAction.RELEASE


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


@router.get("", response_model=DepositListResponse)
async def list_deposits(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositListResponse:
    """Every deposit with its attempt chain, derived status and approvals."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_deposits(db, context)


@router.post(
    "/approvals",
    status_code=201,
    response_model=DepositApprovalResponse,
    responses={
        409: {"description": "The release is not verified or its package changed"},
        422: {"description": "The approver requested this deposit"},
        503: {"description": "Archive deposits are not configured"},
    },
)
async def approve(
    project_id: UUID,
    request: DepositApprovalCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositApprovalResponse:
    """Approve depositing this exact release package (RELEASE)."""
    context = await resolve_project(db, project_id, _uid(current_user), RELEASE)
    result, replayed = await service.approve(db, context, _uid(current_user), request)
    if replayed:
        response.status_code = 200
    return result


@router.post(
    "/approvals/{approval_id:uuid}/revoke",
    status_code=201,
    response_model=DepositApprovalResponse,
    responses={409: {"description": "The approval is not in force"}},
)
async def revoke(
    project_id: UUID,
    approval_id: UUID,
    request: DepositApprovalRevoke,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositApprovalResponse:
    """Revoke the approval in force (an insert-only revocation row)."""
    context = await resolve_project(db, project_id, _uid(current_user), RELEASE)
    result, replayed = await service.revoke(
        db, context, _uid(current_user), approval_id, request
    )
    if replayed:
        response.status_code = 200
    return result


@router.post(
    "",
    status_code=202,
    response_model=DepositResponse,
    responses={
        200: {"description": "The existing operation (replay or same release)"},
        409: {"description": "No valid approval, or the release changed"},
        422: {"description": "The requester approved this deposit"},
        503: {"description": "Archive deposits are not configured"},
    },
)
async def request_deposit(
    project_id: UUID,
    request: DepositCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    """Queue the deposit of one verified release (RELEASE)."""
    context = await resolve_project(db, project_id, _uid(current_user), RELEASE)
    result, replayed = await service.request_deposit(
        db, context, _uid(current_user), request
    )
    if replayed:
        response.status_code = 200
    return result


@router.post(
    "/{operation_id:uuid}/requeue", status_code=202, response_model=DepositResponse
)
async def requeue(
    project_id: UUID,
    operation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DepositResponse:
    """Put a stopped deposit back on the worker's queue (RELEASE)."""
    context = await resolve_project(db, project_id, _uid(current_user), RELEASE)
    return await service.requeue(db, context, operation_id)
