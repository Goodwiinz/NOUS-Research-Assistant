"""Insert-only evidence tables, contradiction chains and certainty (GOO-310).

``EvidenceTableVersion`` freezes one row per analysis unit for a declared
outcome and timepoint; its ``rows`` JSONB is read only as a whole.
``EvidenceContradiction`` is one chain per group (``contradiction_id`` is the
opened row's id), ``OutcomeCertaintyAssessment`` one chain per outcome and
timepoint. Status and staleness are derived on read; the triggers in
migration ``f4b6d8a0c2e3`` refuse UPDATE and DELETE, and that migration
freezes the same DDL.
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
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

INITIAL_TABLE = "supersedes_table_id IS NULL"
INITIAL_CERTAINTY = "supersedes_certainty_id IS NULL"
LEVEL_CHECK = "level IS NULL OR level IN ('very_low','low','moderate','high')"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class EvidenceTableVersion(Base):
    __tablename__ = "evidence_table_versions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    protocol_version_id = _fk("research_protocol_versions")
    outcome_key = Column(String(100), nullable=False)
    timepoint = Column(String(100), nullable=False)
    matrix_id = _fk("extraction_matrices")
    form_version_id = _fk("extraction_form_versions")
    field_ids = Column(JSONB, nullable=False)
    rows = Column(JSONB, nullable=False)
    excluded = Column(JSONB, nullable=False)
    content_hash = Column(String(64), nullable=False)
    created_by_id = _fk("users")
    supersedes_table_id: Column = _fk("evidence_table_versions", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("supersedes_table_id", name="uq_evidence_table_supersedes"),
        # PostgreSQL-only: SQLite (OpenAPI generation, unit tests) lacks jsonb_*.
        CheckConstraint(
            "jsonb_typeof(field_ids) = 'array'"
            " AND jsonb_array_length(field_ids) BETWEEN 1 AND 50",
            name="ck_evidence_table_field_ids",
        ).ddl_if(dialect="postgresql"),
        Index(
            "uq_evidence_table_initial",
            "collection_id",
            "outcome_key",
            "timepoint",
            unique=True,
            postgresql_where=sql_text(INITIAL_TABLE),
            sqlite_where=sql_text(INITIAL_TABLE),
        ),
    )


class EvidenceContradiction(Base):
    __tablename__ = "evidence_contradictions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    contradiction_id: Column = Column(GUID(), nullable=False)
    table_version_id = _fk("evidence_table_versions")
    field_id: Column = Column(GUID(), nullable=False)
    accepted_value_ids = Column(JSONB(none_as_null=True), nullable=True)
    kind = Column(String(16), nullable=False)
    explanation = Column(Text, nullable=False)
    actor_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    # An attributed snapshot of model stance rows; never a decision.
    suggestion = Column(JSONB(none_as_null=True), nullable=True)
    previous_id: Column = _fk("evidence_contradictions", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('opened','resolved','acknowledged','dissent')",
            name="ck_evidence_contradiction_kind",
        ),
        CheckConstraint(
            "actor_role IN ('reviewer','adjudicator')",
            name="ck_evidence_contradiction_actor_role",
        ),
        CheckConstraint(
            "(kind = 'opened') = (previous_id IS NULL)",
            name="ck_evidence_contradiction_chain",
        ),
        CheckConstraint(
            "kind <> 'opened' OR contradiction_id = id",
            name="ck_evidence_contradiction_group",
        ),
        CheckConstraint(
            "(kind = 'opened') = (accepted_value_ids IS NOT NULL)",
            name="ck_evidence_contradiction_members",
        ),
        CheckConstraint(
            "suggestion IS NULL OR kind = 'opened'",
            name="ck_evidence_contradiction_suggestion",
        ),
        CheckConstraint(
            "kind NOT IN ('resolved','acknowledged') OR actor_role = 'adjudicator'",
            name="ck_evidence_contradiction_decider",
        ),
        UniqueConstraint("previous_id", name="uq_evidence_contradiction_previous"),
        Index("idx_evidence_contradiction_table", "collection_id", "table_version_id"),
    )


class OutcomeCertaintyAssessment(Base):
    __tablename__ = "outcome_certainty_assessments"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    table_version_id = _fk("evidence_table_versions")
    outcome_key = Column(String(100), nullable=False)
    timepoint = Column(String(100), nullable=False)
    method_key = Column(String(16), nullable=False)
    method_version = Column(String(32), nullable=False)
    starting_level = Column(String(8), nullable=False)
    domains = Column(JSONB, nullable=False)
    level = Column(String(16), nullable=True)
    appraisal_assessment_ids = Column(JSONB, nullable=False)
    contradiction_ids = Column(JSONB, nullable=False)
    rationale = Column(Text, nullable=False)
    assessed_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    input_hash = Column(String(64), nullable=False)
    supersedes_certainty_id: Column = _fk(
        "outcome_certainty_assessments", nullable=True
    )
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(
            "starting_level IN ('high','low')", name="ck_certainty_starting_level"
        ),
        CheckConstraint(LEVEL_CHECK, name="ck_certainty_level"),
        CheckConstraint("actor_role = 'reviewer'", name="ck_certainty_actor_role"),
        UniqueConstraint("supersedes_certainty_id", name="uq_certainty_supersedes"),
        Index(
            "uq_certainty_initial",
            "collection_id",
            "outcome_key",
            "timepoint",
            unique=True,
            postgresql_where=sql_text(INITIAL_CERTAINTY),
            sqlite_where=sql_text(INITIAL_CERTAINTY),
        ),
    )
