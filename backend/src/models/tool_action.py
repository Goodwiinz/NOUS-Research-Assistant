"""Durable mutation requests shared by native NOUS and external harnesses.

One row per requested action. The row is the only source of truth for whether
the user decided, whether the effect ran, and what it produced; a harness can
only read it, never self-attest a decision.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel

# awaiting_approval -> approved -> executing -> succeeded | outcome_unknown
#                   \-> failed (denied, revoked, or a rolled-back effect)
ACTION_STATES = (
    "awaiting_approval",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "outcome_unknown",
)


class IntegrationToolAction(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_tool_actions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "invocation_id",
            name="uq_integration_tool_actions_invocation",
        ),
        # project_id is the action's target Collection and workspace_id the
        # binding of the grant that authorised it. A workspace-grant action
        # aimed at one Collection sets both, so only "at least one" holds.
        CheckConstraint(
            "project_id IS NOT NULL OR workspace_id IS NOT NULL",
            name="ck_integration_tool_actions_some_binding",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    # None for trusted native requests; external requests always carry one and
    # the grant is revalidated immediately before the effect.
    grant_id: Mapped[UUID | None] = mapped_column(GUID())
    # The consumed grant request behind the grant; renewals keep it, user
    # revocation ends it. Authority and read scope follow this, not the token.
    consent_id: Mapped[UUID | None] = mapped_column(GUID(), index=True)
    invocation_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    # sha256 of the canonical (tool_name, arguments) so a replay with a
    # different payload under the same invocation id is a conflict.
    argument_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    approved: Mapped[bool | None] = mapped_column(Boolean)
    decided_by: Mapped[UUID | None] = mapped_column(GUID())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)
