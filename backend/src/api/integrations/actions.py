"""Durable action transport; the action service owns the guard and commits.

Requests and status reads use the CLI JWT + `tools:write` grant. The decision
route is browser-only: a verified CLI token is refused even without a grant
header, so a harness can never self-attest the user's choice.
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
from src.schemas.integration_tools import ToolInvocation
from src.schemas.tool_actions import ActionActor, ActionDecision, ActionStatus
from src.services.agent.tool_actions import (
    ActionConflict,
    ActionNotFound,
    ToolActionArgumentError,
    decide_action,
    get_action_status,
    request_action,
)

router = APIRouter(prefix="/actions", tags=["integrations"])
_WRITE = Depends(require_integration_context("tools:write"))


def _actor(context: IntegrationContext) -> ActionActor:
    return ActionActor(
        user_id=context.user_id,
        organization_id=context.organization_id,
        project_id=context.project_id,
        thread_id=context.thread_id,
        run_id=context.run_id,
        grant_id=context.grant_id,
        consent_id=context.consent_id,
    )


@router.post("", response_model=ActionStatus)
async def create_action(
    invocation: ToolInvocation,
    context: IntegrationContext = _WRITE,
    db: AsyncSession = Depends(get_db),
) -> ActionStatus:
    # New writes are gated; status reads of existing actions are not.
    if not settings.NOUS_MCP_ENABLED:
        raise HTTPException(503, "NOUS integration tools are disabled")
    try:
        return await request_action(db, _actor(context), invocation)
    except ToolActionArgumentError as error:
        raise HTTPException(422, str(error)) from error
    except ActionConflict as error:
        raise HTTPException(
            409, "Action request conflicts with an existing one"
        ) from error


@router.get("/{invocation_id}", response_model=ActionStatus)
async def read_action(
    invocation_id: UUID,
    context: IntegrationContext = _WRITE,
    db: AsyncSession = Depends(get_db),
) -> ActionStatus:
    try:
        return await get_action_status(db, _actor(context), invocation_id)
    except ActionNotFound as error:
        raise HTTPException(404, "Action not found") from error


@router.post("/{invocation_id}/decision", response_model=ActionStatus)
async def decide(
    invocation_id: UUID,
    decision: ActionDecision,
    user: User = Depends(require_interactive_user),
    db: AsyncSession = Depends(get_db),
) -> ActionStatus:
    try:
        return await decide_action(db, user, invocation_id, approved=decision.approved)
    except ActionNotFound as error:
        raise HTTPException(404, "Action not found") from error
    except ActionConflict as error:
        raise HTTPException(409, "Action has already been decided") from error
