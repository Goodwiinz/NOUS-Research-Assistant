"""Browser view of connected devices and the access each one holds."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ConnectionConsent(BaseModel):
    request_id: UUID
    # Exactly one binding (ck_integration_grant_requests_one_binding): one
    # project, or one workspace and every live project in it (Plan 07).
    kind: Literal["project", "workspace"]
    project_id: UUID | None
    project_label: str | None
    workspace_id: UUID | None
    workspace_label: str | None
    # A chat-bound consent (Plan 06 slice 2) mints only from that chat. The
    # label is null once the chat is deleted or no longer in the project.
    thread_id: UUID | None
    thread_label: str | None
    scopes: list[str]
    status: str
    approved_at: datetime | None = None


class ConnectedDevice(BaseModel):
    device_id: UUID
    device_label: str
    connected_at: datetime
    consents: list[ConnectionConsent]
