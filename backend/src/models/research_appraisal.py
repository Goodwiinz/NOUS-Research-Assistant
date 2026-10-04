"""Insert-only study-design appraisal assessments (GOO-309).

One row is one person's RoB 2 answers for one result: a unit (``study:<id>``
or ``report:<id>``), an outcome and a timepoint, pinned to the instrument
version and structure hash and to the approved protocol version. Edits and
adjudications are new rows (``supersedes_assessment_id``); status and
staleness are derived on read. The trigger in migration ``e2a4c6b8d0f1``
refuses UPDATE and DELETE; that migration freezes the same DDL.
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

INITIAL_INDEPENDENT = "supersedes_assessment_id IS NULL AND kind = 'independent'"
INITIAL_ADJUDICATED = "supersedes_assessment_id IS NULL AND kind = 'adjudicated'"
TARGET_KEY_CHECK = (
    "target_key = COALESCE('study:' || study_id::text, 'report:' || report_id::text)"
)


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


class AppraisalAssessment(Base):
    __tablename__ = "appraisal_assessments"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    protocol_version_id = _fk("research_protocol_versions")
    instrument_key = Column(String(32), nullable=False)
    instrument_version = Column(String(32), nullable=False)
    instrument_spec_hash = Column(String(64), nullable=False)
    study_id = _fk("research_studies", nullable=True)
    report_id = _fk("research_reports", nullable=True)
    target_key = Column(String(80), nullable=False)
    outcome_key = Column(String(100), nullable=False)
    timepoint = Column(String(100), nullable=False)
    study_design = Column(String(40), nullable=False)
    applicability = Column(String(16), nullable=False)
    domains = Column(JSONB, nullable=False)
    overall = Column(String(16), nullable=True)
    kind = Column(String(16), nullable=False)
    actor_role = Column(String(16), nullable=False)
    assessor_id = _fk("users")
    resolves_assessment_ids = Column(JSONB(none_as_null=True), nullable=True)
    rationale = Column(Text, nullable=True)
    input_hash = Column(String(64), nullable=False)
    supersedes_assessment_id: Column = _fk("appraisal_assessments", nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "(study_id IS NULL) <> (report_id IS NULL)", name="ck_appraisal_target"
        ),
        CheckConstraint(TARGET_KEY_CHECK, name="ck_appraisal_target_key").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "applicability IN ('applicable','not_applicable')",
            name="ck_appraisal_applicability",
        ),
        CheckConstraint(
            "overall IS NULL OR overall IN ('low','some_concerns','high')",
            name="ck_appraisal_overall",
        ),
        CheckConstraint(
            "applicability = 'applicable' OR overall IS NULL",
            name="ck_appraisal_not_applicable_overall",
        ),
        CheckConstraint(
            "kind IN ('independent','adjudicated')", name="ck_appraisal_kind"
        ),
        CheckConstraint(
            "actor_role IN ('reviewer','adjudicator')", name="ck_appraisal_actor_role"
        ),
        CheckConstraint(
            "(kind = 'adjudicated') = (actor_role = 'adjudicator')",
            name="ck_appraisal_kind_role",
        ),
        CheckConstraint(
            "(kind = 'adjudicated') = (resolves_assessment_ids IS NOT NULL)",
            name="ck_appraisal_resolves",
        ),
        CheckConstraint(
            "kind <> 'adjudicated' OR rationale IS NOT NULL",
            name="ck_appraisal_adjudication_rationale",
        ),
        UniqueConstraint("supersedes_assessment_id", name="uq_appraisal_supersedes"),
        Index(
            "uq_appraisal_initial_independent",
            "collection_id",
            "assessor_id",
            "target_key",
            "outcome_key",
            "timepoint",
            "instrument_key",
            "instrument_version",
            unique=True,
            postgresql_where=sql_text(INITIAL_INDEPENDENT),
            sqlite_where=sql_text(INITIAL_INDEPENDENT),
        ),
        Index(
            "uq_appraisal_initial_adjudicated",
            "collection_id",
            "target_key",
            "outcome_key",
            "timepoint",
            "instrument_key",
            "instrument_version",
            unique=True,
            postgresql_where=sql_text(INITIAL_ADJUDICATED),
            sqlite_where=sql_text(INITIAL_ADJUDICATED),
        ),
        Index(
            "idx_appraisal_collection_key",
            "collection_id",
            "target_key",
            "outcome_key",
            "timepoint",
        ),
    )
