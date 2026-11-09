"""
Enhanced document processing models for multimodal RAG system
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class ProcessingStage(PyEnum):
    """Processing stages for documents"""

    UPLOADED = "uploaded"
    VALIDATED = "validated"
    EXTRACTED = "extracted"
    ANALYZED = "analyzed"
    INDEXED = "indexed"
    EMBEDDED = "embedded"
    COMPLETED = "completed"


class ContentType(PyEnum):
    """Content types for multimodal processing"""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    METADATA = "metadata"


class QualityMetricType(PyEnum):
    """Quality metric types"""

    TEXT_CLARITY = "text_clarity"
    IMAGE_RESOLUTION = "image_resolution"
    AUDIO_CLARITY = "audio_clarity"
    VIDEO_QUALITY = "video_quality"
    CONTENT_RICHNESS = "content_richness"
    EXTRACTION_ACCURACY = "extraction_accuracy"


class ProcessingHistory(BaseModel):
    """
    History tracking for document processing pipeline
    """

    __tablename__ = "processing_history"

    # Document reference
    document_id = Column(GUID(), ForeignKey("documents.id"), nullable=False, index=True)

    # Processing stage information
    stage = Column(Enum(ProcessingStage), nullable=False, index=True)
    status = Column(
        String(20), nullable=False, default="pending"
    )  # pending, running, completed, failed

    # Timing information
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)

    # Processing details
    processor_id = Column(String(255), nullable=True)  # Worker/process ID
    processing_config = Column(JSONB, nullable=True)  # Configuration used
    processing_metadata = Column(JSONB, nullable=True)  # Additional metadata

    # Error handling
    error_message = Column(Text, nullable=True)
    error_type = Column(String(100), nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)

    # Resource usage
    cpu_time_seconds = Column(Float, nullable=True)
    memory_peak_mb = Column(Float, nullable=True)

    # Progress tracking
    progress_percentage = Column(Float, default=0.0, nullable=False)
    current_step = Column(String(255), nullable=True)

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)

    # Relationships
    document = relationship("Document", back_populates="processing_history")
    organization = relationship("Organization")

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "progress_percentage >= 0 AND progress_percentage <= 100",
            name="check_progress_range",
        ),
        CheckConstraint("duration_seconds >= 0", name="check_duration_positive"),
        CheckConstraint("retry_count >= 0", name="check_retry_non_negative"),
        # Created by f931599b6b5b_enhance_document_processing.
        Index(
            "idx_processing_history_document_stage",
            "document_id",
            "stage",
            "created_at",
        ),
        Index(
            "idx_processing_history_org_status",
            "organization_id",
            "status",
            "stage",
        ),
    )

    def __repr__(self):
        return f"<ProcessingHistory(document_id={self.document_id}, stage={self.stage.value}, status={self.status})>"


class DocumentVersion(BaseModel):
    """
    Version control for documents
    """

    __tablename__ = "document_versions"

    # Document reference
    document_id = Column(GUID(), ForeignKey("documents.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)

    # Version information
    version_label = Column(
        String(100), nullable=True
    )  # e.g., "v1.0.0", "draft", "final"
    change_description = Column(Text, nullable=True)
    is_major_version = Column(Boolean, default=False, nullable=False)
    is_current_version = Column(Boolean, default=False, nullable=False)

    # File information
    file_path = Column(String(1000), nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    file_hash = Column(String(64), nullable=True)  # SHA-256 hash
    mime_type = Column(String(100), nullable=False)

    # Content changes
    content_diff = Column(JSONB, nullable=True)  # Structured diff information
    changed_sections = Column(ARRAY(String), nullable=True)  # List of changed sections

    # Version metadata
    version_metadata = Column(JSONB, nullable=True)

    # Creation tracking
    created_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=False)
    parent_version_id = Column(
        GUID(), ForeignKey("document_versions.id"), nullable=True
    )

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)

    # Relationships
    document = relationship("Document", back_populates="versions")
    created_by_user = relationship("User")
    parent_version = relationship(
        "DocumentVersion",
        remote_side="DocumentVersion.id",
        back_populates="child_versions",
    )
    child_versions = relationship("DocumentVersion", back_populates="parent_version")
    organization = relationship("Organization")

    # Constraints
    __table_args__ = (
        CheckConstraint("version_number > 0", name="check_version_positive"),
        CheckConstraint("file_size_bytes >= 0", name="check_file_size_positive"),
        # Unique constraint on document_id and version_number
    )

    def __repr__(self):
        return f"<DocumentVersion(document_id={self.document_id}, version={self.version_number})>"


class MultimodalContent(BaseModel):
    """
    Extracted content from different modalities
    """

    __tablename__ = "multimodal_content"

    # Document reference
    document_id = Column(GUID(), ForeignKey("documents.id"), nullable=False, index=True)
    document_version_id = Column(
        GUID(), ForeignKey("document_versions.id"), nullable=True
    )

    # Content identification
    # values_callable: the DB enum was created with lowercase values
    # (alembic/versions/f931599b6b5b) but plain Enum(ContentType) binds the
    # member *name* ("IMAGE"), not its value ("image") -> invalid input value
    # for enum contenttype. Mirrors thread.py's ThreadStatus fix. Model-side
    # only, no migration (the DB type is unchanged).
    content_type = Column(
        Enum(
            ContentType,
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
        ),
        nullable=False,
        index=True,
    )
    content_id = Column(
        String(255), nullable=False, index=True
    )  # Unique ID within document
    sequence_order = Column(Integer, default=0, nullable=False)

    # Content data
    raw_content = Column(Text, nullable=True)  # Raw extracted content
    processed_content = Column(Text, nullable=True)  # Processed/cleaned content
    content_metadata = Column(JSONB, nullable=True)  # Content-specific metadata

    # Quality information
    quality_score = Column(Float, nullable=True)  # 0.0 to 1.0
    extraction_method = Column(String(100), nullable=True)
    extraction_confidence = Column(Float, nullable=True)  # 0.0 to 1.0

    # Media-specific fields
    media_duration_seconds = Column(Float, nullable=True)  # For audio/video
    media_dimensions = Column(
        JSONB, nullable=True
    )  # For images/video {"width": 1920, "height": 1080}
    media_format = Column(String(50), nullable=True)  # e.g., "jpg", "mp3", "mp4"

    # Text-specific fields
    language_code = Column(String(10), nullable=True)  # e.g., "en", "es"
    word_count = Column(Integer, nullable=True)
    character_count = Column(Integer, nullable=True)

    # Indexing information
    is_indexed = Column(Boolean, default=False, nullable=False)
    embedding_id = Column(String(255), nullable=True, index=True)
    search_vector = Column(Text, nullable=True)  # For full-text search

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)

    # Relationships
    document = relationship("Document", back_populates="multimodal_content")
    document_version = relationship("DocumentVersion")
    organization = relationship("Organization")

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1",
            name="check_quality_score_range",
        ),
        CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="check_extraction_confidence_range",
        ),
        CheckConstraint("sequence_order >= 0", name="check_sequence_order_positive"),
        CheckConstraint("word_count >= 0", name="check_word_count_positive"),
        CheckConstraint("character_count >= 0", name="check_character_count_positive"),
        CheckConstraint(
            "media_duration_seconds >= 0", name="check_media_duration_positive"
        ),
        # Created by f931599b6b5b_enhance_document_processing.
        Index(
            "idx_multimodal_content_document_type",
            "document_id",
            "content_type",
            "sequence_order",
        ),
        Index(
            "idx_multimodal_content_indexed",
            "is_indexed",
            "content_type",
            "organization_id",
        ),
    )

    def __repr__(self):
        return f"<MultimodalContent(document_id={self.document_id}, type={self.content_type.value})>"


class DocumentQualityMetrics(BaseModel):
    """
    Quality metrics for documents and content
    """

    __tablename__ = "document_quality_metrics"

    # Document reference
    document_id = Column(GUID(), ForeignKey("documents.id"), nullable=False, index=True)
    content_id = Column(
        String(255), nullable=True, index=True
    )  # Reference to specific content

    # Metric information
    metric_type = Column(Enum(QualityMetricType), nullable=False, index=True)
    metric_value = Column(Float, nullable=False)
    metric_unit = Column(
        String(50), nullable=True
    )  # e.g., "score", "percentage", "pixels"

    # Assessment information
    assessment_method = Column(
        String(100), nullable=True
    )  # e.g., "automated", "manual", "hybrid"
    assessment_version = Column(
        String(50), nullable=True
    )  # Version of assessment algorithm
    confidence_score = Column(Float, nullable=True)  # Confidence in metric assessment

    # Thresholds and status
    threshold_min = Column(Float, nullable=True)
    threshold_max = Column(Float, nullable=True)
    threshold_target = Column(Float, nullable=True)
    meets_threshold = Column(Boolean, nullable=True, index=True)

    # Detailed metrics
    metric_details = Column(JSONB, nullable=True)  # Detailed breakdown
    comparison_baseline = Column(JSONB, nullable=True)  # Comparison with baseline

    # Assessment metadata
    assessed_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    assessment_config = Column(
        JSONB, nullable=True
    )  # Configuration used for assessment

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)

    # Relationships
    document = relationship("Document", back_populates="quality_metrics")
    assessed_by_user = relationship("User")
    organization = relationship("Organization")

    # Constraints
    __table_args__ = (
        CheckConstraint("metric_value >= 0", name="check_metric_value_positive"),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="check_confidence_score_range",
        ),
        # Created by f931599b6b5b_enhance_document_processing.
        Index(
            "idx_quality_metrics_document_type",
            "document_id",
            "metric_type",
            "created_at",
        ),
        Index(
            "idx_quality_metrics_threshold",
            "meets_threshold",
            "metric_type",
            "organization_id",
        ),
    )

    def __repr__(self):
        return f"<DocumentQualityMetrics(document_id={self.document_id}, metric={self.metric_type.value}, value={self.metric_value})>"


class DocumentAccessLog(BaseModel):
    """
    Access logging for documents for security audit
    """

    __tablename__ = "document_access_log"

    # Document reference
    document_id = Column(GUID(), ForeignKey("documents.id"), nullable=False, index=True)
    document_version_id = Column(
        GUID(), ForeignKey("document_versions.id"), nullable=True
    )

    # Access information
    access_type = Column(
        String(50), nullable=False, index=True
    )  # view, download, edit, delete, share
    access_result = Column(
        String(20), nullable=False, default="success"
    )  # success, denied, failed

    # User information
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    session_id = Column(String(255), nullable=True, index=True)
    api_key_id = Column(String(255), nullable=True, index=True)

    # Request information
    request_method = Column(String(10), nullable=True)  # GET, POST, PUT, DELETE
    request_path = Column(String(1000), nullable=True)
    request_query_params = Column(JSONB, nullable=True)

    # Client information
    ip_address = Column(String(45), nullable=True, index=True)  # IPv6 compatible
    user_agent = Column(Text, nullable=True)
    referer = Column(String(1000), nullable=True)

    # Response information
    response_status_code = Column(Integer, nullable=True)
    response_size_bytes = Column(Integer, nullable=True)
    response_time_ms = Column(Float, nullable=True)

    # Security information
    is_suspicious = Column(Boolean, default=False, nullable=False, index=True)
    threat_score = Column(Float, nullable=True)  # 0.0 to 1.0
    security_flags = Column(JSONB, nullable=True)  # Security-related flags

    # Access metadata
    access_metadata = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)

    # Relationships
    document = relationship("Document", back_populates="access_logs")
    document_version = relationship("DocumentVersion")
    user = relationship("User")
    organization = relationship("Organization")

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "response_size_bytes >= 0", name="check_response_size_positive"
        ),
        CheckConstraint("response_time_ms >= 0", name="check_response_time_positive"),
        CheckConstraint(
            "threat_score >= 0 AND threat_score <= 1", name="check_threat_score_range"
        ),
        # Created by f931599b6b5b_enhance_document_processing.
        Index("idx_access_log_document_time", "document_id", "created_at"),
        Index("idx_access_log_user_time", "user_id", "created_at"),
        Index(
            "idx_access_log_suspicious",
            "is_suspicious",
            "threat_score",
            "created_at",
        ),
    )

    def __repr__(self):
        return f"<DocumentAccessLog(document_id={self.document_id}, access_type={self.access_type}, result={self.access_result})>"
