"""Shared, project-scoped integration identity and pairing HTTP contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

STANDARD_SCOPES = frozenset(
    {
        "harness:execute",
        "tools:read",
        "tools:write",
        "context:read",
        "artifacts:publish",
        "artifacts:read",
        "artifacts:edit",
        "artifacts:share",
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


class IssuedGrant(BaseModel):
    token: str
    grant_id: UUID


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    device_id: UUID
    scopes: set[str]


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
