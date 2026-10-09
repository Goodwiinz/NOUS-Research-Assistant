"""Immutable receipts for externally imported search results (GOO-300).

One receipt per imported file, citation chase or scheduled search execution
(GOO-319); one record row per parsed chunk, accepted or rejected, with its
original text. Nothing is ever deleted or
overwritten: changed file contents become a new receipt version in the same
lineage.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base


def _collection_fk() -> Column:
    return Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ResearchImportReceipt(Base):
    """What was imported, as declared by the importer and as observed by us."""

    __tablename__ = "research_import_receipts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    kind = Column(String(16), nullable=False)
    dedup_key = Column(String(160), nullable=False)
    lineage_key = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    previous_receipt_id: Column = Column(
        GUID(),
        ForeignKey("research_import_receipts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    declared = Column(JSONB, nullable=False)
    observed = Column(JSONB, nullable=False)
    parsed_count = Column(Integer, nullable=False)
    accepted_count = Column(Integer, nullable=False)
    rejected_count = Column(Integer, nullable=False)
    actor_user_id: Column = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "collection_id", "dedup_key", name="uq_research_import_receipt_dedup"
        ),
        CheckConstraint(
            # GOO-319 adds scheduled search executions.
            "kind IN ('file_import','citation_chase','scheduled_search')",
            name="ck_research_import_receipt_kind",
        ),
        CheckConstraint("version >= 1", name="ck_research_import_receipt_version"),
        CheckConstraint(
            "accepted_count + rejected_count = parsed_count",
            name="ck_research_import_receipt_counts",
        ),
        Index("idx_research_import_receipt_collection", "collection_id"),
        # Also serves (collection_id, lineage_key) lookups; a second writer
        # that bypassed the Collection lock cannot mint the same version.
        UniqueConstraint(
            "collection_id",
            "lineage_key",
            "version",
            name="uq_research_import_receipt_version",
        ),
    )


class ResearchImportRecord(Base):
    """One parsed record; accepted records carry their GOO-299 report link."""

    __tablename__ = "research_import_records"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _collection_fk()
    receipt_id: Column = Column(
        GUID(),
        ForeignKey("research_import_receipts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    record_index = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    raw = Column(Text, nullable=False)
    parsed = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    rejection_reason = Column(String(64), nullable=True)
    report_id: Column = Column(
        GUID(), ForeignKey("research_reports.id", ondelete="RESTRICT"), nullable=True
    )
    match_method = Column(String(32), nullable=True)
    evidence = Column(JSONB, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "receipt_id", "record_index", name="uq_research_import_record_index"
        ),
        CheckConstraint(
            "status IN ('accepted','rejected')",
            name="ck_research_import_record_status",
        ),
        CheckConstraint(
            "(status = 'rejected') = (rejection_reason IS NOT NULL)",
            name="ck_research_import_record_rejection",
        ),
        CheckConstraint(
            "status = 'accepted' OR report_id IS NULL",
            name="ck_research_import_record_report",
        ),
        Index("idx_research_import_record_collection", "collection_id"),
        Index("idx_research_import_record_report", "report_id"),
    )
