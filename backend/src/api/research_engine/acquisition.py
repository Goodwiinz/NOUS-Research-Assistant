"""Full-text acquisition and derived PRISMA flow API (GOO-303).

Transport only: authorization through ``resolve_project``, persistence in
``acquisition_service``, reads in ``prisma_service``. Each mutating route ends
its single transaction with one ``commit``; reads never commit. PRISMA totals
are recomputed on every call and never stored.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextRequestCreate,
    FulltextStateResponse,
    PrismaFlowResponse,
)
from src.services.research_engine import prisma
from src.services.research_engine.acquisition_service import (
    list_fulltext,
    record_attempt,
    request_fulltext,
)
from src.services.research_engine.prisma_service import load_inputs
from src.services.research_engine.project_access import ResearchAction, resolve_project

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research-engine", tags=["research-engine-acquisition"])
PROJECT = "/projects/{project_id}"


@router.post(
    PROJECT + "/fulltext/requests",
    response_model=FulltextStateResponse,
    status_code=201,
)
async def request_fulltext_route(
    project_id: UUID,
    body: FulltextRequestCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FulltextStateResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    state, created = await request_fulltext(db, context, user_id, body)
    await db.commit()
    response.status_code = 201 if created else 200
    return state


@router.post(
    PROJECT + "/fulltext/requests/{request_id}/attempts",
    response_model=FulltextStateResponse,
    status_code=201,
)
async def record_attempt_route(
    project_id: UUID,
    request_id: UUID,
    body: FulltextAttemptCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FulltextStateResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    state, created = await record_attempt(db, context, request_id, user_id, body)
    await db.commit()
    response.status_code = 201 if created else 200
    return state


@router.get(PROJECT + "/fulltext", response_model=list[FulltextStateResponse])
async def list_fulltext_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FulltextStateResponse]:
    context = await resolve_project(
        db, project_id, cast(UUID, current_user.id), ResearchAction.VIEW
    )
    return await list_fulltext(db, context)


async def _flow(db: AsyncSession, project_id: UUID, user_id: UUID) -> dict[str, Any]:
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    # load_inputs ends the access-check transaction: do not touch context after.
    inputs = await load_inputs(db, context)
    try:
        return prisma.package(prisma.derive_prisma_flow(inputs))
    except prisma.PrismaInconsistency as error:
        event_id = uuid4()
        logger.error(
            "PRISMA flow inconsistent",
            extra={"event_id": str(event_id), "project_id": str(project_id)},
            exc_info=error,
        )
        raise HTTPException(
            status_code=500, detail="PRISMA flow inconsistent"
        ) from error


@router.get(PROJECT + "/prisma", response_model=PrismaFlowResponse)
async def prisma_flow_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await _flow(db, project_id, cast(UUID, current_user.id))


@router.get(PROJECT + "/prisma/export")
async def prisma_export_route(
    project_id: UUID,
    format: Literal["json", "md"] = "json",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    package = await _flow(db, project_id, cast(UUID, current_user.id))
    if format == "md":
        content = prisma.render_markdown(package["body"])
        media_type = "text/markdown; charset=utf-8"
    else:
        content = json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False)
        media_type = "application/json"
    name = f"prisma-flow-{project_id}-{package['body_sha256'][:12]}.{format}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
