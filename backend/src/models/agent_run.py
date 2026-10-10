"""Durable Postgres projection of agent execution jobs (``agent_runs``).

The agent job store is Redis-first (``src/services/agent/job_store.py``,
TTL 1h). That alone is a single point of failure: a Redis failover 404s every
poller and orphans HITL confirm state (audit findings X1/D7). This table is a
write-through projection of every job-status transition so the *status* of a
run survives Redis loss and a future sweeper can reap runs stuck ``running``.

Redis stays authoritative during rollout: writes here are log-and-continue
(never fail the turn), and the poll endpoint only reads this table when Redis
misses. Full result payloads / confirmation details are NOT projected — only
the lifecycle status and error, which is what a failed-over poller needs to
stop spinning.

All access goes through ``src/services/agent/agent_run_service.py`` — do not
query this table directly. Tenant rule: user-facing reads MUST filter
``organization_id`` (and ``user_id``); only the system sweeper may scan
cross-tenant.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from src.shared.enums import JobStatus

from .base import GUID, Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Rendered into a CHECK constraint so the column domain cannot drift from
# JobStatus without a migration. "error" is intentionally absent: the service
# normalizes the legacy alias to "failed" before any write (see JobStatus).
# Shared with the Alembic migration so the constraint text has one source.
AGENT_RUN_STATUS_CHECK = (
    "status IN (" + ", ".join(f"'{s.value}'" for s in JobStatus) + ")"
)


class AgentRun(Base):
    """One row per agent execution job (job_id from ``POST /agent/execute``)."""

    __tablename__ = "agent_runs"

    # The job id handed to pollers — a server-generated uuid4 string.
    job_id = Column(String(36), primary_key=True)

    # Tenancy — who owns the run. Nullable to mirror User.organization_id
    # (SET NULL FK); reads filter on both, so an org-less row is only visible
    # to an org-less owner.
    organization_id = Column(GUID(), nullable=True)
    user_id = Column(GUID(), nullable=True)

    # Correlation — the thread this run produces a turn for. Was a free-form
    # String(255) (uuid or job-id fallback); the run-events migration nulls
    # dangling correlations and converts to a real FK.
    thread_id = Column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id = Column(
        GUID(), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    project_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )

    # Transcript linkage — the committed user turn that started the run and
    # (once terminal) the assistant message it projected.
    user_message_id = Column(
        GUID(), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    assistant_message_id = Column(
        GUID(), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )

    # Frozen tool registry + approved skill versions for the whole run;
    # HITL resume reuses this snapshot, never creates another.
    runtime_snapshot_id = Column(
        GUID(),
        ForeignKey("agent_runtime_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )

    execution_provider = Column(
        String(16), nullable=False, default="nous", server_default="nous"
    )

    # Lifecycle status; values constrained to the JobStatus domain.
    status = Column(String(32), nullable=False)

    # Client idempotency: the raw client_message_id for the turn plus the
    # derived idempotency_key. Unique per user where not null — tenant-scoped
    # so keys cannot collide across users/organizations.
    client_message_id = Column(String(255), nullable=True)
    idempotency_key = Column(String(255), nullable=True)

    # High-water mark of agent_run_events.seq — bumped monotonically by
    # run_event_store.append_event in the same transaction as the event insert.
    last_event_seq = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Lease fields for the sweeper/worker: a worker claims a stuck run by
    # writing its identity + expiry atomically (see claim_lease in the service).
    # lease_generation increments on each takeover so a fenced-out worker's
    # late writes can be rejected.
    lease_owner = Column(String(255), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    lease_generation = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Lifecycle timestamps (created_at/updated_at below are row bookkeeping).
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)

    # Structured failure: machine-readable code + client-safe message.
    error_code = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)

    # Terminal accounting and free-form run annotations (attribute is
    # run_metadata because SQLAlchemy reserves .metadata).
    usage = Column(JSONB, nullable=True)
    run_metadata = Column(JSONB, nullable=True)

    # Client-side defaults keep ORM-written timestamps timezone-aware on every
    # backend (sqlite tests included); server_default covers raw SQL inserts.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        # Tenant-scoped listing ("org X's recent runs").
        Index("idx_agent_runs_org_updated", "organization_id", "updated_at"),
        # Sweeper scan: non-terminal runs ordered by staleness.
        Index("idx_agent_runs_status_updated", "status", "updated_at"),
        # Tenant-scoped idempotency: a retried /execute resolves to its
        # existing run; keys cannot collide across users.
        Index(
            "uq_agent_runs_user_idempotency_key",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        # One non-terminal run per thread — the concurrency invariant the run
        # API converts into a conflict instead of starting a second writer.
        Index(
            "uq_agent_runs_active_thread",
            "thread_id",
            unique=True,
            postgresql_where=text(
                "thread_id IS NOT NULL AND status IN "
                "('queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering')"
            ),
            sqlite_where=text(
                "thread_id IS NOT NULL AND status IN "
                "('queued', 'running', 'awaiting_confirmation', 'stopping', 'recovering')"
            ),
        ),
        CheckConstraint(
            "execution_provider IN ('nous', 'codex')", name="ck_agent_runs_provider"
        ),
        CheckConstraint(AGENT_RUN_STATUS_CHECK, name="ck_agent_runs_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentRun(job_id={self.job_id!r}, status={self.status!r}, "
            f"org={self.organization_id})>"
        )
