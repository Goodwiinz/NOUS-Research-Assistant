"""Append-only human review ledger for persisted research stages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import GUID, Base

if TYPE_CHECKING:
    from .research_run import ResearchRun


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchStageReview(Base):
    """Immutable audit row recording one decision for one stage output."""

    __tablename__ = "research_stage_reviews"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("research_runs.id"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_type: Mapped[str] = mapped_column(String(50), nullable=False)
    review_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )

    run: Mapped[ResearchRun] = relationship("ResearchRun", back_populates="reviews")

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "step_index",
            "output_hash",
            "review_kind",
            name="uq_research_stage_reviews_gate",
        ),
        Index("ix_research_stage_reviews_run_step", "run_id", "step_index"),
        Index("ix_research_stage_reviews_owner_id", "owner_id"),
        Index("ix_research_stage_reviews_reviewer_id", "reviewer_id"),
        Index("ix_research_stage_reviews_organization_id", "organization_id"),
    )
