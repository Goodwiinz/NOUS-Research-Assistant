"""
Collection model for Terminal Observatory document organization
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class Collection(BaseModel):
    """
    Collection model - groups of documents within a workspace.

    Collections allow users to organize documents by topic, project,
    or any other logical grouping within a workspace.

    In the Research Assistant feature (User Story 4), collections can
    function as research projects with additional metadata like project type,
    research status, goals, deadlines, and tags.
    """

    __tablename__ = "collections"
    __table_args__ = (
        # Created by n9p2q3r4s5t6_add_missing_performance_indexes.
        Index("idx_collections_workspace_created", "workspace_id", "created_at"),
        Index("idx_collections_workspace_status", "workspace_id", "research_status"),
    )

    # Parent relationship
    workspace_id = Column(
        GUID(),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Basic information
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)

    # Settings
    color = Column(String(7), nullable=True)  # Hex color for UI
    icon = Column(String(50), nullable=True)  # Icon name for UI

    # Research project fields (for Research Assistant feature - User Story 4)
    project_type = Column(
        String(50), nullable=False, default="research", server_default="research"
    )  # research, literature_review, thesis, paper
    research_status = Column(
        String(50),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
    )  # active, paused, completed, archived
    research_goals = Column(Text, nullable=True)  # Project objectives and goals
    deadline = Column(DateTime(timezone=True), nullable=True)  # Project deadline
    tags = Column(
        JSONB, nullable=False, default=list, server_default="[]"
    )  # Project categorization tags
    is_private = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )  # Privacy setting (always TRUE for Phase 3)

    # Relationships
    workspace = relationship("Workspace", back_populates="collections")
    documents = relationship(
        "CollectionDocument", back_populates="collection", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Collection(name={self.name}, workspace_id={self.workspace_id})>"

    @property
    def document_count(self) -> int:
        """Get number of documents in collection"""
        return len(self.documents) if self.documents else 0

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["document_count"] = self.document_count
        # Add research project fields
        data["project_type"] = self.project_type
        data["research_status"] = self.research_status
        data["research_goals"] = self.research_goals
        data["deadline"] = self.deadline.isoformat() if self.deadline else None
        data["tags"] = self.tags
        data["is_private"] = self.is_private
        return data


class CollectionDocument(BaseModel):
    """
    Association table for Collection-Document many-to-many relationship.

    A document can belong to multiple collections, and each collection
    can contain multiple documents.
    """

    __tablename__ = "collection_documents"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "document_id",
            name="uq_collection_documents",
        ),
    )

    collection_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        GUID(),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional ordering within collection
    sort_order = Column(Integer, default=0, nullable=False)

    # Relationships
    collection = relationship("Collection", back_populates="documents")
    document = relationship("Document")

    def __repr__(self):
        return f"<CollectionDocument(collection_id={self.collection_id}, document_id={self.document_id})>"
