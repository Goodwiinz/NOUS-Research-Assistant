"""Scheduled search update API (GOO-319): transport only; the service commits.

A supervisor pins one GOO-298 strategy to a cron schedule in an IANA
timezone; the beat worker runs each due fire and classifies the corpus
delta. Writes are SUPERVISE, reads VIEW. The delta export is a sealed,
read-only JSON attachment.
"""

import json
from typing import Literal, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    SearchDeltaExport,
    SearchExecutionListResponse,
    SearchScheduleCreate,
    SearchScheduleListResponse,
    SearchScheduleResponse,
    SearchScheduleVersionCreate,
)
from src.services.research_engine import search_update_service as service
from src.services.research_engine.project_access import ResearchAction, resolve_project

SCHEDULES = "/projects/{project_id}/search-schedules"
router = APIRouter(prefix="/research-engine", tags=["research-engine-search-updates"])
SUPERVISE = ResearchAction.SUPERVISE
DeltaClassQuery = Literal[
    "new", "changed", "corrected_retracted", "unchanged", "unknown"
]


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


@router.get(SCHEDULES, response_model=SearchScheduleListResponse)
async def list_schedules(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchScheduleListResponse:
    """Schedules (tip, versions, derived status, next fire, last execution)
    and the pinnable strategies of completed runs."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_schedules(db, context)


@router.post(
    SCHEDULES,
    status_code=201,
    response_model=SearchScheduleResponse,
    responses={
        200: {"description": "Replayed schedule"},
        409: {"description": "Strategy built under a superseded protocol"},
        422: {"description": "Invalid cron, timezone or strategy hash"},
    },
)
async def create_schedule(
    project_id: UUID,
    request: SearchScheduleCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchScheduleResponse:
    """Pin one strategy to a schedule (SUPERVISE)."""
    context = await resolve_project(db, project_id, _uid(current_user), SUPERVISE)
    result, replayed = await service.create_schedule(
        db, context, _uid(current_user), request
    )
    if replayed:
        response.status_code = 200
    return result


@router.post(
    SCHEDULES + "/{schedule_id:uuid}/versions",
    status_code=201,
    response_model=SearchScheduleResponse,
    responses={
        200: {"description": "Replayed version"},
        409: {"description": "Stale tip or superseded protocol"},
        422: {"description": "Invalid cron, timezone or strategy hash"},
    },
)
async def version_schedule(
    project_id: UUID,
    schedule_id: UUID,
    request: SearchScheduleVersionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchScheduleResponse:
    """Edit, enable or disable a schedule as a new version (SUPERVISE)."""
    context = await resolve_project(db, project_id, _uid(current_user), SUPERVISE)
    result, replayed = await service.version_schedule(
        db, context, _uid(current_user), schedule_id, request
    )
    if replayed:
        response.status_code = 200
    return result


@router.get(
    SCHEDULES + "/{schedule_id:uuid}/executions",
    response_model=SearchExecutionListResponse,
)
async def list_executions(
    project_id: UUID,
    schedule_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchExecutionListResponse:
    """Every execution of one schedule with all attempts (failures kept)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_executions(db, context, schedule_id)


@router.get(
    SCHEDULES + "/executions/{execution_id:uuid}/delta",
    response_class=Response,
    responses={
        200: {
            "model": SearchDeltaExport,
            "description": "Sealed nous.academic.search-delta.v1 export",
        },
        404: {"description": "Unknown, foreign or unfinished execution"},
    },
)
async def export_delta(
    project_id: UUID,
    execution_id: UUID,
    delta_class: Optional[DeltaClassQuery] = Query(None, alias="class"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The classified delta of one succeeded execution (read-only)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    package = await service.export_delta(db, context, execution_id, delta_class)
    return Response(
        content=json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="search-delta-{execution_id}.json"'
            )
        },
    )
