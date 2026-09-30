"""Protocol-bound screening queue API (GOO-301).

Transport only: authorization through ``resolve_project``, persistence in
``screening_service``. The service never commits, so each mutating route ends
its single transaction here with one ``commit``.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    IdentityEventResponse,
    MyScreeningQueueResponse,
    ScreeningAssignmentCreate,
    ScreeningAssignmentResponse,
    ScreeningObservationCreate,
    ScreeningObservationResponse,
    ScreeningQueueCreate,
    ScreeningQueueResponse,
    ScreeningRevokeRequest,
)
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.services.research_engine.screening_service import (
    assign,
    create_queue,
    history,
    list_queues,
    my_queue,
    revoke,
    submit,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine-screening"])
QUEUES = "/projects/{project_id}/screening/queues"


@router.get(QUEUES, response_model=list[ScreeningQueueResponse])
async def list_screening_queues_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ScreeningQueueResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await list_queues(db, context)


@router.post(QUEUES, response_model=ScreeningQueueResponse, status_code=201)
async def create_screening_queue_route(
    project_id: UUID,
    body: ScreeningQueueCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScreeningQueueResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.SUPERVISE
    )
    queue = await create_queue(db, context, cast(UUID, current_user.id), body)
    await db.commit()
    return queue


@router.post(
    QUEUES + "/{queue_id}/assignments", response_model=ScreeningAssignmentResponse
)
async def assign_screening_reviewer_route(
    project_id: UUID,
    queue_id: UUID,
    body: ScreeningAssignmentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScreeningAssignmentResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.SUPERVISE
    )
    assignment = await assign(db, context, queue_id, cast(UUID, current_user.id), body)
    await db.commit()
    return assignment


@router.post(
    QUEUES + "/{queue_id}/assignments/{assignment_id}/revoke",
    response_model=ScreeningAssignmentResponse,
)
async def revoke_screening_assignment_route(
    project_id: UUID,
    queue_id: UUID,
    assignment_id: UUID,
    body: ScreeningRevokeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScreeningAssignmentResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.SUPERVISE
    )
    assignment = await revoke(
        db, context, queue_id, assignment_id, cast(UUID, current_user.id), body
    )
    await db.commit()
    return assignment


# VIEW, not REVIEW: REVIEW is a mutating action that locks rows and refuses
# archived projects; the service still requires the REVIEWER role here.
@router.get(QUEUES + "/{queue_id}/mine", response_model=MyScreeningQueueResponse)
async def my_screening_queue_route(
    project_id: UUID,
    queue_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyScreeningQueueResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await my_queue(db, context, queue_id, cast(UUID, current_user.id))


@router.post(
    QUEUES + "/{queue_id}/observations", response_model=ScreeningObservationResponse
)
async def submit_screening_observation_route(
    project_id: UUID,
    queue_id: UUID,
    body: ScreeningObservationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScreeningObservationResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.REVIEW
    )
    observation = await submit(db, context, queue_id, cast(UUID, current_user.id), body)
    await db.commit()
    return observation


@router.get(QUEUES + "/{queue_id}/history", response_model=list[IdentityEventResponse])
async def screening_history_route(
    project_id: UUID,
    queue_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[IdentityEventResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await history(db, context, queue_id, cast(UUID, current_user.id))
