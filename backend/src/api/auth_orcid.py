"""ORCID OAuth ``/authenticate`` (GOO-316): transport only.

``start`` returns the authorize URL (the API is bearer-authenticated, so the
client navigates to it); the frontend's ``/auth/orcid/callback`` page sends
``code`` and ``state`` back here. The callback verifies the signed state for
the session user, exchanges the code and keeps a non-secret receipt (never a
token). Both answer 503 without ``ORCID_CLIENT_ID``; failures are one safe
code. A receipt is a user identity record, not a project decision.
"""

from typing import Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.research import statements_service as service
from src.shared.statements_schemas import (
    OrcidAuthenticationListResponse,
    OrcidAuthenticationResponse,
    OrcidStartResponse,
)

router = APIRouter(prefix="/api/v1/auth/orcid", tags=["orcid"])


@router.get(
    "/start",
    response_model=OrcidStartResponse,
    responses={503: {"description": "ORCID not configured"}},
)
async def orcid_start(
    current_user: User = Depends(get_current_user),
) -> OrcidStartResponse:
    """The ORCID authorize URL with a signed, 10-minute state."""
    return service.orcid_start(cast(UUID, current_user.id))


@router.get(
    "/callback",
    response_model=OrcidAuthenticationResponse,
    responses={
        400: {"description": "orcid_denied or orcid_state_invalid"},
        502: {"description": "orcid_exchange_failed"},
        503: {"description": "ORCID not configured"},
    },
)
async def orcid_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrcidAuthenticationResponse:
    """Complete the flow for the session user and keep the receipt."""
    return await service.orcid_callback(
        db, cast(UUID, current_user.id), code, state, error
    )


@router.get("/authentications", response_model=OrcidAuthenticationListResponse)
async def orcid_authentications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrcidAuthenticationListResponse:
    """The current user's own receipts only."""
    return await service.list_orcid(db, cast(UUID, current_user.id))
