"""Durable native-session identity and uncertain local-workspace ownership."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
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
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    source_id: Mapped[str | None] = mapped_column(String(128))
    source_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
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


class HarnessCommand(Base):
    """Durable per-action outbox; retries preserve command identity and generation."""

    __tablename__ = "harness_commands"
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("harness_sessions.run_id"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    __table_args__ = (
        Index(
            "uq_harness_command_kind",
            "run_id",
            "generation",
            "kind",
            unique=True,
            postgresql_where=text("kind IN ('start', 'interrupt')"),
            sqlite_where=text("kind IN ('start', 'interrupt')"),
        ),
    )


class HarnessReceipt(Base):
    """Source-to-canonical cursor mapping, committed with each event."""

    __tablename__ = "harness_receipts"
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("harness_sessions.run_id"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_seq: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        Index(
            "uq_harness_source_seq",
            "run_id",
            "generation",
            "source_id",
            "source_seq",
            unique=True,
        ),
    )
