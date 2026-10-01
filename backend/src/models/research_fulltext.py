"""Per-report full-text acquisition, kept apart from eligibility (GOO-303).

One request per (Collection, report); its attempts form an insert-only linear
chain (``previous_attempt_id`` UNIQUE plus one head per request). A retrieved
attempt pins the linked Document's content hash. An unavailable attempt is
never an exclusion.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .base import GUID, Base

_FIRST_ATTEMPT = "previous_attempt_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _now() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class ResearchFulltextRequest(Base):
    """A report is sought for full text; the status is derived from attempts."""

    __tablename__ = "research_fulltext_requests"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    report_id = _fk("research_reports")
    requested_by_id = _fk("users")
    requested_at = _now()
    # The governing protocol version at request time.
    protocol_version_id = _fk("research_protocol_versions", nullable=True)

    __table_args__ = (
        # One request per report: "reports sought" cannot inflate.
        UniqueConstraint(
            "collection_id", "report_id", name="uq_research_fulltext_request_report"
        ),
        Index("idx_research_fulltext_request_collection", "collection_id"),
    )


class ResearchFulltextAttempt(Base):
    """Insert-only acquisition attempt; the chain head is the current state."""

    # ponytail: retrieved is terminal; add a "replace document" attempt if a
    # wrong PDF is ever linked.

    __tablename__ = "research_fulltext_attempts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    request_id = _fk("research_fulltext_requests")
    outcome = Column(String(16), nullable=False)
    reason = Column(Text, nullable=True)
    attempted_on = Column(Date, nullable=False)
    actor_id = _fk("users")
    created_at = _now()
    document_id = _fk("documents", nullable=True)
    document_content_hash = Column(String(64), nullable=True)
    # Same-request only: composite FK (request_id, previous_attempt_id).
    previous_attempt_id: Column = Column(GUID(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('requested','retrieved','unavailable')",
            name="ck_research_fulltext_attempt_outcome",
        ),
        CheckConstraint(
            "(outcome = 'retrieved') = (document_id IS NOT NULL)",
            name="ck_research_fulltext_attempt_document",
        ),
        CheckConstraint(
            "(document_id IS NULL) = (document_content_hash IS NULL)",
            name="ck_research_fulltext_attempt_hash",
        ),
        CheckConstraint(
            "outcome <> 'unavailable' OR reason IS NOT NULL",
            name="ck_research_fulltext_attempt_reason",
        ),
        UniqueConstraint(
            "previous_attempt_id", name="uq_research_fulltext_attempt_previous"
        ),
        UniqueConstraint("request_id", "id", name="uq_research_fulltext_attempt_chain"),
        ForeignKeyConstraint(
            ["request_id", "previous_attempt_id"],
            [
                "research_fulltext_attempts.request_id",
                "research_fulltext_attempts.id",
            ],
            ondelete="RESTRICT",
            name="fk_research_fulltext_attempt_previous",
        ),
        Index(
            "uq_research_fulltext_attempt_head",
            "request_id",
            unique=True,
            postgresql_where=text(_FIRST_ATTEMPT),
            sqlite_where=text(_FIRST_ATTEMPT),
        ),
        Index("idx_research_fulltext_attempt_request", "request_id"),
    )
