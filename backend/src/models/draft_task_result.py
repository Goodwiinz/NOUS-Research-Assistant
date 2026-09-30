"""Retained terminal outcome of one draft-generation task (GOO-297).

One row per task. Inserted ``running`` in the transaction that accepts the
task; moved to a terminal state exactly once by a guarded
``UPDATE ... WHERE state = 'running'``. Redis stays the progress cache.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)

from .base import GUID, Base

DRAFT_TASK_STATES = ("running", "completed", "failed", "cancelled", "interrupted")


class DraftTaskResult(Base):
    """Terminal state of a draft task, bound to the exact draft it produced."""

    __tablename__ = "draft_task_results"

    task_id = Column(String(64), primary_key=True)
    collection_id: Column[uuid.UUID] = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Column[uuid.UUID] = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    state = Column(String(16), nullable=False)
    # No FK on purpose: delete_draft must keep working and this record must
    # outlive the artifact it names.
    artifact_id: Column[uuid.UUID] = Column(GUID(), nullable=True)
    artifact_version = Column(Integer, nullable=True)
    artifact_hash = Column(String(64), nullable=True)
    request_fingerprint = Column(String(64), nullable=False)
    error_code = Column(String(64), nullable=True)
    started_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    heartbeat_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    terminal_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')",
            name="ck_draft_task_results_state",
        ),
        CheckConstraint(
            "(state = 'completed') = (artifact_id IS NOT NULL"
            " AND artifact_version IS NOT NULL AND artifact_hash IS NOT NULL)",
            name="ck_draft_task_results_artifact",
        ),
        CheckConstraint(
            "artifact_hash IS NULL OR length(artifact_hash) = 64",
            name="ck_draft_task_results_artifact_hash",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_draft_task_results_request_fingerprint",
        ),
        Index("idx_draft_task_results_collection", "collection_id"),
    )
