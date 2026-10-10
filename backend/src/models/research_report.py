"""Durable, project-scoped report and study identities (GOO-299).

Identity rows are never deleted: a merged report points at its survivor through
``merged_into_report_id`` and every original ``ResearchSource`` stays untouched,
linked through an append-only observation.
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

# Mirrors report_identity.IDENTITY_KINDS; the migration freezes the same list.
_IDENTITY_KIND_CHECK = (
    "kind IN ('doi','pmid','pmcid','arxiv_base','openalex','semantic_scholar')"
)


def _collection_fk() -> Column:
    return Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ResearchStudy(Base):
    """One underlying study that several reports may describe."""

    __tablename__ = "research_studies"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    label = Column(String(500), nullable=False)
    created_at = _created_at()

    __table_args__ = (Index("idx_research_study_collection", "collection_id"),)


class ResearchReport(Base):
    """Canonical publication identity inside one Collection."""

    __tablename__ = "research_reports"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    title_snapshot = Column(String(500), nullable=False)
    merged_into_report_id: Column = Column(
        GUID(), ForeignKey("research_reports.id", ondelete="RESTRICT"), nullable=True
    )
    study_id: Column = Column(
        GUID(), ForeignKey("research_studies.id", ondelete="RESTRICT"), nullable=True
    )
    study_link_status = Column(String(16), nullable=True)
    study_link_actor_id: Column = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    study_link_rationale = Column(Text, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(
            "study_link_status IN ('proposed','confirmed','disputed')",
            name="ck_research_report_study_link_status",
        ),
        CheckConstraint(
            "(study_id IS NULL) = (study_link_status IS NULL)",
            name="ck_research_report_study_link_pair",
        ),
        Index("idx_research_report_collection", "collection_id"),
    )


class ResearchReportIdentifier(Base):
    """One stable identity per (collection, kind, value)."""

    __tablename__ = "research_report_identifiers"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    report_id: Column = Column(
        GUID(), ForeignKey("research_reports.id", ondelete="RESTRICT"), nullable=False
    )
    kind = Column(String(32), nullable=False)
    value = Column(String(512), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "kind",
            "value",
            name="uq_research_report_identifier_value",
        ),
        CheckConstraint(
            _IDENTITY_KIND_CHECK, name="ck_research_report_identifier_kind"
        ),
        Index("idx_research_report_identifier_collection", "collection_id"),
        Index("idx_research_report_identifier_report", "report_id"),
    )


class ResearchReportObservation(Base):
    """Append-only link from an untouched provider snapshot to its report."""

    __tablename__ = "research_report_observations"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    report_id: Column = Column(
        GUID(), ForeignKey("research_reports.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Column = Column(
        GUID(), ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    match_method = Column(String(32), nullable=False)
    evidence = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("source_id", name="uq_research_report_observation_source"),
        Index("idx_research_report_observation_collection", "collection_id"),
        Index("idx_research_report_observation_report", "report_id"),
    )
