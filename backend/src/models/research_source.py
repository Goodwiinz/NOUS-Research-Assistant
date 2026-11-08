"""
ResearchSource model for external sources discovered during research.
"""

from sqlalchemy import Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class ResearchSource(BaseModel):
    """An external source (paper, article, etc.) found during a research run."""

    __tablename__ = "research_sources"
    __table_args__ = (
        # Created by n9p2q3r4s5t6_add_missing_performance_indexes.
        Index("idx_research_sources_run", "run_id"),
    )

    run_id = Column(GUID(), ForeignKey("research_runs.id"), nullable=False)
    connector_type = Column(String(50), nullable=False)
    external_id = Column(String(255), nullable=True)
    title = Column(String(500), nullable=False)
    authors = Column(JSONB, nullable=True)
    abstract = Column(Text, nullable=True)
    url = Column(String(2048), nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    content_hash = Column(String(64), nullable=True)

    # Relationships
    run = relationship("ResearchRun", back_populates="sources")
