"""Protocol-bound screening queues and independent observations (GOO-301).

A queue freezes a report corpus at one approved protocol version, criterion
version and stage. Observations are insert-only: a change supersedes the
current row through ``supersedes_observation_id``. AI output lives only in
``screening_suggestions`` and can never become an observation.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

# Mirrors screening_rules.STAGES / DECISIONS; the migration freezes the same lists.
_STAGE_CHECK = "stage IN ('title_abstract','full_text')"
_DECISION_CHECK = "decision IN ('include','exclude','uncertain')"
_ACTIVE_ASSIGNMENT = "revoked_at IS NULL"
_INITIAL_OBSERVATION = "supersedes_observation_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ScreeningQueue(Base):
    """Immutable corpus snapshot bound to a Collection, protocol version and stage."""

    __tablename__ = "screening_queues"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    protocol_version_id = _fk("research_protocol_versions")
    criteria_hash = Column(String(64), nullable=False)
    stage = Column(String(16), nullable=False)
    report_ids = Column(JSONB, nullable=False)
    supersedes_queue_id: Column = _fk("screening_queues", nullable=True)
    created_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(_STAGE_CHECK, name="ck_screening_queue_stage"),
        UniqueConstraint("supersedes_queue_id", name="uq_screening_queue_supersedes"),
        Index("idx_screening_queue_collection", "collection_id"),
    )


class ScreeningAssignment(Base):
    """One revision of a reviewer's assignment; re-assigning inserts a new row."""

    __tablename__ = "screening_assignments"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    queue_id = _fk("screening_queues")
    reviewer_id = _fk("users")
    assigned_by_id = _fk("users")
    created_at = _created_at()
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_id = _fk("users", nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_id IS NULL)",
            name="ck_screening_assignment_revocation",
        ),
        Index(
            "uq_screening_assignment_active",
            "queue_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text(_ACTIVE_ASSIGNMENT),
            sqlite_where=text(_ACTIVE_ASSIGNMENT),
        ),
    )


class ScreeningObservation(Base):
    """Insert-only human screening decision; changes supersede, never overwrite."""

    __tablename__ = "screening_observations"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    queue_id = _fk("screening_queues")
    report_id = _fk("research_reports")
    reviewer_id = _fk("users")
    assignment_id = _fk("screening_assignments")
    decision = Column(String(16), nullable=False)
    exclusion_reason = Column(String(200), nullable=True)
    note = Column(Text, nullable=True)
    supersedes_observation_id: Column = _fk("screening_observations", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(_DECISION_CHECK, name="ck_screening_observation_decision"),
        CheckConstraint(
            "decision = 'exclude' OR exclusion_reason IS NULL",
            name="ck_screening_observation_reason",
        ),
        UniqueConstraint(
            "supersedes_observation_id", name="uq_screening_observation_supersedes"
        ),
        Index(
            "uq_screening_observation_initial",
            "queue_id",
            "report_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text(_INITIAL_OBSERVATION),
            sqlite_where=text(_INITIAL_OBSERVATION),
        ),
        Index("idx_screening_observation_reviewer", "queue_id", "reviewer_id"),
    )


class ScreeningSuggestion(Base):
    """AI screening output attributed to a step, source and model; no user."""

    __tablename__ = "screening_suggestions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    queue_id = _fk("screening_queues")
    report_id = _fk("research_reports")
    source_id = _fk("research_sources")
    step_id = _fk("research_steps")
    model_id = Column(String(100), nullable=True)
    decision = Column(String(16), nullable=False)
    reason = Column(Text, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(
            "decision IN ('include','exclude')",
            name="ck_screening_suggestion_decision",
        ),
        UniqueConstraint(
            "queue_id", "step_id", "source_id", name="uq_screening_suggestion_source"
        ),
    )
