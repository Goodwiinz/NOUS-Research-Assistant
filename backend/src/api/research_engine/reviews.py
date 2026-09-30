"""Owner-scoped API for exact-hash research stage reviews."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    PendingReviewResponse,
    StageReviewRequest,
    StageReviewResponse,
)
from src.services.research_engine.review_service import (
    ResearchReviewError,
    ResearchReviewService,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine"])


def _error_detail(error: ResearchReviewError) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
    }
    if error.descriptor is not None:
        detail["descriptor"] = error.descriptor.model_dump(
            mode="json", exclude_none=True
        )
    return detail


def _error_response(error: ResearchReviewError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=jsonable_encoder({"detail": _error_detail(error)}),
    )


@router.get(
    "/runs/{run_id}/reviews/pending",
    response_model=PendingReviewResponse,
)
async def get_pending_review(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PendingReviewResponse | JSONResponse:
    """Return the current owned review gate and its bounded persisted output."""

    try:
        return await ResearchReviewService(db).get_pending_review(
            run_id=run_id,
            owner_id=cast(UUID, current_user.id),
        )
    except ResearchReviewError as error:
        return _error_response(error)


@router.post(
    "/runs/{run_id}/reviews/{step_index}",
    response_model=StageReviewResponse,
)
async def submit_review(
    run_id: UUID,
    step_index: int,
    request: StageReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StageReviewResponse | JSONResponse:
    """Append one review bound to the current persisted stage envelope."""

    try:
        return await ResearchReviewService(db).submit_review(
            run_id=run_id,
            step_index=step_index,
            owner_id=cast(UUID, current_user.id),
            organization_id=cast(
                UUID | None, getattr(current_user, "organization_id", None)
            ),
            reviewer_id=cast(UUID, current_user.id),
            request=request,
        )
    except ResearchReviewError as error:
        return _error_response(error)
