"""External peer-review API (GOO-314): transport only; the service commits.

A first write answers 201; an identical retry with the same idempotency key
answers 200 with the original row. Resolution and reopening need an explicit
adjudicator assignment (ADJUDICATE); ownership or edit rights never do.
"""

from typing import Literal, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import peer_review_service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.peer_review_schemas import (
    CommentCreate,
    CommentResponse,
    DecisionCreate,
    DecisionResponse,
    ResponseCreate,
    ResponseVersionResponse,
    RoundCreate,
    RoundDetail,
    RoundListResponse,
    RoundResponse,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/peer-review", tags=["peer-review"]
)


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


def _status(response: Response, replayed: bool) -> None:
    if replayed:
        response.status_code = 200


@router.get("/rounds", response_model=RoundListResponse)
async def list_rounds(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoundListResponse:
    context = await resolve_project(db, project_id, _uid(current_user))
    return await peer_review_service.list_rounds(db, context)


@router.post("/rounds", status_code=201, response_model=RoundResponse)
async def create_round(
    project_id: UUID,
    request: RoundCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoundResponse:
    """Record a round of one exact saved draft version (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await peer_review_service.create_round(
        db, context, _uid(current_user), request
    )
    _status(response, replayed)
    return result


@router.get("/rounds/{round_id:uuid}", response_model=RoundDetail)
async def get_round(
    project_id: UUID,
    round_id: UUID,
    target_draft_id: Optional[UUID] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoundDetail:
    """Comments with derived status and anchor state against the target
    version (default: the current draft)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await peer_review_service.get_round(db, context, round_id, target_draft_id)


@router.get(
    "/rounds/{round_id:uuid}/export",
    response_class=Response,
    responses={
        200: {"content": {"application/json": {}, "text/markdown": {}}},
    },
)
async def export_round(
    project_id: UUID,
    round_id: UUID,
    format: Literal["markdown", "json"] = Query("markdown"),
    target_draft_id: Optional[UUID] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download the response export (VIEW), ending with unresolved items."""
    context = await resolve_project(db, project_id, _uid(current_user))
    content, filename, media_type = await peer_review_service.export_round(
        db, context, round_id, format, target_draft_id
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/rounds/{round_id:uuid}/comments",
    status_code=201,
    response_model=CommentResponse,
)
async def add_comment(
    project_id: UUID,
    round_id: UUID,
    request: CommentCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    """Add a comment or a new version of one (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await peer_review_service.add_comment(
        db, context, _uid(current_user), round_id, request
    )
    _status(response, replayed)
    return result


@router.post(
    "/comments/{comment_root_id:uuid}/responses",
    status_code=201,
    response_model=ResponseVersionResponse,
)
async def respond(
    project_id: UUID,
    comment_root_id: UUID,
    request: ResponseCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResponseVersionResponse:
    """Record a response version (EDIT); 409 on a stale tip."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await peer_review_service.respond(
        db, context, _uid(current_user), comment_root_id, request
    )
    _status(response, replayed)
    return result


@router.post(
    "/comments/{comment_root_id:uuid}/decisions",
    status_code=201,
    response_model=DecisionResponse,
)
async def decide(
    project_id: UUID,
    comment_root_id: UUID,
    request: DecisionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DecisionResponse:
    """Assign (EDIT) or resolve/reopen (ADJUDICATE) a comment."""
    context = await resolve_project(
        db,
        project_id,
        _uid(current_user),
        peer_review_service.decision_action(request.kind),
    )
    result, replayed = await peer_review_service.decide(
        db, context, _uid(current_user), comment_root_id, request
    )
    _status(response, replayed)
    return result
