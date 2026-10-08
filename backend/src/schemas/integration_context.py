"""Shared, project- or workspace-scoped integration identity and pairing contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Scopes some route enforces, plus library:write (consented from Plan 07 slice
# 1, enforced by the auto-run actions in slice 3). context:read is enforced by
# GET /integrations/context (#1784) and, like harness:execute, artifacts:publish
# and the handoff scopes, is refused for a workspace grant (see
# services/integrations/context.py). A scope is added in the PR that checks it.
STANDARD_SCOPES = frozenset(
    {
        "harness:execute",
        "tools:read",
        "tools:write",
        "context:read",
        "artifacts:publish",
        "handoff:read",
        "handoff:write",
        # Plan 07: workspace library. read = list folders; write = reversible
        # folder/document changes run without per-action approval.
        "library:read",
        "library:write",
    }
)


class IntegrationContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    # Exactly one binding. project_id: one Collection (today's model).
    # workspace_id: every live Collection the user can see in that workspace.
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    thread_id: UUID | None = None
    run_id: UUID | None = None
    grant_id: UUID
    # The consumed consent request the grant was exchanged from; None for
    # trusted internal issuance. Renewed grants share it.
    consent_id: UUID | None = None
    # The scopes of the grant this context was resolved from, so a service can
    # authorize each tool against them. Empty for a context built without
    # resolving a grant, which therefore passes no scope check.
    scopes: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _one_binding(self) -> "IntegrationContext":
        if (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self


class IssuedGrant(BaseModel):
    token: str
    grant_id: UUID


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    device_id: UUID
    scopes: set[str]
    thread_id: UUID | None = None

    @model_validator(mode="after")
    def _one_binding(self) -> "GrantRequestCreate":
        if (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self


GrantRequestStatus = Literal["pending", "approved", "denied", "expired", "consumed"]


class GrantRequestDTO(BaseModel):
    id: UUID
    status: GrantRequestStatus
    expires_at: datetime
    approval_url: str
    project_id: UUID | None
    workspace_id: UUID | None = None
    device_id: UUID
    scopes: set[str]
    project_label: str | None
    workspace_label: str | None = None
    device_label: str
    thread_id: UUID | None = None
    thread_label: str | None = None


class GrantDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)


class DeviceDTO(DeviceCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


class DeviceListItemDTO(DeviceDTO):
    """A paired computer as the chat composer lists it."""

    # Chats this computer can run Codex in when every live run consent
    # (harness:execute) it holds is bound to a chat (Plan 06 slice 2). Empty
    # when one of them is project-wide, or when it holds none: the composer
    # then does not restrict it. The mint stays the authority.
    bound_thread_ids: list[UUID]


class WorkspaceBindingCreate(DeviceCreate):
    workspace_id: UUID
    project_id: UUID


class WorkspaceBindingDTO(WorkspaceBindingCreate):
    model_config = ConfigDict(from_attributes=True)
