"""Hashed short-lived grants and immutable browser consent requests."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel

# One binding per kind (migration ac01_account_bindings). "account" binds
# neither column: what it covers is evaluated per statement
# (services/integrations/context.account_scope_condition).
BINDING_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)


def derived_binding_kind(context: Any) -> str | None:
    """Default ``binding_kind`` for a writer that names only ``project_id`` or
    ``workspace_id`` (every writer that predates ac01).

    Never ``"account"``: an account row must say so, and a row naming neither
    column gets None and fails NOT NULL. Context-sensitive defaults do not work
    with a multi-VALUES ``insert(...).values([...])``; such a writer passes
    ``binding_kind`` itself.
    """
    params = context.get_current_parameters()
    if params.get("workspace_id") is not None:
        return "workspace"
    if params.get("project_id") is not None:
        return "project"
    return None


class IntegrationGrantRequest(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_grant_requests"
    __table_args__ = (
        CheckConstraint(
            BINDING_KIND_CHECK, name="ck_integration_grant_requests_binding_kind"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    # "project" | "workspace" | "account" (BindingKind).
    binding_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=derived_binding_kind
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
    __table_args__ = (
        CheckConstraint(BINDING_KIND_CHECK, name="ck_integration_grants_binding_kind"),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    # The grant's own kind. A run's grant under an account approval is
    # "project" while its request_id names the "account" consent.
    binding_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=derived_binding_kind
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
