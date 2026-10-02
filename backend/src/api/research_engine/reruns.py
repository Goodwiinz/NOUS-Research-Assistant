"""Fresh rerun API (GOO-313).

Transport only: run routes reach the run through ``require_run`` (VIEW; 404
for a foreign user), rerun routes through the service's rerun lookup; the
service resolves REVIEW for admission, cancel and retry (403 without the
reviewer role) and owns the one commit. Downloads are the exact hashed
bytes with an ``X-Content-SHA256`` header; no storage key is ever returned.
202 for an admitted rerun, 200 for a replay.
"""

import json
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    RerunCreate,
    RerunEligibilityResponse,
    RerunListResponse,
    RerunResponse,
)
from src.services.research_engine import rerun_service
from src.services.research_engine.project_access import ResearchAction, require_run

router = APIRouter(prefix="/research-engine", tags=["research-engine-reruns"])
RUN = "/runs/{run_id}"
RERUN = "/reruns/{rerun_id}"
_INELIGIBLE: dict[int | str, dict[str, Any]] = {
    409: {"description": "Not eligible; the body lists every reason"}
}


@router.get(RUN + "/rerun-eligibility", response_model=RerunEligibilityResponse)
async def rerun_eligibility_route(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RerunEligibilityResponse:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    return await rerun_service.eligibility(db, run, user_id)


@router.get(RUN + "/reruns", response_model=RerunListResponse)
async def list_reruns_route(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RerunListResponse:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    return await rerun_service.list_for_run(db, run, user_id)


@router.post(
    RUN + "/reruns",
    response_model=RerunResponse,
    status_code=202,
    responses=_INELIGIBLE,
)
async def admit_rerun_route(
    run_id: UUID,
    body: RerunCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    try:
        rerun, replayed = await rerun_service.admit(db, user_id, run, body)
    except rerun_service.RerunIneligible as refused:
        return JSONResponse(
            status_code=409,
            content={"detail": rerun_service.NOT_ELIGIBLE, "reasons": refused.reasons},
        )
    if replayed:
        response.status_code = 200
    return rerun


@router.get(RERUN, response_model=RerunResponse)
async def get_rerun_route(
    rerun_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RerunResponse:
    return await rerun_service.get(db, rerun_id, cast(UUID, current_user.id))


@router.post(RERUN + "/cancel", response_model=RerunResponse)
async def cancel_rerun_route(
    rerun_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RerunResponse:
    return await rerun_service.cancel(db, rerun_id, cast(UUID, current_user.id))


@router.post(RERUN + "/retry", response_model=RerunResponse)
async def retry_rerun_route(
    rerun_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RerunResponse:
    """Any request body is ignored: attempt n+1 reuses the stored rule."""
    return await rerun_service.retry(db, rerun_id, cast(UUID, current_user.id))


@router.get(
    RERUN + "/attempts/{attempt}/outputs/{name}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_rerun_output_route(
    rerun_id: UUID,
    attempt: int,
    name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    data, digest = await rerun_service.read_output(
        db, rerun_id, attempt, name, cast(UUID, current_user.id)
    )
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{digest}"',
            "X-Content-SHA256": digest,
        },
    )


@router.get(
    RERUN + "/comparison",
    response_class=Response,
    responses={200: {"content": {"application/json": {}}}},
)
async def download_rerun_comparison_route(
    rerun_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    body = await rerun_service.comparison_export(
        db, rerun_id, cast(UUID, current_user.id)
    )
    return Response(
        content=json.dumps(body, sort_keys=True, indent=2).encode(),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="rerun-comparison-{rerun_id}.json"'
            )
        },
    )
