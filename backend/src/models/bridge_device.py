"""Owner-bound devices and opaque local workspace labels (never filesystem paths)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class BridgeDevice(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "bridge_devices"
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceBinding(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "bridge_workspace_bindings"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "workspace_id", name="uq_bridge_device_workspace"
        ),
    )
    device_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("bridge_devices.id"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
