"""
Search query and result models for RAG system
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel
from .utils import StringArray


class SearchType(PyEnum):
    """Search types for different query approaches"""

    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    GRAPH = "graph"
    MULTIMODAL = "multimodal"


class SearchQuery(BaseModel):
    """Search query model for tracking user queries"""

    __tablename__ = "search_queries"
    __table_args__ = (
        # Created by p3q4r5s6t7u8_add_dashboard_performance_indexes.
        Index("idx_search_queries_org_created", "organization_id", "created_at"),
        Index(
            "idx_search_queries_org_created_duration",
            "organization_id",
            "created_at",
            "search_duration_ms",
        ),
        Index(
            "idx_search_queries_created_duration", "created_at", "search_duration_ms"
        ),
    )

    # Query information
    query_text = Column(Text, nullable=False)
    search_type = Column(Enum(SearchType), nullable=False, index=True)
    filters = Column(JSON, nullable=True)  # Search filters
    query_parameters = Column(JSON, nullable=True)  # Additional parameters

    # Performance metrics
    total_results = Column(Integer, default=0, nullable=False)
    search_duration_ms = Column(Integer, nullable=True)
    vector_search_duration_ms = Column(Integer, nullable=True)
    graph_search_duration_ms = Column(Integer, nullable=True)
    reranking_duration_ms = Column(Integer, nullable=True)

    # User information
    user_id = Column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    # Session tracking
    session_id = Column(String(255), nullable=True, index=True)
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)

    # Feedback
    user_satisfaction = Column(Integer, nullable=True)  # 1-5 rating
    feedback_text = Column(Text, nullable=True)
    clicked_results = Column(Integer, default=0, nullable=False)

    # Relationships
    user = relationship("User", back_populates="search_queries")
    organization = relationship("Organization")
    results = relationship(
        "SearchResult", back_populates="search_query", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<SearchQuery(query={self.query_text[:50]}..., type={self.search_type.value}, results={self.total_results})>"

    @property
    def average_result_score(self) -> float:
        """Get average relevance score of results"""
        if not self.results:
            return 0.0
        scores = [
            result.relevance_score for result in self.results if result.relevance_score
        ]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def search_duration_seconds(self) -> float:
        """Get search duration in seconds"""
        if self.search_duration_ms:
            return self.search_duration_ms / 1000.0
        return 0.0

    @property
    def has_feedback(self) -> bool:
        """Check if query has user feedback"""
        return self.user_satisfaction is not None or bool(self.feedback_text)

    def add_filter(self, key: str, value):
        """Add search filter"""
        if not self.filters:
            self.filters = {}
        self.filters[key] = value

    def remove_filter(self, key: str):
        """Remove search filter"""
        if self.filters and key in self.filters:
            del self.filters[key]

    def add_parameter(self, key: str, value):
        """Add query parameter"""
        if not self.query_parameters:
            self.query_parameters = {}
        self.query_parameters[key] = value

    def update_performance_metrics(
        self,
        total_duration_ms: int = None,
        vector_duration_ms: int = None,
        graph_duration_ms: int = None,
        reranking_duration_ms: int = None,
    ):
        """Update performance metrics"""
        if total_duration_ms is not None:
            self.search_duration_ms = total_duration_ms
        if vector_duration_ms is not None:
            self.vector_search_duration_ms = vector_duration_ms
        if graph_duration_ms is not None:
            self.graph_search_duration_ms = graph_duration_ms
        if reranking_duration_ms is not None:
            self.reranking_duration_ms = reranking_duration_ms

    def record_result_click(self):
        """Record that user clicked on a result"""
        self.clicked_results += 1

    def add_feedback(self, satisfaction: int, feedback_text: str = None):
        """Add user feedback"""
        if 1 <= satisfaction <= 5:
            self.user_satisfaction = satisfaction
        if feedback_text:
            self.feedback_text = feedback_text

    def to_dict(self, include_results: bool = False) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data["search_type"] = self.search_type.value if self.search_type else None

        # Add computed fields
        data["average_result_score"] = self.average_result_score
        data["search_duration_seconds"] = self.search_duration_seconds
        data["has_feedback"] = self.has_feedback

        # Include results if requested
        if include_results:
            data["results"] = [result.to_dict() for result in self.results]

        # Remove sensitive fields
        data.pop("ip_address", None)
        data.pop("user_agent", None)

        return data

    @classmethod
    def get_popular_queries(
        cls, organization_id: Optional[uuid.UUID] = None, limit: int = 10
    ) -> list:
        """Get most frequent search queries"""
        from sqlalchemy import func

        query = (
            cls.session.query(cls.query_text, func.count(cls.id).label("search_count"))
            .filter(cls.is_deleted == False)
            .group_by(cls.query_text)
            .order_by(func.count(cls.id).desc())
            .limit(limit)
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        return query.all()

    @classmethod
    def get_queries_with_low_satisfaction(
        cls, organization_id: Optional[uuid.UUID] = None, limit: int = 10
    ) -> list:
        """Get queries with low user satisfaction"""
        query = (
            cls.query.filter(cls.user_satisfaction <= 2, cls.is_deleted == False)
            .order_by(cls.user_satisfaction.asc())
            .limit(limit)
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        return query.all()


class SearchResult(BaseModel):
    """Search result model for individual document matches"""

    __tablename__ = "search_results"

    # Result information
    document_id = Column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    search_query_id = Column(
        GUID(), ForeignKey("search_queries.id", ondelete="CASCADE"), nullable=False
    )
    rank_position = Column(Integer, nullable=False)
    relevance_score = Column(Float, nullable=False, index=True)
    confidence = Column(Float, default=1.0, nullable=False)

    # Matching information
    matching_text = Column(Text, nullable=True)  # Text that matched the query
    match_type = Column(String(50), nullable=True)  # semantic, keyword, etc.
    highlight_spans = Column(JSON, nullable=True)  # Highlight positions in text

    # Context information
    context_before = Column(Text, nullable=True)
    context_after = Column(Text, nullable=True)
    context_window_size = Column(Integer, default=200, nullable=False)

    # Multimodal information
    matched_modalities = Column(StringArray, nullable=True)  # text, image, audio, etc.
    modality_scores = Column(JSON, nullable=True)  # Scores per modality

    # User interaction
    was_clicked = Column(Boolean, default=False, nullable=False)
    clicked_at = Column(DateTime(timezone=True), nullable=True)
    dwell_time_ms = Column(Integer, nullable=True)  # Time spent on result

    # Feedback
    user_rating = Column(Integer, nullable=True)  # 1-5 rating
    feedback_text = Column(Text, nullable=True)

    # Relationships
    document = relationship("Document", back_populates="search_results")
    search_query = relationship("SearchQuery", back_populates="results")

    def __repr__(self):
        return f"<SearchResult(rank={self.rank_position}, score={self.relevance_score}, clicked={self.was_clicked})>"

    @property
    def click_through_rate(self) -> float:
        """Get click-through rate (placeholder for aggregate calculations)"""
        return 1.0 if self.was_clicked else 0.0

    @property
    def dwell_time_seconds(self) -> float:
        """Get dwell time in seconds"""
        if self.dwell_time_ms:
            return self.dwell_time_ms / 1000.0
        return 0.0

    def record_click(self):
        """Record that user clicked on this result"""
        self.was_clicked = True
        self.clicked_at = datetime.utcnow()
        # Also update parent query
        if self.search_query:
            self.search_query.record_result_click()

    def record_dwell_time(self, dwell_time_ms: int):
        """Record dwell time on this result"""
        self.dwell_time_ms = dwell_time_ms

    def add_feedback(self, rating: int, feedback_text: str = None):
        """Add user feedback for this result"""
        if 1 <= rating <= 5:
            self.user_rating = rating
        if feedback_text:
            self.feedback_text = feedback_text

    def add_highlight_span(self, start: int, end: int, text: str = None):
        """Add a highlight span"""
        if not self.highlight_spans:
            self.highlight_spans = []
        self.highlight_spans.append({"start": start, "end": end, "text": text})

    def set_modality_score(self, modality: str, score: float):
        """Set score for a specific modality"""
        if not self.modality_scores:
            self.modality_scores = {}
        self.modality_scores[modality] = score

    def get_modality_score(self, modality: str, default: float = 0.0) -> float:
        """Get score for a specific modality"""
        if not self.modality_scores:
            return default
        return self.modality_scores.get(modality, default)

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Add computed fields
        data["click_through_rate"] = self.click_through_rate
        data["dwell_time_seconds"] = self.dwell_time_seconds

        # Include document information
        if self.document:
            data["document"] = {
                "id": str(self.document.id),
                "title": self.document.title,
                "document_type": self.document.document_type.value,
                "content_preview": self.document.get_content_preview(150),
            }

        return data

    @classmethod
    def get_clicked_results(
        cls,
        search_query_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
    ) -> list:
        """Get clicked search results"""
        query = cls.query.filter(cls.was_clicked == True, cls.is_deleted == False)

        if search_query_id:
            query = query.filter(cls.search_query_id == search_query_id)

        if organization_id:
            query = query.join(SearchQuery).filter(
                SearchQuery.organization_id == organization_id
            )

        return query.all()

    @classmethod
    def get_high_scoring_results(
        cls, min_score: float = 0.8, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get high scoring search results"""
        query = cls.query.filter(
            cls.relevance_score >= min_score, cls.is_deleted == False
        )

        if organization_id:
            query = query.join(SearchQuery).filter(
                SearchQuery.organization_id == organization_id
            )

        return query.all()
