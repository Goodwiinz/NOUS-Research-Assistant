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
    # The grant's binding: a project grant names its Collection, a workspace
    # grant its workspace. Native requests are project-bound.
    project_id: UUID | None
    workspace_id: UUID | None = None
    thread_id: UUID | None = None
    run_id: UUID | None = None
    # None only for trusted native NOUS requests.
    grant_id: UUID | None = None
    # Consumed grant request behind the grant, when it was issued by consent.
    consent_id: UUID | None = None
    # The scopes of that grant. request_action checks each action against the
    # one it needs (tools:write, or library:write for the reversible library
    # actions, which then run without a per-action decision). Empty: nothing.
    scopes: frozenset[str] = frozenset()


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
    project_id: UUID | None
    project_label: str | None
    # Set when the grant behind the action is bound to a workspace. The review
    # page shows the workspace whenever there is no project.
    workspace_id: UUID | None = None
    workspace_label: str | None = None
    # Whether the target (project, else workspace) is still live.
    project_available: bool
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
