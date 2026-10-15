"""Shared project-, workspace- or account-scoped integration identity and pairing contracts."""

from datetime import datetime
from typing import Any, Literal
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


# project: one Collection. workspace: every live Collection of one workspace.
# account: every live, unarchived Collection of the user's organization
# workspaces that they own or belong to, evaluated per statement
# (services/integrations/context.account_scope_condition).
BindingKind = Literal["project", "workspace", "account"]


def derive_binding_kind(
    project_id: object, workspace_id: object
) -> Literal["project", "workspace"] | None:
    """The kind a binding without a stated kind has. Never "account": both
    NULL without the kind means "bound to nothing" and is denied."""
    if project_id is not None and workspace_id is None:
        return "project"
    if workspace_id is not None and project_id is None:
        return "workspace"
    return None


def binding_matches_kind(kind: str, project_id: object, workspace_id: object) -> bool:
    """Whether the two binding columns have the shape ``kind`` requires."""
    if kind == "project":
        return project_id is not None and workspace_id is None
    if kind == "workspace":
        return workspace_id is not None and project_id is None
    if kind == "account":
        return project_id is None and workspace_id is None
    return False


class IntegrationContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    # project_id: one Collection. workspace_id: every live Collection the user
    # can see in that workspace. Neither: an account context (binding_kind).
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    # Derived from project_id / workspace_id when not given; an account
    # context must name it. The default is never used for validated dict input
    # (_derive_kind, a before-validator, always fills the field); model_construct
    # and model_copy skip the validator.
    binding_kind: BindingKind = "project"
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

    @model_validator(mode="before")
    @classmethod
    def _derive_kind(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("binding_kind") is None:
            kind = derive_binding_kind(data.get("project_id"), data.get("workspace_id"))
            if kind is None:
                raise ValueError("exactly one of project_id or workspace_id")
            data = {**data, "binding_kind": kind}
        return data

    @model_validator(mode="after")
    def _one_binding(self) -> "IntegrationContext":
        if not binding_matches_kind(
            self.binding_kind, self.project_id, self.workspace_id
        ):
            raise ValueError("binding does not match binding_kind")
        return self


class IssuedGrant(BaseModel):
    token: str
    grant_id: UUID


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    # The whole account (ACCOUNT_CONNECTIONS_ENABLED): no project, workspace
    # or chat.
    account: bool = False
    device_id: UUID
    scopes: set[str]
    thread_id: UUID | None = None

    @model_validator(mode="after")
    def _one_binding(self) -> "GrantRequestCreate":
        if self.account:
            if self.project_id is not None or self.workspace_id is not None:
                raise ValueError("account excludes project_id and workspace_id")
            if self.thread_id is not None:
                raise ValueError("an account connection cannot be bound to a chat")
        elif (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self

    @property
    def binding_kind(self) -> BindingKind:
        if self.account:
            return "account"
        return "project" if self.project_id is not None else "workspace"


GrantRequestStatus = Literal["pending", "approved", "denied", "expired", "consumed"]


class AccountSummaryDTO(BaseModel):
    """What an account approval covers at the moment it is shown."""

    workspace_count: int
    project_count: int


class GrantRequestDTO(BaseModel):
    id: UUID
    status: GrantRequestStatus
    expires_at: datetime
    approval_url: str
    binding_kind: BindingKind
    project_id: UUID | None
    workspace_id: UUID | None = None
    device_id: UUID
    scopes: set[str]
    project_label: str | None
    workspace_label: str | None = None
    device_label: str
    thread_id: UUID | None = None
    thread_label: str | None = None
    # Account requests only, computed each time the request is read.
    account_summary: AccountSummaryDTO | None = None


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
