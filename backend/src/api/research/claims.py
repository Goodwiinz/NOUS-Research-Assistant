"""Versioned claims API (GOO-306): transport only; the service commits.

A first write answers 201; an identical retry with the same idempotency key
answers 200 with the original rows.
"""

from typing import Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import claims_service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.claim_schemas import (
    ClaimAssessmentCreate,
    ClaimAssessmentResponse,
    ClaimCreate,
    ClaimDetailResponse,
    ClaimLinkCreate,
    ClaimLinkResponse,
    ClaimListResponse,
    ClaimResponse,
    ClaimVersionCreate,
    ClaimVersionResponse,
    StanceObservationCreate,
    StanceObservationResponse,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}/claims", tags=["claims"])


def _uid(user: User) -> UUID:
    return cast(UUID, user.id)


def _status(response: Response, replayed: bool) -> None:
    if replayed:
        response.status_code = 200


@router.get("", response_model=ClaimListResponse)
async def list_claims(
    project_id: UUID,
    draft_id: Optional[UUID] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimListResponse:
    """Each claim's tip version, or its version pinned to ``draft_id``."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await claims_service.list_claims(db, context, draft_id)


@router.get("/{claim_id:uuid}", response_model=ClaimDetailResponse)
async def get_claim(
    project_id: UUID,
    claim_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimDetailResponse:
    """The claim's full version, link, observation and assessment history."""
    context = await resolve_project(db, project_id, _uid(current_user))
    return await claims_service.get_claim(db, context, claim_id)


@router.post("", status_code=201, response_model=ClaimResponse)
async def create_claim(
    project_id: UUID,
    request: ClaimCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimResponse:
    """Create a claim over an exact draft passage (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await claims_service.create_claim(
        db, context, _uid(current_user), request
    )
    _status(response, replayed)
    return result


@router.post(
    "/{claim_id:uuid}/versions", status_code=201, response_model=ClaimVersionResponse
)
async def create_claim_version(
    project_id: UUID,
    claim_id: UUID,
    request: ClaimVersionCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimVersionResponse:
    """Append a reworded version superseding the claim's tip (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await claims_service.create_version(
        db, context, claim_id, _uid(current_user), request
    )
    _status(response, replayed)
    return result


@router.post(
    "/{claim_id:uuid}/links", status_code=201, response_model=ClaimLinkResponse
)
async def create_claim_link(
    project_id: UUID,
    claim_id: UUID,
    request: ClaimLinkCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimLinkResponse:
    """Link, re-point or withdraw evidence on the claim's tip version (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await claims_service.link(
        db, context, claim_id, _uid(current_user), request
    )
    _status(response, replayed)
    return result


@router.post(
    "/{claim_id:uuid}/links/{link_id:uuid}/observations",
    status_code=201,
    response_model=StanceObservationResponse,
)
async def create_stance_observation(
    project_id: UUID,
    claim_id: UUID,
    link_id: UUID,
    request: StanceObservationCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StanceObservationResponse:
    """Snapshot the evidence meter's stance for this link's source (EDIT)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.EDIT
    )
    result, replayed = await claims_service.observe_stance(
        db, context, claim_id, link_id, _uid(current_user), request
    )
    _status(response, replayed)
    return result


@router.post(
    "/{claim_id:uuid}/assessments",
    status_code=201,
    response_model=ClaimAssessmentResponse,
)
async def create_claim_assessment(
    project_id: UUID,
    claim_id: UUID,
    request: ClaimAssessmentCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ClaimAssessmentResponse:
    """Assess the claim's tip version (ADJUDICATOR role only)."""
    context = await resolve_project(
        db, project_id, _uid(current_user), ResearchAction.ADJUDICATE
    )
    result, replayed = await claims_service.assess(
        db, context, claim_id, _uid(current_user), request
    )
    _status(response, replayed)
    return result
