"""Retained append-only decisions for research protocol aggregates."""

import uuid

from sqlalchemy import (
    BigInteger,
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


class ResearchDecisionStream(Base):
    """Sequence allocator for one retained research aggregate."""

    __tablename__ = "research_decision_streams"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    aggregate_type = Column(String(64), nullable=False)
    aggregate_id = Column(GUID(), nullable=False)
    next_seq = Column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            name="uq_research_decision_stream_aggregate",
        ),
        CheckConstraint("next_seq > 0", name="ck_research_decision_stream_next_seq"),
        Index("idx_research_decision_stream_collection", "collection_id"),
    )


class ResearchDecisionEvent(Base):
    """One immutable attributable fact in a research decision stream."""

    __tablename__ = "research_decision_events"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    stream_id = Column(
        GUID(),
        ForeignKey("research_decision_streams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    seq = Column(BigInteger, nullable=False)
    event_type = Column(String(64), nullable=False)
    event_schema_version = Column(Integer, nullable=False)
    actor_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_role = Column(String(64), nullable=False)
    subject_type = Column(String(64), nullable=False)
    subject_id = Column(GUID(), nullable=False)
    subject_version_id = Column(GUID(), nullable=True)
    subject_hash = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))
    idempotency_key = Column(String(255), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    occurred_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "stream_id", "seq", name="uq_research_decision_event_stream_seq"
        ),
        UniqueConstraint(
            "stream_id",
            "idempotency_key",
            name="uq_research_decision_event_stream_idempotency",
        ),
        CheckConstraint("seq > 0", name="ck_research_decision_event_seq"),
        CheckConstraint(
            "event_schema_version > 0",
            name="ck_research_decision_event_schema_version",
        ),
        CheckConstraint(
            "length(subject_hash) = 64",
            name="ck_research_decision_event_subject_hash",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_research_decision_event_request_fingerprint",
        ),
        Index("idx_research_decision_event_collection_seq", "collection_id", "seq"),
        Index("idx_research_decision_event_stream_seq", "stream_id", "seq"),
    )
