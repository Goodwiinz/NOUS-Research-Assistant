"""
Document model for multimodal content storage and management
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import joinedload, relationship, selectinload

from .base import GUID, BaseModel
from .utils import StringArray

# Live-row predicates of the org-scoped dedup indexes; keep them identical to
# the migrations that create them (uq_documents_org_checksum and
# i9j0k1l2m3n4). The upload path's dedup catches a violation of
# uq_documents_org_checksum_live by name (services/documents/file_service.py).
LIVE_CHECKSUM = "is_deleted = false AND checksum_sha256 IS NOT NULL"
LIVE_ARXIV_ID = "is_deleted = false AND arxiv_id IS NOT NULL"


class DocumentType(PyEnum):
    """Document types for different modalities"""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    PDF = "pdf"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    MULTIMODAL = "multimodal"


class ProcessingStatus(PyEnum):
    """Processing status for documents"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class Document(BaseModel):
    """Document model for multimodal content"""

    __tablename__ = "documents"

    # Basic information
    title = Column(String(500), nullable=False, index=True)
    filename = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String(100), nullable=False)
    document_type = Column(Enum(DocumentType), nullable=False, index=True)

    # Storage
    storage_path = Column(String(2000), nullable=True)  # Storage key (bucket/key)
    storage_backend = Column(String(20), nullable=False, server_default="local")
    checksum_sha256 = Column(
        String(64), nullable=True, index=True
    )  # SHA-256 content hash
    # Canonical exact-revision key for arXiv documents. Legacy rows are
    # nullable until the audited duplicate cleanup selects their survivor.
    arxiv_id = Column(String(64), nullable=True)

    # Content
    content_text = Column(Text, nullable=True)  # Extracted text content
    content_summary = Column(Text, nullable=True)  # AI-generated summary
    document_metadata = Column(JSON, nullable=True)  # Document-specific metadata

    # Full-text search
    search_vector = Column(
        TSVECTOR, nullable=True
    )  # PostgreSQL full-text search vector

    # Processing
    processing_status = Column(
        Enum(ProcessingStatus), nullable=False, default=ProcessingStatus.PENDING
    )
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    processing_completed_at = Column(DateTime(timezone=True), nullable=True)
    processing_error = Column(Text, nullable=True)
    processing_retry_count = Column(Integer, default=0, nullable=False)

    # Vector and search
    embedding_id = Column(String(255), nullable=True, index=True)  # Qdrant vector ID
    is_embedded = Column(Boolean, default=False, nullable=False)
    is_indexed = Column(Boolean, default=False, nullable=False)

    # DigitalOcean Knowledge Base data source (Phase 2 dual-write)
    do_kb_data_source_uuid = Column(String(64), nullable=True, index=True)
    do_kb_indexed_at = Column(DateTime(timezone=True), nullable=True)
    # Per-document DO KB indexing health: indexed | skipped | failed | timeout
    do_kb_index_status = Column(String(20), nullable=True, index=True)

    # Per-satellite fan-out truth (audit D1). Values from
    # src.shared.enums.SatelliteSyncStatus (pending | completed | failed);
    # NULL = never attempted. Satellite failures are non-fatal by design (the
    # document still reaches COMPLETED), so these columns are the only record
    # that a satellite index drifted — the scheduled reconciler
    # (src.tasks.reconcile_tasks) re-drives rows marked 'failed'.
    # Outcome of the last Neo4j knowledge-graph indexing attempt:
    neo4j_index_status = Column(String(20), nullable=True, index=True)
    neo4j_indexed_at = Column(DateTime(timezone=True), nullable=True)
    # Outcome of the last DO KB sync attempt (data-source registration).
    # Distinct from do_kb_index_status, which tracks the KB-side indexing
    # lifecycle after a successful registration.
    do_kb_sync_status = Column(String(20), nullable=True, index=True)

    # Access control
    is_public = Column(Boolean, default=False, nullable=False)
    tags = Column(StringArray, nullable=True)

    # Organization
    organization_id = Column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    organization = relationship("Organization", back_populates="documents")
    uploaded_by_user = relationship("User", back_populates="documents")
    entities = relationship(
        "Entity", back_populates="document", cascade="all, delete-orphan"
    )
    processing_jobs = relationship(
        "ProcessingJob", back_populates="document", cascade="all, delete-orphan"
    )
    search_results = relationship(
        "SearchResult", back_populates="document", cascade="all, delete-orphan"
    )

    # Enhanced document processing relationships
    processing_history = relationship(
        "ProcessingHistory", back_populates="document", cascade="all, delete-orphan"
    )
    versions = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )
    multimodal_content = relationship(
        "MultimodalContent", back_populates="document", cascade="all, delete-orphan"
    )
    quality_metrics = relationship(
        "DocumentQualityMetrics",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    access_logs = relationship(
        "DocumentAccessLog", back_populates="document", cascade="all, delete-orphan"
    )

    # Database indexes for performance optimization
    __table_args__ = (
        Index("idx_document_org_user", "organization_id", "uploaded_by_user_id"),
        Index("idx_document_user_created", "uploaded_by_user_id", "created_at"),
        Index("idx_document_org_status", "organization_id", "processing_status"),
        Index("idx_document_org_type", "organization_id", "document_type"),
        Index("idx_document_status_created", "processing_status", "created_at"),
        Index("idx_document_embedded_indexed", "is_embedded", "is_indexed"),
        Index("idx_document_org_public", "organization_id", "is_public"),
        # Backs the default GET /documents list: WHERE org ORDER BY created_at DESC
        Index("idx_document_org_created", "organization_id", "created_at"),
        # Org-scoped partial unique indexes: one live row per content hash
        # (uq_documents_org_checksum) and per exact arXiv revision
        # (i9j0k1l2m3n4_add_documents_arxiv_id). Soft-deleted rows are exempt.
        Index(
            "uq_documents_org_checksum_live",
            "organization_id",
            "checksum_sha256",
            unique=True,
            postgresql_where=text(LIVE_CHECKSUM),
            sqlite_where=text(LIVE_CHECKSUM),
        ),
        Index(
            "uq_documents_org_arxiv_id_live",
            "organization_id",
            "arxiv_id",
            unique=True,
            postgresql_where=text(LIVE_ARXIV_ID),
            sqlite_where=text(LIVE_ARXIV_ID),
        ),
        # f931599b6b5b_enhance_document_processing:
        Index("ix_documents_is_embedded", "is_embedded"),
        Index("ix_documents_is_indexed", "is_indexed"),
        Index("ix_documents_is_public", "is_public"),
        Index("ix_documents_uploaded_by_user_id", "uploaded_by_user_id"),
        Index(
            "idx_documents_org_status_processing",
            "organization_id",
            "processing_status",
            "document_type",
        ),
        Index(
            "idx_documents_processing_queue",
            "processing_status",
            "created_at",
            "organization_id",
        ),
        Index(
            "idx_documents_searchable",
            "is_embedded",
            "is_indexed",
            "is_deleted",
            "organization_id",
        ),
        # p3q4r5s6t7u8_add_dashboard_performance_indexes:
        Index(
            "idx_documents_org_deleted_created",
            "organization_id",
            "is_deleted",
            "created_at",
        ),
        Index("idx_documents_org_title", "organization_id", "title"),
        Index(
            "idx_documents_search_gin",
            "search_vector",
            postgresql_using="gin",
            postgresql_where=text("search_vector IS NOT NULL"),
        ),
    )

    def __repr__(self):
        return f"<Document(title={self.title}, type={self.document_type.value}, status={self.processing_status.value})>"

    @classmethod
    def get_with_user_and_org(cls, document_id):
        """Get document with user and organization eagerly loaded"""
        return (
            cls.query.options(
                joinedload(cls.uploaded_by_user), joinedload(cls.organization)
            )
            .filter(cls.id == document_id)
            .first()
        )

    @classmethod
    def get_user_documents_with_details(cls, user_id, limit=50):
        """Get user's documents with organization loaded to avoid N+1"""
        return (
            cls.query.options(
                joinedload(cls.organization), selectinload(cls.processing_history)
            )
            .filter(cls.uploaded_by_user_id == user_id, cls.is_deleted == False)
            .order_by(cls.created_at.desc())
            .limit(limit)
            .all()
        )

    @classmethod
    def get_org_documents_with_users(cls, organization_id, limit=100):
        """Get organization documents with users loaded to avoid N+1"""
        return (
            cls.query.options(
                joinedload(cls.uploaded_by_user), selectinload(cls.quality_metrics)
            )
            .filter(cls.organization_id == organization_id, cls.is_deleted == False)
            .order_by(cls.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_metadata(self):
        """Get document metadata as dict"""
        return self.document_metadata or {}

    def set_metadata(self, value):
        """Set document metadata"""
        self.document_metadata = value

    @property
    def effective_file_path(self) -> str:
        """Return the best available file path (storage_path preferred over file_path)."""
        return self.storage_path or self.file_path

    @property
    def file_size_mb(self) -> float:
        """Get file size in MB"""
        return self.file_size_bytes / (1024 * 1024)

    @property
    def processing_duration_seconds(self) -> float:
        """Get processing duration in seconds"""
        if self.processing_started_at and self.processing_completed_at:
            return (
                self.processing_completed_at - self.processing_started_at
            ).total_seconds()
        return 0.0

    @property
    def is_processing_complete(self) -> bool:
        """Check if processing is complete (success or failure)"""
        return self.processing_status in [
            ProcessingStatus.COMPLETED,
            ProcessingStatus.FAILED,
        ]

    @property
    def is_processing_successful(self) -> bool:
        """Check if processing was successful"""
        return self.processing_status == ProcessingStatus.COMPLETED

    def can_be_searched(self) -> bool:
        """Check if document can be searched"""
        return (
            self.is_processing_successful
            and self.is_embedded
            and self.is_indexed
            and not self.is_deleted
        )

    def update_processing_status(self, status: ProcessingStatus, error: str = None):
        """Update processing status with timestamp"""
        self.processing_status = status

        if status == ProcessingStatus.PROCESSING and not self.processing_started_at:
            self.processing_started_at = datetime.utcnow()
            self.processing_retry_count += 1
        elif status in [ProcessingStatus.COMPLETED, ProcessingStatus.FAILED]:
            self.processing_completed_at = datetime.utcnow()
            if error:
                self.processing_error = error

    def add_metadata(self, key: str, value):
        """Add metadata entry"""
        current_metadata = self.get_metadata()
        current_metadata[key] = value
        self.set_metadata(current_metadata)

    def get_metadata_value(self, key: str, default=None):
        """Get metadata value"""
        current_metadata = self.get_metadata()
        return current_metadata.get(key, default)

    def add_tags(self, tags: list):
        """Add tags to document"""
        if not self.tags:
            self.tags = []
        # Remove duplicates and preserve order
        unique_tags = []
        for tag in tags + self.tags:
            if tag not in unique_tags:
                unique_tags.append(tag)
        self.tags = unique_tags

    def remove_tags(self, tags: list):
        """Remove tags from document"""
        if not self.tags:
            return
        for tag in tags:
            if tag in self.tags:
                self.tags.remove(tag)

    def get_content_preview(self, max_length: int = 200) -> str:
        """Get content preview"""
        if not self.content_text:
            return ""
        if len(self.content_text) <= max_length:
            return self.content_text
        return self.content_text[:max_length] + "..."

    def get_mapped_status(self) -> str:
        """Get processing status mapped to the public API vocabulary.

        Delegates to :class:`~src.shared.enums.ApiDocumentStatus` — the single
        source of truth for the db->api status translation.
        """
        from src.shared.enums import ApiDocumentStatus

        return ApiDocumentStatus.from_db(self.processing_status).value

    def to_dict(self, include_content: bool = False) -> dict:
        """Convert to dictionary"""
        from src.shared.enums import ApiDocumentStatus

        data = super().to_dict()

        # Convert enum values
        data["document_type"] = self.document_type.value if self.document_type else None

        # Map processing status to the public API vocabulary
        data["processing_status"] = ApiDocumentStatus.from_db(
            self.processing_status
        ).value

        # Add computed fields
        data["file_size_mb"] = self.file_size_mb
        data["processing_duration_seconds"] = self.processing_duration_seconds
        data["is_processing_complete"] = self.is_processing_complete
        data["is_processing_successful"] = self.is_processing_successful
        data["can_be_searched"] = self.can_be_searched()
        data["do_kb_index_status"] = self.do_kb_index_status
        # Per-satellite fan-out truth (audit D1)
        data["neo4j_index_status"] = self.neo4j_index_status
        data["do_kb_sync_status"] = self.do_kb_sync_status

        # Include content if requested
        if not include_content:
            data.pop("content_text", None)

        # Remove sensitive/internal fields (R4-L4). storage_path/storage_backend
        # expose the raw object-storage key + backend layout, checksum_sha256
        # is an internal dedup key, and do_kb_data_source_uuid is an internal
        # DO Knowledge Base identifier — none of these are part of the public
        # document API contract.
        for internal_field in (
            "file_path",
            "storage_path",
            "storage_backend",
            "checksum_sha256",
            "do_kb_data_source_uuid",
        ):
            data.pop(internal_field, None)

        return data

    @classmethod
    def get_documents_by_type(cls, document_type: DocumentType) -> list:
        """Get documents by type"""
        return cls.query.filter(
            cls.document_type == document_type, cls.is_deleted == False
        ).all()

    @classmethod
    def get_processing_queue(cls, limit: int = 50) -> list:
        """Get documents in processing queue"""
        return (
            cls.query.filter(
                cls.processing_status == ProcessingStatus.PENDING,
                cls.is_deleted == False,
            )
            .order_by(cls.created_at)
            .limit(limit)
            .all()
        )

    @classmethod
    def get_failed_documents(cls, retry_threshold: int = 3) -> list:
        """Get failed documents that haven't exceeded retry threshold"""
        return (
            cls.query.filter(
                cls.processing_status == ProcessingStatus.FAILED,
                cls.processing_retry_count < retry_threshold,
                cls.is_deleted == False,
            )
            .order_by(cls.processing_retry_count)
            .all()
        )
