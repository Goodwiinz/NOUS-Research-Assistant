"""Durable native-session identity and uncertain local-workspace ownership."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, Base


class HarnessSession(Base):
    __tablename__ = "harness_sessions"
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id"), nullable=False, unique=True
    )
    grant_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("integration_grants.id"), nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    # Native identifiers are filled by delivery reconciliation, never guessed on retry.
    provider_session_id: Mapped[str | None] = mapped_column(String(255))
    provider_turn_id: Mapped[str | None] = mapped_column(String(255))
    observation: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_locked: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "workspace_id"],
            [
                "bridge_workspace_bindings.device_id",
                "bridge_workspace_bindings.workspace_id",
            ],
        ),
        Index(
            "uq_harness_active_workspace",
            "device_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("workspace_locked"),
            sqlite_where=text("workspace_locked"),
        ),
        CheckConstraint(
            "observation IN ('unknown', 'running', 'completed', 'failed', 'interrupted')",
            name="ck_harness_observation",
        ),
    )
