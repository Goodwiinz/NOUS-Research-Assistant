"""Study-design appraisal API (GOO-309).

Transport only: authorization through ``resolve_project`` (REVIEW to submit,
ADJUDICATE to resolve a conflict, VIEW to read), persistence and the one
commit in ``appraisal_service``. 201 for a new row, 200 for a replay.
"""

import json
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    AppraisalAdjudicate,
    AppraisalListResponse,
    AppraisalResponse,
    AppraisalSubmit,
)
from src.services.research_engine import appraisal_service
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-appraisals"])
APPRAISALS = "/projects/{project_id}/appraisals"


@router.get(APPRAISALS, response_model=AppraisalListResponse)
async def list_appraisals_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppraisalListResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await appraisal_service.list_appraisals(db, context, user_id)


@router.post(APPRAISALS, response_model=AppraisalResponse, status_code=201)
async def submit_appraisal_route(
    project_id: UUID,
    body: AppraisalSubmit,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppraisalResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.REVIEW)
    row, replayed = await appraisal_service.submit(db, context, user_id, body)
    if replayed:
        response.status_code = 200
    return row


@router.post(
    APPRAISALS + "/adjudications", response_model=AppraisalResponse, status_code=201
)
async def adjudicate_appraisal_route(
    project_id: UUID,
    body: AppraisalAdjudicate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppraisalResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.ADJUDICATE)
    row, replayed = await appraisal_service.adjudicate(db, context, user_id, body)
    if replayed:
        response.status_code = 200
    return row


@router.get(
    APPRAISALS + "/export",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {}},
            "description": "nous.academic.appraisal.v1 package (visible rows only)",
        }
    },
)
async def export_appraisals_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    package = await appraisal_service.export_package(db, context, user_id)
    return Response(
        content=json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="appraisal-{project_id}.json"'
        },
    )
