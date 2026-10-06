"""Shared, project-scoped integration identity and pairing HTTP contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Scopes some route enforces, plus context:read (kept for PR #1784, which
# enforces it). A scope is added in the PR that checks it.
STANDARD_SCOPES = frozenset(
    {
        "harness:execute",
        "tools:read",
        "tools:write",
        "context:read",
        "artifacts:publish",
    }
)


class IntegrationContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    project_id: UUID
    thread_id: UUID | None = None
    run_id: UUID | None = None
    grant_id: UUID
    # The consumed consent request the grant was exchanged from; None for
    # trusted internal issuance. Renewed grants share it.
    consent_id: UUID | None = None


class IssuedGrant(BaseModel):
    token: str
    grant_id: UUID


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    device_id: UUID
    scopes: set[str]
    thread_id: UUID | None = None


GrantRequestStatus = Literal["pending", "approved", "denied", "expired", "consumed"]


class GrantRequestDTO(BaseModel):
    id: UUID
    status: GrantRequestStatus
    expires_at: datetime
    approval_url: str
    project_id: UUID
    device_id: UUID
    scopes: set[str]
    project_label: str
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


class WorkspaceBindingCreate(DeviceCreate):
    workspace_id: UUID
    project_id: UUID


class WorkspaceBindingDTO(WorkspaceBindingCreate):
    model_config = ConfigDict(from_attributes=True)
