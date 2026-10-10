"""Hashed short-lived grants and immutable browser consent requests."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class IntegrationGrantRequest(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_grant_requests"
    # Exactly one binding: one Collection, or every live Collection of one
    # workspace (Plan 07).
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NULL) <> (workspace_id IS NULL)",
            name="ck_integration_grant_requests_one_binding",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
    device_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("bridge_devices.id"), nullable=False, index=True
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationGrant(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_grants"
    # Exactly one binding: one Collection, or every live Collection of one
    # workspace (Plan 07).
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NULL) <> (workspace_id IS NULL)",
            name="ck_integration_grants_one_binding",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
    device_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("bridge_devices.id"), index=True
    )
    request_id: Mapped[UUID | None] = mapped_column(
        GUID(),
        ForeignKey("integration_grant_requests.id", ondelete="SET NULL"),
        index=True,
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
