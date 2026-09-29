"""Scoped read-tool transport; the gateway service owns authorization."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.integrations.auth import require_integration_context
from src.core.config import settings
from src.core.database import get_db
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolDescriptorDTO, ToolInvocation, ToolResult
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.read_tools import (
    ToolArgumentError,
    invoke_read,
    list_read_tools,
)

router = APIRouter(prefix="/tools", tags=["integrations"])


def _require_enabled() -> None:
    if not settings.NOUS_MCP_ENABLED:
        raise HTTPException(503, "NOUS integration tools are disabled")


@router.get("", response_model=list[ToolDescriptorDTO])
async def list_tools(
    _context: IntegrationContext = Depends(require_integration_context("tools:read")),
) -> list[ToolDescriptorDTO]:
    _require_enabled()
    return list_read_tools()


@router.post("/read", response_model=ToolResult)
async def read_tool(
    invocation: ToolInvocation,
    context: IntegrationContext = Depends(require_integration_context("tools:read")),
    db: AsyncSession = Depends(get_db),
) -> ToolResult:
    _require_enabled()
    try:
        return await invoke_read(db, context, invocation)
    except ToolArgumentError as error:
        raise HTTPException(422, str(error)) from error
    except IntegrationAccessDenied as error:
        raise HTTPException(403, "Integration access denied") from error
