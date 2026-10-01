"""Evidence tables, contradiction review and outcome certainty API (GOO-310).

Transport only: authorization through ``resolve_project`` (VIEW to read;
REVIEW or ADJUDICATE to freeze a table, open a contradiction or dissent;
ADJUDICATE to resolve or acknowledge; REVIEW to assess certainty),
persistence and the one commit in ``evidence_service``. 201 for a new row,
200 for a replay or an unchanged rebuild.
"""

import json
from typing import List, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    CertaintyCreate,
    CertaintyResponse,
    ContradictionCreate,
    ContradictionResponse,
    EvidenceOutcomeListResponse,
    EvidenceTableCreate,
    EvidenceTablePreview,
    EvidenceTableResponse,
)
from src.services.research_engine import evidence_service
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine-evidence"])
EVIDENCE = "/projects/{project_id}/evidence"


async def _reviewer_or_adjudicator(
    db: AsyncSession, project_id: UUID, user_id: UUID
) -> tuple[ProjectContext, str]:
    """REVIEW first, else ADJUDICATE; the role used is the one recorded."""
    try:
        return (
            await resolve_project(db, project_id, user_id, ResearchAction.REVIEW),
            "reviewer",
        )
    except HTTPException as error:
        if error.status_code != 403:
            raise
    try:
        return (
            await resolve_project(db, project_id, user_id, ResearchAction.ADJUDICATE),
            "adjudicator",
        )
    except HTTPException as error:
        if error.status_code != 403:
            raise
        raise HTTPException(
            status_code=403, detail="reviewer or adjudicator role required"
        ) from error


@router.get(EVIDENCE, response_model=EvidenceOutcomeListResponse)
async def list_evidence_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EvidenceOutcomeListResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await evidence_service.list_outcomes(db, context)


@router.get(EVIDENCE + "/tables/preview", response_model=EvidenceTablePreview)
async def preview_evidence_table_route(
    project_id: UUID,
    outcome_key: str = Query(..., min_length=1, max_length=100),
    timepoint: str = Query(..., min_length=1, max_length=100),
    matrix_id: UUID = Query(...),
    field_ids: List[UUID] = Query(..., min_length=1, max_length=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EvidenceTablePreview:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await evidence_service.preview(
        db, context, outcome_key, timepoint, matrix_id, field_ids
    )


@router.post(
    EVIDENCE + "/tables", response_model=EvidenceTableResponse, status_code=201
)
async def create_evidence_table_route(
    project_id: UUID,
    body: EvidenceTableCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EvidenceTableResponse:
    user_id = cast(UUID, current_user.id)
    context, role = await _reviewer_or_adjudicator(db, project_id, user_id)
    row, replayed = await evidence_service.create_table(
        db, context, user_id, role, body
    )
    if replayed:
        response.status_code = 200
    return row


@router.post(
    EVIDENCE + "/contradictions", response_model=ContradictionResponse, status_code=201
)
async def record_contradiction_route(
    project_id: UUID,
    body: ContradictionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContradictionResponse:
    user_id = cast(UUID, current_user.id)
    if body.kind in ("resolved", "acknowledged"):
        context = await resolve_project(
            db, project_id, user_id, ResearchAction.ADJUDICATE
        )
        role = "adjudicator"
    else:
        context, role = await _reviewer_or_adjudicator(db, project_id, user_id)
    group, replayed = await evidence_service.record_contradiction(
        db, context, user_id, role, body
    )
    if replayed:
        response.status_code = 200
    return group


@router.post(EVIDENCE + "/certainty", response_model=CertaintyResponse, status_code=201)
async def assess_certainty_route(
    project_id: UUID,
    body: CertaintyCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CertaintyResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.REVIEW)
    row, replayed = await evidence_service.assess_certainty(db, context, user_id, body)
    if replayed:
        response.status_code = 200
    return row


@router.get(
    EVIDENCE + "/export",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {}},
            "description": "nous.academic.evidence.v1 package (every version)",
        }
    },
)
async def export_evidence_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    package = await evidence_service.export_package(db, context)
    return Response(
        content=json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="evidence-{project_id}.json"'
        },
    )
