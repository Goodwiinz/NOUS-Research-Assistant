"""Manuscript release API (GOO-315): transport only; the service commits.

A candidate (EDIT) packages one exact saved draft version; promotion
(RELEASE: an adjudicator or supervisor, never ownership alone) re-evaluates
the obligations fresh and answers 409 with every failing one. Packages are
private bytes served only through these routes; nothing here authorizes an
external submission.

GOO-316: ``?variant=anonymized`` serves the anonymized package (the default
stays ``identified``), and venue checks run ``generic-icmje-credit/1``
against the stored package bytes (EDIT) or list their results (VIEW).
"""

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import manuscript_release_service as service
from src.services.research import statements_service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.manuscript_release_schemas import (
    CandidateCreate,
    ManuscriptReleaseListResponse,
    ManuscriptReleaseResponse,
    PackageVariant,
    PromoteRequest,
    ReleaseVerification,
)
from src.shared.statements_schemas import (
    VenueCheckCreate,
    VenueCheckListResponse,
    VenueCheckResponse,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/manuscript-releases",
    tags=["manuscript-releases"],
)


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


@router.post("", status_code=201, response_model=ManuscriptReleaseResponse)
async def create_candidate(
    project_id: UUID,
    request: CandidateCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ManuscriptReleaseResponse:
    """Package a candidate release of one exact draft version (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await service.create_candidate(
        db, context, _uid(current_user), request
    )
    if replayed:
        response.status_code = 200
    return result


@router.get("", response_model=ManuscriptReleaseListResponse)
async def list_releases(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ManuscriptReleaseListResponse:
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.list_releases(db, context)


@router.post(
    "/{release_id:uuid}/promote",
    status_code=201,
    response_model=ManuscriptReleaseResponse,
    responses={409: {"description": "Obligations not met, or a stale candidate"}},
)
async def promote(
    project_id: UUID,
    release_id: UUID,
    request: PromoteRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Promote one exact candidate to verified (RELEASE)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.RELEASE
    )
    try:
        result, replayed = await service.promote(
            db, context, _uid(current_user), release_id, request
        )
    except service.ObligationsNotMet as refused:
        return JSONResponse(
            status_code=409,
            content={"detail": service.OBLIGATIONS_NOT_MET, "failing": refused.failing},
        )
    if replayed:
        response.status_code = 200
    return result


@router.get(
    "/{release_id:uuid}/package",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def download_package(
    project_id: UUID,
    release_id: UUID,
    variant: PackageVariant = "identified",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The stored package bytes (VIEW), re-hashed before they leave."""
    context = await resolve_project(db, project_id, _uid(current_user))
    data, sha256 = await service.package_bytes(db, context, release_id, variant)
    suffix = "" if variant == "identified" else f"-{variant}"
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="manuscript-release-{release_id}{suffix}.zip"'
            ),
            "X-Content-SHA256": sha256,
        },
    )


@router.get("/{release_id:uuid}/verify", response_model=ReleaseVerification)
async def verify_release(
    project_id: UUID,
    release_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReleaseVerification:
    """Recompute every package hash and the reference mapping (VIEW)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await service.verify(db, context, release_id)


@router.post(
    "/{release_id:uuid}/venue-checks",
    status_code=201,
    response_model=VenueCheckResponse,
)
async def run_venue_check(
    project_id: UUID,
    release_id: UUID,
    request: VenueCheckCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VenueCheckResponse:
    """Run ``generic-icmje-credit/1`` on the stored package bytes (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    return await service.run_venue_check(
        db, context, _uid(current_user), release_id, request
    )


@router.get("/{release_id:uuid}/venue-checks", response_model=VenueCheckListResponse)
async def list_venue_checks(
    project_id: UUID,
    release_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VenueCheckListResponse:
    """Venue check results with actionable items (VIEW)."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await statements_service.list_venue_checks(db, context, release_id)
