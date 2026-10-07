"""Selected-context transport; the service owns authorization and commits.

The browser owner lists and saves the shared memories; a harness with a
`context:read` grant only reads them. Neither side can widen the other.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import (
    require_integration_context,
    require_interactive_user,
)
from src.core.config import settings
from src.core.database import get_db
from src.models.user import User
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_selected_context import (
    ContextOptions,
    ContextSelectionUpdate,
)
from src.schemas.integration_tools import ToolResult
from src.services.integrations.selected_context import (
    ContextNotFound,
    ContextSelectionInvalid,
    ContextSelectionTooLarge,
    context_options,
    read_selected_context,
    save_selection,
)

router = APIRouter(prefix="/context", tags=["integrations"])


@router.get("/options", response_model=ContextOptions)
async def get_context_options(
    request_id: UUID,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> ContextOptions:
    try:
        return await context_options(db, user, request_id)
    except ContextNotFound as error:
        raise HTTPException(404, "Context request not found") from error


@router.put("/selection", response_model=ContextOptions)
async def put_context_selection(
    update: ContextSelectionUpdate,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> ContextOptions:
    try:
        return await save_selection(db, user, update.request_id, update.memory_ids)
    except ContextNotFound as error:
        raise HTTPException(404, "Context request not found") from error
    except ContextSelectionInvalid as error:
        raise HTTPException(
            422, "Selected memories must belong to this project"
        ) from error
    except ContextSelectionTooLarge as error:
        raise HTTPException(
            422, "Selected memories exceed the 64 KiB sharing limit"
        ) from error


@router.get("", response_model=ToolResult)
async def get_selected_context(
    context: IntegrationContext = Depends(require_integration_context("context:read")),
    db: AsyncSession = Depends(get_db),
) -> ToolResult:
    if not settings.NOUS_MCP_ENABLED:
        raise HTTPException(503, "NOUS integration tools are disabled")
    return await read_selected_context(db, context)
