"""Plan-to-write journey API (GOO-308).

Transport only: VIEW through ``resolve_project`` after the service opens one
read-only snapshot; the routes never commit and record no ledger event (a
download is not a decision, so it needs no idempotency key either).
"""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.research_engine import JourneyResponse, JourneyStage
from src.services.research_engine import audit_bundle, journey
from src.services.research_engine.project_access import ResearchAction, resolve_project

router = APIRouter(prefix="/research-engine", tags=["research-engine-journey"])
PROJECT = "/projects/{project_id}"


@router.get(PROJECT + "/journey", response_model=JourneyResponse)
async def journey_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JourneyResponse:
    """Stage facts and derived status; reports state, never enforces order."""
    user_id = cast(UUID, current_user.id)  # before the snapshot expires it
    await journey.begin_read_snapshot(db)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    stages = journey.derive_stages(await journey.facts(db, context))
    return JourneyResponse(
        stages=[
            JourneyStage(
                key=cast(journey.StageKey, s.key),
                status=s.status,
                facts=s.facts,
                blockers=s.blockers,
            )
            for s in stages
        ],
        current=cast(journey.StageKey | None, journey.current_stage(stages)),
    )


@router.get(
    PROJECT + "/audit-bundle",
    response_class=Response,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Offline-verifiable audit bundle (manifest + SHA256SUMS)",
        }
    },
)
async def audit_bundle_route(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    user_id = cast(UUID, current_user.id)  # before the snapshot expires it
    await journey.begin_read_snapshot(db)
    context = await resolve_project(db, project_id, user_id, ResearchAction.VIEW)
    content, manifest_sha = await audit_bundle.build(db, context)
    filename = f"audit-{project_id}-{manifest_sha[:12]}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
