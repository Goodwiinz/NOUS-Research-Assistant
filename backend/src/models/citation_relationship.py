"""
CitationRelationship model for citation graph (Research Assistant - User Story 3)
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class CitationRelationship(BaseModel):
    """
    Citation relationship model - represents directed edges in the citation graph.

    This model stores relationships between citations, such as:
    - Paper A cites Paper B
    - Paper A is cited by Paper B
    - Paper A is related to Paper B

    Used for citation network visualization and analysis (User Story 3).
    """

    __tablename__ = "citation_relationships"

    # Source and target citations (directed edge)
    source_citation_id = Column(
        GUID(),
        ForeignKey("citations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_citation_id = Column(
        GUID(),
        ForeignKey("citations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationship metadata
    relationship_type = Column(
        String(50), nullable=False, default="cites", server_default="cites"
    )  # cites, cited_by, related_to

    citation_context = Column(Text, nullable=True)  # Where the citation appears in text
    confidence = Column(Float, nullable=True)  # Extraction confidence score (0.0 - 1.0)

    # Relationships
    source_citation = relationship(
        "Citation", foreign_keys=[source_citation_id], backref="outgoing_relationships"
    )
    target_citation = relationship(
        "Citation", foreign_keys=[target_citation_id], backref="incoming_relationships"
    )

    # Constraints
    __table_args__ = (
        UniqueConstraint(
            "source_citation_id", "target_citation_id", name="uq_citation_relationship"
        ),
        CheckConstraint(
            "source_citation_id != target_citation_id",
            name="ck_citation_no_self_reference",
        ),
        # Created by e0g4h5i6j7k8_create_citation_relationships.
        Index("ix_citation_relationships_source", "source_citation_id"),
        Index("ix_citation_relationships_target", "target_citation_id"),
    )

    def __repr__(self):
        return f"<CitationRelationship(source={self.source_citation_id}, target={self.target_citation_id}, type={self.relationship_type})>"

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["source_citation_id"] = str(self.source_citation_id)
        data["target_citation_id"] = str(self.target_citation_id)
        data["relationship_type"] = self.relationship_type
        data["citation_context"] = self.citation_context
        data["confidence"] = self.confidence
        return data

    def to_graph_edge(self) -> dict:
        """
        Convert to format suitable for graph visualization (Cytoscape.js).

        Returns edge data in Cytoscape.js format:
        {
            data: {
                id: "edge_id",
                source: "source_node_id",
                target: "target_node_id",
                type: "cites",
                context: "...",
                confidence: 0.95
            }
        }
        """
        return {
            "data": {
                "id": str(self.id),
                "source": str(self.source_citation_id),
                "target": str(self.target_citation_id),
                "type": self.relationship_type,
                "context": self.citation_context,
                "confidence": self.confidence,
                "created_at": self.created_at.isoformat() if self.created_at else None,
            }
        }
