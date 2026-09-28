"""Durable citation review for a generated draft candidate."""

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, BaseModel


class DraftReview(BaseModel):
    __tablename__ = "draft_reviews"

    project_id = Column(
        GUID(), ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    base_draft_id = Column(
        GUID(), ForeignKey("generated_drafts.id", ondelete="SET NULL"), nullable=True
    )
    candidate_content_hash = Column(String(64), nullable=False)
    candidate_content = Column(Text, nullable=False)
    source_document_ids = Column(JSONB, nullable=False, default=list)
    review = Column(JSONB, nullable=False)
    outcome = Column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('passed', 'blocked')", name="ck_draft_reviews_outcome"
        ),
        Index("idx_draft_reviews_project_created", "project_id", "created_at"),
    )

    def to_frontend_format(self) -> dict:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "base_draft_id": str(self.base_draft_id) if self.base_draft_id else None,
            "candidate_content_hash": self.candidate_content_hash,
            "candidate_content": self.candidate_content,
            "source_document_ids": self.source_document_ids,
            "review": self.review,
            "outcome": self.outcome,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
