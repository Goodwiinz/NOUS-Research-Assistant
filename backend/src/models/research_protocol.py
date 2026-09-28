"""Versioned research questions, protocols, deviations, and registrations."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base, BaseModel


class ResearchQuestion(BaseModel):
    __tablename__ = "research_questions"

    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    current_version_id = Column(
        GUID(),
        ForeignKey(
            "research_question_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_research_question_current_version",
        ),
        nullable=True,
    )


class ResearchQuestionVersion(Base):
    __tablename__ = "research_question_versions"
    __table_args__ = (
        UniqueConstraint("question_id", "version", name="uq_research_question_version"),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    question_id = Column(
        GUID(), ForeignKey("research_questions.id", ondelete="RESTRICT"), nullable=False
    )
    version = Column(Integer, nullable=False)
    parent_version_id = Column(
        GUID(), ForeignKey("research_question_versions.id", ondelete="RESTRICT")
    )
    question = Column(Text, nullable=False)
    hypothesis = Column(Text)
    scope = Column(Text)
    framework = Column(JSONB, nullable=False, default=dict)
    content_hash = Column(String(64), nullable=False)
    author_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class ResearchProtocol(BaseModel):
    __tablename__ = "research_protocols"
    __table_args__ = (
        UniqueConstraint("collection_id", "name", name="uq_research_protocol_name"),
    )

    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    name = Column(String(255), nullable=False)
    current_draft_version_id = Column(
        GUID(),
        ForeignKey(
            "research_protocol_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_research_protocol_current_draft",
        ),
        nullable=True,
    )
    current_approved_version_id = Column(
        GUID(),
        ForeignKey(
            "research_protocol_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_research_protocol_current_approved",
        ),
        nullable=True,
    )


class ResearchProtocolVersion(Base):
    __tablename__ = "research_protocol_versions"
    __table_args__ = (
        UniqueConstraint("protocol_id", "version", name="uq_research_protocol_version"),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    protocol_id = Column(
        GUID(), ForeignKey("research_protocols.id", ondelete="RESTRICT"), nullable=False
    )
    version = Column(Integer, nullable=False)
    parent_version_id = Column(
        GUID(), ForeignKey("research_protocol_versions.id", ondelete="RESTRICT")
    )
    question_version_id = Column(
        GUID(),
        ForeignKey("research_question_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    blueprint_id = Column(
        GUID(),
        ForeignKey("research_blueprints.id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_plan = Column(JSONB, nullable=False)
    snapshot = Column(JSONB, nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="draft")
    change_kind = Column(String(32), nullable=False, default="initial")
    amendment_reason = Column(Text)
    author_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id = Column(GUID(), ForeignKey("users.id", ondelete="RESTRICT"))
    approved_at = Column(DateTime(timezone=True))
    superseded_at = Column(DateTime(timezone=True))
    created_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class ProtocolDeviation(Base):
    __tablename__ = "protocol_deviations"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    protocol_version_id = Column(
        GUID(),
        ForeignKey("research_protocol_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id = Column(GUID(), ForeignKey("research_runs.id", ondelete="RESTRICT"))
    output_reference = Column(
        GUID(), ForeignKey("research_steps.id", ondelete="RESTRICT")
    )
    observed_difference = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    disposition = Column(String(32), nullable=False)
    actor_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class ProtocolRegistrationOperation(Base):
    __tablename__ = "protocol_registration_operations"
    __table_args__ = (
        UniqueConstraint(
            "protocol_version_id",
            "idempotency_key",
            name="uq_protocol_registration_idempotency",
        ),
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="RESTRICT"), nullable=False
    )
    protocol_version_id = Column(
        GUID(),
        ForeignKey("research_protocol_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider = Column(String(100), nullable=False)
    status = Column(String(32), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    external_identifier = Column(String(255))
    url = Column(String(2048))
    receipt = Column(JSONB)
    protocol_version_hash = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    failure_reason = Column(Text)
    recorded_by_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
