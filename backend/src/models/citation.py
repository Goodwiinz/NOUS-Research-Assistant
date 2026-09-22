"""
Citation model for Terminal Observatory RAG references
"""

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class Citation(BaseModel):
    """
    Citation model - links document chunks to generated messages.

    When the RAG system generates a response using retrieved context,
    each source chunk is recorded as a citation for transparency and
    verification.

    Citations may reference either:
    - A document in the database (document_id)
    - An external reference like an arXiv paper (external_reference_id)
    """

    __tablename__ = "citations"

    # Parent relationships
    # message_id is nullable to support standalone citations (e.g., bibliography entries)
    message_id = Column(
        GUID(),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    document_id = Column(
        GUID(),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # External reference (for sources not in database, e.g., arXiv papers)
    external_reference_id = Column(String(255), nullable=True, index=True)
    document_title = Column(String(500), nullable=True)  # Store title for external refs
    document_type = Column(String(100), nullable=True)  # Store type for external refs

    # Scholarly metadata (for Research Assistant feature - User Story 2)
    authors = Column(JSONB, nullable=True)  # List of author names/objects
    year = Column(Integer, nullable=True)  # Publication year
    venue = Column(String(500), nullable=True)  # Journal/conference name
    # Non-unique on purpose: DOI/arXiv ids identify public papers, so global
    # uniqueness made tenant B's insert of a paper A cited first a 500 and
    # leaked citation activity across tenants (R6-M8). o2r3s4t5u6v7 dropped
    # the DB-level unique indexes; the model must not resurrect them via
    # create_all. Per-org dedup is application logic.
    doi = Column(String(255), nullable=True)  # Digital Object Identifier
    arxiv_id = Column(String(100), nullable=True)  # arXiv identifier
    abstract = Column(Text, nullable=True)  # Paper abstract
    metadata_source = Column(
        String(100), nullable=True
    )  # Source of metadata (e.g., 'arxiv', 'semantic_scholar', 'crossref')
    needs_review = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # Flag for incomplete metadata

    # Chunk information
    source_position = Column(Integer, nullable=True)
    chunk_index = Column(Integer, nullable=True)  # Index of the vector chunk
    chunk_id = Column(String(255), nullable=True)  # Qdrant chunk ID

    # Content
    snippet = Column(Text, nullable=True)  # The text snippet used
    page_number = Column(Integer, nullable=True)  # Page number if applicable

    # Relevance scores
    score = Column(Float, nullable=True)  # Retrieval score (cosine similarity or RRF)
    rerank_score = Column(Float, nullable=True)  # Reranking score (e.g., Cohere)

    # Highlight positions (for UI highlighting)
    start_char = Column(Integer, nullable=True)
    end_char = Column(Integer, nullable=True)

    # Relationships
    message = relationship("ChatMessage", back_populates="citations")
    document = relationship("Document")

    def __repr__(self):
        ref = self.document_id or self.external_reference_id or "unknown"
        return (
            f"<Citation(message_id={self.message_id}, ref={ref}, score={self.score})>"
        )

    @property
    def snippet_preview(self) -> str:
        """Get a preview of the snippet"""
        if not self.snippet:
            return ""
        if len(self.snippet) <= 100:
            return self.snippet
        return self.snippet[:100] + "..."

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()
        data["snippet_preview"] = self.snippet_preview
        data["external_reference_id"] = self.external_reference_id
        data["document_title"] = self.document_title
        data["document_type"] = self.document_type
        # Add scholarly metadata
        data["authors"] = self.authors
        data["year"] = self.year
        data["venue"] = self.venue
        data["doi"] = self.doi
        data["arxiv_id"] = self.arxiv_id
        data["abstract"] = self.abstract
        data["metadata_source"] = self.metadata_source
        data["needs_review"] = self.needs_review
        return data

    def to_frontend_format(self) -> dict:
        """Convert to format suitable for frontend display"""
        return {
            "id": str(self.id),
            "document_id": str(self.document_id) if self.document_id else None,
            "external_reference_id": self.external_reference_id,
            "document_title": self.document_title,
            "snippet": self.snippet,
            "snippet_preview": self.snippet_preview,
            "page_number": self.page_number,
            "source_position": self.source_position,
            "chunk_id": self.chunk_id,
            "score": self.score,
            "chunk_index": self.chunk_index,
            # Scholarly metadata
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "abstract": self.abstract,
            "metadata_source": self.metadata_source,
            "needs_review": self.needs_review,
        }
