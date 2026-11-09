"""
GeneratedDraft model for AI-generated literature review drafts (Research Assistant - User Story 5)
"""

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class GeneratedDraft(BaseModel):
    """
    Generated draft model - AI-generated literature review drafts with versioning.

    Multi-agent systems generate literature review drafts from research projects.
    Each project can have multiple draft versions (max 10), with automatic
    version retention and version management.

    Used for Research Assistant feature (User Story 5).
    """

    __tablename__ = "generated_drafts"

    # Parent relationship
    project_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Version information
    version = Column(Integer, nullable=False)  # Version number (1, 2, 3, ...)

    # Draft content
    title = Column(String(255), nullable=False)
    content = Column(
        Text, nullable=False
    )  # Full markdown content of the literature review

    # Metadata
    themes = Column(
        JSONB, nullable=False, default=list, server_default="[]"
    )  # Identified themes/topics
    word_count = Column(Integer, nullable=True)  # Total word count
    citation_count = Column(Integer, nullable=True)  # Number of citations included

    # Generation metadata
    generation_params = Column(
        JSONB, nullable=True
    )  # Parameters used for generation (model, temperature, etc.)
    generation_time_ms = Column(
        Integer, nullable=True
    )  # Time taken to generate (in milliseconds)

    # Status
    is_current = Column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )

    # Relationships
    project = relationship("Collection", backref="drafts")
    citations = relationship(
        "DraftCitation", back_populates="draft", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # r6_run_integrity_uniques: one row per (project, version) and at most
        # one current draft per project.
        Index(
            "uq_generated_drafts_project_version", "project_id", "version", unique=True
        ),
        Index(
            "uq_generated_drafts_current",
            "project_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
        # n9p2q3r4s5t6_add_missing_performance_indexes:
        Index("idx_generated_drafts_project_version", "project_id", "version"),
        Index("idx_generated_drafts_project_current", "project_id", "is_current"),
        {"schema": None},  # Use default schema
    )

    def __repr__(self):
        return f"<GeneratedDraft(project_id={self.project_id}, version={self.version}, title={self.title})>"

    @property
    def content_preview(self) -> str:
        """Get a preview of the draft content"""
        if not self.content:
            return ""
        if len(self.content) <= 500:
            return self.content
        return self.content[:500] + "..."

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["project_id"] = str(self.project_id)
        data["version"] = self.version
        data["title"] = self.title
        data["content"] = self.content
        data["content_preview"] = self.content_preview
        data["themes"] = self.themes
        data["word_count"] = self.word_count
        data["citation_count"] = self.citation_count
        data["generation_params"] = self.generation_params
        data["generation_time_ms"] = self.generation_time_ms
        data["is_current"] = self.is_current
        return data

    def to_frontend_format(self) -> dict:
        """Convert to format suitable for frontend display"""
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "version": self.version,
            "title": self.title,
            "content": self.content,
            "content_preview": self.content_preview,
            "themes": self.themes or [],
            "word_count": self.word_count,
            "citation_count": self.citation_count,
            "generation_params": self.generation_params,
            "generation_time_ms": self.generation_time_ms,
            "is_current": self.is_current,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def mark_as_current(self, session):
        """
        Mark this draft as the current version and unmark all others.

        Args:
            session: SQLAlchemy session
        """
        # Unmark all other drafts in the same project as current
        session.query(GeneratedDraft).filter(
            GeneratedDraft.project_id == self.project_id, GeneratedDraft.id != self.id
        ).update({"is_current": False})

        # Mark this draft as current
        self.is_current = True
        session.commit()
