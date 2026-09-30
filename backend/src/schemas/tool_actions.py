"""Transport shapes for durable, approval-gated tool actions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from src.schemas.integration_tools import ToolResult

ActionState = Literal[
    "awaiting_approval",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "outcome_unknown",
]


class ActionActor(BaseModel):
    """Server-derived identity of the requester; never built from tool arguments."""

    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    project_id: UUID
    thread_id: UUID | None = None
    run_id: UUID | None = None
    # None only for trusted native NOUS requests.
    grant_id: UUID | None = None
    # Consumed grant request behind the grant, when it was issued by consent.
    consent_id: UUID | None = None


class ActionStatus(BaseModel):
    invocation_id: UUID
    state: ActionState
    tool_name: str
    result: ToolResult | None = None
    # Absolute browser link while the action waits for the user's decision.
    approval_url: str | None = None


class ActionReview(BaseModel):
    """What the interactive owner sees before deciding: the exact stored target."""

    invocation_id: UUID
    state: ActionState
    tool_name: str
    project_id: UUID
    project_label: str
    title: str
    content: str
    tags: list[str]
    requested_at: datetime
    decided_at: datetime | None = None
    result: ToolResult | None = None
    last_error: str | None = None


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
