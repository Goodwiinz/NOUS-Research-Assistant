"""Browser view of connected devices and the access each one holds."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ConnectionConsent(BaseModel):
    request_id: UUID
    project_id: UUID
    project_label: str
    scopes: list[str]
    status: str
    approved_at: datetime | None = None


class ConnectedDevice(BaseModel):
    device_id: UUID
    device_label: str
    connected_at: datetime
    consents: list[ConnectionConsent]
