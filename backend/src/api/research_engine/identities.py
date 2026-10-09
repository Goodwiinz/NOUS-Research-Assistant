"""Project-scoped report/study identity API (GOO-299).

Transport only: authorization through ``resolve_project``, persistence in
``identity_service``. The service never commits, so each mutating route ends
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
    ReportCandidatesResponse,
    ReportMergeRequest,
    ReportResponse,
    ReportSplitRequest,
    StudyLinkRequest,
)
from src.services.research_engine.identity_service import (
    candidates,
    history,
    link_study,
    list_reports,
    merge_reports,
    split_report,
)
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-identities"])


@router.get("/projects/{project_id}/reports", response_model=list[ReportResponse])
async def list_reports_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ReportResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await list_reports(db, collection_id=cast(UUID, context.collection.id))


@router.get(
    "/projects/{project_id}/reports/history",
    response_model=list[IdentityEventResponse],
)
async def report_history_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[IdentityEventResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await history(db, collection_id=cast(UUID, context.collection.id))


@router.get(
    "/projects/{project_id}/reports/{report_id}/candidates",
    response_model=ReportCandidatesResponse,
)
async def report_candidates_route(
    project_id: UUID,
    report_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReportCandidatesResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await candidates(
        db, collection_id=cast(UUID, context.collection.id), report_id=report_id
    )


@router.post(
    "/projects/{project_id}/reports/{report_id}/study-link",
    response_model=ReportResponse,
)
async def link_study_route(
    project_id: UUID,
    report_id: UUID,
    body: StudyLinkRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    action = (
        ResearchAction.REVIEW
        if body.status == "proposed"
        else ResearchAction.ADJUDICATE
    )
    context = await resolve_project(db, project_id, cast(UUID, current_user.id), action)
    report = await link_study(db, context, report_id, cast(UUID, current_user.id), body)
    await db.commit()
    return report


@router.post("/projects/{project_id}/reports/merge", response_model=ReportResponse)
async def merge_reports_route(
    project_id: UUID,
    body: ReportMergeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.ADJUDICATE
    )
    report = await merge_reports(db, context, cast(UUID, current_user.id), body)
    await db.commit()
    return report


@router.post(
    "/projects/{project_id}/reports/{report_id}/split",
    response_model=ReportResponse,
)
async def split_report_route(
    project_id: UUID,
    report_id: UUID,
    body: ReportSplitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.ADJUDICATE
    )
    report = await split_report(
        db, context, report_id, cast(UUID, current_user.id), body
    )
    await db.commit()
    return report
