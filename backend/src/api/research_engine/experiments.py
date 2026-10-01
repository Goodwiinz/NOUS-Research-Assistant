"""Run manifest v2, retained artifact and figure API (GOO-312).

Transport only: run routes authorize through ``require_run`` (VIEW), figure
routes through ``resolve_project`` (VIEW to read, EDIT to register);
persistence and the one commit live in ``experiment_service``. Downloads are
the exact hashed bytes with an ``X-Content-SHA256`` header; no storage key
or URL is ever returned. 201 for a new figure, 200 for a replay or an
unchanged output.
"""

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import (
    FigureCreate,
    FigureLineageResponse,
    FigureListResponse,
    FigureResponse,
    RunManifestV2Response,
)
from src.services.research_engine import experiment_service
from src.services.research_engine.project_access import (
    ResearchAction,
    require_run,
    resolve_project,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine-experiments"])
RUN = "/runs/{run_id}"
FIGURES = "/projects/{project_id}/figures"
_BYTES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/octet-stream": {}}}
}


@router.get(RUN + "/manifest/v2", response_model=RunManifestV2Response)
async def get_manifest_v2_route(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunManifestV2Response:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    return await experiment_service.manifest_v2(db, run)


@router.get(
    RUN + "/manifest/v2/download",
    response_class=Response,
    responses={200: {"content": {"application/json": {}}}},
)
async def download_manifest_v2_route(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    data, digest = await experiment_service.manifest_bytes(db, run)
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="run-manifest-{run_id}.json"',
            "X-Content-SHA256": digest,
        },
    )


@router.get(RUN + "/artifacts/{artifact_id}", response_class=Response, responses=_BYTES)
async def download_artifact_route(
    run_id: UUID,
    artifact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)
    run = await require_run(db, run_id, user_id, ResearchAction.VIEW)
    data, artifact = await experiment_service.read_artifact(db, run, artifact_id)
    return Response(
        content=data,
        media_type=str(artifact.media_type),
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.sha256}"',
            "X-Content-SHA256": str(artifact.sha256),
        },
    )


@router.get(FIGURES, response_model=FigureListResponse)
async def list_figures_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FigureListResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await experiment_service.list_figures(db, context)


@router.post(FIGURES, response_model=FigureResponse, status_code=201)
async def register_figure_route(
    project_id: UUID,
    body: FigureCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FigureResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.EDIT)
    row, replayed = await experiment_service.register_figure(db, context, user_id, body)
    if replayed:
        response.status_code = 200
    return row


@router.get(FIGURES + "/{figure_id}/lineage", response_model=FigureLineageResponse)
async def figure_lineage_route(
    project_id: UUID,
    figure_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FigureLineageResponse:
    user_id = cast(UUID, current_user.id)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    return await experiment_service.lineage(db, context, figure_id)
