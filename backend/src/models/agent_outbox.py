"""Transactional-outbox record of an agent run's dispatch intent (P0-C).

One dispatch row per accepted submission, plus one ``handoff.resume`` row per
finished handoff child. The dispatch row is written in the SAME transaction as
the user message, the ``agent_runs`` row and the run's ``run.created`` event.
That is the whole point: a client that received an ``accepted`` frame is
guaranteed a durable record that the run was meant to be dispatched, so a
crash between acceptance and execution is recoverable instead of silently lost.

**Scope of this table today: a durable intent record only.** Dispatch itself
still happens exactly as it did before — the ``/stream`` path runs the graph
in-process, right after the accept transaction commits, and stamps
``status='dispatched'`` best-effort once the graph iterator is open. There is
no relay/poller reading ``status='pending'`` yet; building one is deliberately
a later work order. Anything that grows into that role must treat a ``pending``
row as "dispatch may or may not have happened" and re-dispatch idempotently
(the run id is the dedupe key).

All access goes through ``src/services/agent/agent_submission_service.py`` —
do not query this table directly, the same rule ``agent_run_service`` holds for
``agent_runs`` and ``run_event_store`` holds for ``agent_run_events``. Tenant
rule: ``organization_id`` is nullable (org-less users exist) and every
user-facing read MUST compare it null-safely — never ``str(org)``.
"""

from typing import Any

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB

from src.shared.enums import AgentOutboxStatus

from .base import GUID, BaseModel

# Rendered into a CHECK constraint so the column domain cannot drift from
# AgentOutboxStatus without a migration. Shared with the Alembic revision
# (c7d8e9f0a1b2) so the constraint text has one source.
AGENT_OUTBOX_STATUS_CHECK = (
    "status IN (" + ", ".join(f"'{s.value}'" for s in AgentOutboxStatus) + ")"
)


class AgentOutbox(BaseModel):
    """One durable dispatch intent for an accepted agent run."""

    __tablename__ = "agent_outbox"

    run_id = Column(
        String(36),
        ForeignKey("agent_runs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Tenancy. Nullable and never stringified, mirroring
    # ``agent_runs.organization_id`` / ``agent_run_events.organization_id``.
    # Annotated because ``GUID`` is an untyped legacy ``TypeDecorator``, so mypy
    # cannot infer the column's type parameter on its own.
    organization_id: "Column[Any]" = Column(GUID(), nullable=True)

    # What kind of dispatch was intended (e.g. "agent.stream.execute"). Kept a
    # free-form short string rather than an enum: a relay dispatching to a new
    # backend must be addable without a migration, and the value is never used
    # in a SQL predicate built from user input.
    kind = Column(String(64), nullable=False)
    # Bounded, non-secret dispatch arguments — ids and flags the dispatcher
    # needs, never prompts, tool results or credentials.
    payload = Column(JSONB, nullable=False, default=dict, server_default="{}")

    status = Column(
        String(16),
        nullable=False,
        default=AgentOutboxStatus.PENDING.value,
        server_default=AgentOutboxStatus.PENDING.value,
    )
    dispatched_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A future relay's claim scan: oldest pending rows first.
        Index("idx_agent_outbox_status_created", "status", "created_at"),
        # Correlate an outbox row back to its run (and, tenant-scoped, to an
        # org's pending dispatches).
        Index("idx_agent_outbox_run", "run_id"),
        Index("idx_agent_outbox_org_status", "organization_id", "status"),
        # One dispatch intent per run and kind: the accept transaction writes
        # the run's own dispatch row once, and a finished handoff child gets
        # exactly one ``handoff.resume`` row (finalize_submission). A retried
        # submission resolves to the existing run instead of a second row.
        Index("uq_agent_outbox_run_kind", "run_id", "kind", unique=True),
        CheckConstraint(AGENT_OUTBOX_STATUS_CHECK, name="ck_agent_outbox_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentOutbox(run_id={self.run_id!r}, kind={self.kind!r}, "
            f"status={self.status!r})>"
        )
