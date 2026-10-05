"""Chat handoff transport; the handoff service owns validation and commits.

The chat is always the grant's bound thread, never a request parameter. Reads
are ungated (like action status); new writes need ``NOUS_MCP_ENABLED``.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_integration_context
from src.core.config import settings
from src.core.database import get_db
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_handoff import (
    HandoffConflictBody,
    HandoffCreate,
    HandoffDTO,
)
from src.services.integrations import handoffs
from src.services.integrations.context import IntegrationAccessDenied

router = APIRouter(prefix="/handoffs", tags=["integrations"])
_READ = Depends(require_integration_context("handoff:read"))
_WRITE = Depends(require_integration_context("handoff:write"))
_DENIED = "Integration access denied"


@router.get("/latest", response_model=HandoffDTO)
async def read_latest_handoff(
    context: IntegrationContext = _READ,
    db: AsyncSession = Depends(get_db),
) -> HandoffDTO:
    try:
        latest = await handoffs.read_latest(db, context)
    except IntegrationAccessDenied as error:
        raise HTTPException(403, _DENIED) from error
    if latest is None:
        raise HTTPException(404, "No handoff for this chat")
    return latest


@router.post(
    "",
    response_model=HandoffDTO,
    responses={409: {"model": HandoffConflictBody, "description": "Merge with latest"}},
)
async def save_handoff(
    payload: HandoffCreate,
    context: IntegrationContext = _WRITE,
    db: AsyncSession = Depends(get_db),
) -> HandoffDTO | JSONResponse:
    if not settings.NOUS_MCP_ENABLED:
        raise HTTPException(503, "NOUS integration tools are disabled")
    try:
        return await handoffs.save(db, context, payload)
    except IntegrationAccessDenied as error:
        raise HTTPException(403, _DENIED) from error
    except handoffs.HandoffInvalid as error:
        raise HTTPException(422, str(error)) from error
    except handoffs.HandoffConflict as error:
        # One envelope for every 409; the rejected payload is never stored.
        body = HandoffConflictBody(
            detail=(
                "Handoff is stale; merge with the latest version and retry"
                if error.latest is not None
                else "Handoff version conflict"
            ),
            latest=error.latest,
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))
