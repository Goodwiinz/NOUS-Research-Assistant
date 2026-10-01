"""Quantitative synthesis API (GOO-311).

Transport only: authorization through ``resolve_project`` (VIEW to preview,
list and export; REVIEW to execute), persistence and the one commit in
``synthesis_service``. 201 for a new result, 200 for a replay or an
unchanged input.
"""

import json
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    SynthesisExecute,
    SynthesisListResponse,
    SynthesisPreview,
    SynthesisResultResponse,
)
from src.services.research_engine import synthesis_service
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-synthesis"])
SYNTHESIS = "/projects/{project_id}/synthesis"


@router.get(SYNTHESIS, response_model=SynthesisListResponse)
async def list_synthesis_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SynthesisListResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await synthesis_service.list_results(db, context)


@router.get(SYNTHESIS + "/preview", response_model=SynthesisPreview)
async def preview_synthesis_route(
    project_id: UUID,
    table_version_id: UUID = Query(...),
    mean_i: UUID = Query(...),
    sd_i: UUID = Query(...),
    n_i: UUID = Query(...),
    mean_c: UUID = Query(...),
    sd_c: UUID = Query(...),
    n_c: UUID = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SynthesisPreview:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    roles = {
        "mean_i": mean_i,
        "sd_i": sd_i,
        "n_i": n_i,
        "mean_c": mean_c,
        "sd_c": sd_c,
        "n_c": n_c,
    }
    return await synthesis_service.preview(db, context, table_version_id, roles)


@router.post(SYNTHESIS, response_model=SynthesisResultResponse, status_code=201)
async def execute_synthesis_route(
    project_id: UUID,
    body: SynthesisExecute,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SynthesisResultResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.REVIEW)
    row, replayed = await synthesis_service.execute(db, context, user_id, body)
    if replayed:
        response.status_code = 200
    return row


@router.get(
    SYNTHESIS + "/export",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {}},
            "description": "nous.academic.synthesis.v1 package (every result)",
        }
    },
)
async def export_synthesis_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    package = await synthesis_service.export_package(db, context)
    # json.dumps writes floats with repr, so every number round-trips exactly.
    return Response(
        content=json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="synthesis-{project_id}.json"'
        },
    )
