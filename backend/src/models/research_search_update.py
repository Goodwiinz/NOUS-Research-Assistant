"""Scheduled search updates and classified corpus deltas (GOO-319).

Four insert-only tables (GOO-309's trigger refuses UPDATE and DELETE):

- ``research_search_schedules``: a version chain per schedule. The root
  version's id is the ``schedule_id``; editing cron, timezone, enabled or
  the pinned strategy inserts a successor (``UNIQUE(supersedes...)`` keeps a
  chain linear). Each version pins the exact GOO-298 strategy snapshot and
  the approved protocol version it was built under, and runs as its
  ``owner_id``. Only the root carries the first baseline (a corpus snapshot
  derived from the GOO-300 export, with its sealed digest). No next-run
  column: the next fire is derived (``search_update_rules``).
- ``research_search_executions``: one row per claimed fire. ``UNIQUE
  (schedule_id, scheduled_local)`` is the idempotency guard: two beats, a
  duplicated broker message or a restarted worker claim one row.
- ``research_search_execution_attempts``: every attempt outcome
  (``started``, ``succeeded``, ``failed``, ``skipped``), so failures and
  restarts are retained.
- ``research_search_execution_results``: one row per succeeded execution,
  inserted with its ``succeeded`` attempt: coverage, the corpus snapshot
  after the import, the classified delta and its hash. GOO-320 reads it.

The migration ``c2f4b6d8e0a1`` freezes the same DDL.
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ROOT_BASELINE_CHECK = (
    "(supersedes_schedule_version_id IS NULL) = (baseline_snapshot IS NOT NULL)"
    " AND (baseline_snapshot IS NULL) = (baseline_digest IS NULL)"
)
ROOT_ID_CHECK = "supersedes_schedule_version_id IS NOT NULL OR schedule_id = id"
OUTCOME_CHECK = "outcome IN ('started','succeeded','failed','skipped')"
REASON_CHECK = "outcome IN ('started','succeeded') OR reason IS NOT NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ResearchSearchSchedule(Base):
    __tablename__ = "research_search_schedules"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    schedule_id: Column = Column(GUID(), nullable=False)
    collection_id = _fk("collections")
    owner_id = _fk("users")
    protocol_version_id = _fk("research_protocol_versions")
    source_run_id = _fk("research_runs")
    step_id = Column(String(100), nullable=False)
    strategy_version = Column(String(80), nullable=False)
    strategy = Column(JSONB, nullable=False)
    query = Column(Text, nullable=False)
    cron = Column(String(64), nullable=False)
    timezone = Column(String(64), nullable=False)
    enabled = Column(Boolean, nullable=False)
    baseline_snapshot = Column(JSONB, nullable=True)
    baseline_digest = Column(String(64), nullable=True)
    supersedes_schedule_version_id: Column = _fk(
        "research_search_schedules", nullable=True
    )
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "supersedes_schedule_version_id",
            name="uq_research_search_schedules_supersedes",
        ),
        CheckConstraint(ROOT_BASELINE_CHECK, name="ck_research_search_schedules_root"),
        CheckConstraint(ROOT_ID_CHECK, name="ck_research_search_schedules_id"),
        Index("idx_research_search_schedules_collection", "collection_id"),
        Index("idx_research_search_schedules_schedule", "schedule_id"),
    )


class ResearchSearchExecution(Base):
    __tablename__ = "research_search_executions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    schedule_id: Column = _fk("research_search_schedules")
    schedule_version_id = _fk("research_search_schedules")
    scheduled_local = Column(String(16), nullable=False)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    missed_fires = Column(Integer, nullable=False, default=0)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_local",
            name="uq_research_search_executions_fire",
        ),
        CheckConstraint(
            "missed_fires >= 0", name="ck_research_search_executions_missed"
        ),
        Index("idx_research_search_executions_collection", "collection_id"),
    )


class ResearchSearchExecutionAttempt(Base):
    __tablename__ = "research_search_execution_attempts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    execution_id = _fk("research_search_executions")
    outcome = Column(String(16), nullable=False)
    reason = Column(String(64), nullable=True)
    detail = Column(JSONB, nullable=True)
    worker = Column(String(100), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(OUTCOME_CHECK, name="ck_research_search_attempts_outcome"),
        CheckConstraint(REASON_CHECK, name="ck_research_search_attempts_reason"),
        Index("idx_research_search_attempts_execution", "execution_id"),
    )


class ResearchSearchExecutionResult(Base):
    __tablename__ = "research_search_execution_results"

    execution_id: Column = Column(
        GUID(),
        ForeignKey("research_search_executions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    baseline_execution_id = _fk("research_search_executions", nullable=True)
    import_receipt_id = _fk("research_import_receipts")
    citation_chasing = Column(JSONB, nullable=False)
    coverage = Column(JSONB, nullable=False)
    corpus_snapshot = Column(JSONB, nullable=False)
    delta = Column(JSONB, nullable=False)
    delta_hash = Column(String(64), nullable=False)
    created_at = _created_at()
