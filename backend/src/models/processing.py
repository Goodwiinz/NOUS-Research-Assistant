"""
Processing job model for background task management
"""

import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
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


class JobType(PyEnum):
    """Types of processing jobs"""

    DOCUMENT_INGESTION = "document_ingestion"
    TEXT_EXTRACTION = "text_extraction"
    EMBEDDING_GENERATION = "embedding_generation"
    ENTITY_EXTRACTION = "entity_extraction"
    GRAPH_INDEXING = "graph_indexing"
    QUALITY_EVALUATION = "quality_evaluation"
    BATCH_PROCESSING = "batch_processing"
    CLEANUP = "cleanup"


def _as_utc(value: datetime) -> datetime:
    """``started_at`` is timestamptz, but a refreshed row can come back naive
    on SQLite (unit tests); treat naive as UTC so durations stay computable."""
    return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)


class JobStatus(PyEnum):
    """Status of processing jobs"""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class JobPriority(PyEnum):
    """Priority levels for jobs"""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


class ProcessingJob(BaseModel):
    """Processing job model for background task management"""

    __tablename__ = "processing_jobs"

    # Job information
    job_type = Column(Enum(JobType), nullable=False, index=True)
    status = Column(
        Enum(JobStatus), nullable=False, default=JobStatus.PENDING, index=True
    )
    priority = Column(
        Enum(JobPriority), nullable=False, default=JobPriority.NORMAL, index=True
    )

    # Job configuration
    parameters = Column(JSON, nullable=True)  # Job-specific parameters
    config = Column(JSON, nullable=True)  # Job configuration
    requirements = Column(
        JSON, nullable=True
    )  # System requirements (memory, CPU, etc.)

    # Execution information
    celery_task_id = Column(String(255), nullable=True, index=True)  # Celery task ID
    worker_id = Column(String(255), nullable=True)  # Worker that processed the job
    queue_name = Column(String(100), nullable=True)  # Queue name

    # Timing
    queued_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Duration and performance
    duration_seconds = Column(Float, nullable=True)
    cpu_time_seconds = Column(Float, nullable=True)
    memory_peak_mb = Column(Float, nullable=True)

    # Progress tracking
    progress_percentage = Column(Float, default=0.0, nullable=False)
    current_step = Column(String(255), nullable=True)
    total_steps = Column(Integer, nullable=True)
    completed_steps = Column(Integer, default=0, nullable=False)

    # Error handling
    error_message = Column(Text, nullable=True)
    error_type = Column(String(100), nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)

    # Results
    result = Column(JSON, nullable=True)  # Job result data
    artifacts = Column(
        JSON, nullable=True
    )  # Generated artifacts (files, vectors, etc.)
    metrics = Column(JSON, nullable=True)  # Performance metrics

    # Relationships
    document_id = Column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    organization_id = Column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Database indexes for performance optimization
    __table_args__ = (
        Index("idx_job_status_created", "status", "created_at"),
        Index("idx_job_type_org", "job_type", "organization_id"),
    )

    # Relationships
    document = relationship("Document", back_populates="processing_jobs")
    organization = relationship("Organization")
    created_by_user = relationship("User")

    def __repr__(self):
        return f"<ProcessingJob(type={self.job_type.value}, status={self.status.value}, progress={self.progress_percentage}%)>"

    @property
    def is_finished(self) -> bool:
        """Check if job is finished (completed, failed, or cancelled)"""
        return self.status in [
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ]

    @property
    def is_active(self) -> bool:
        """Check if job is active (queued, running, or retrying)"""
        return self.status in [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING]

    @property
    def can_retry(self) -> bool:
        """Check if job can be retried"""
        return self.status == JobStatus.FAILED and self.retry_count < self.max_retries

    @property
    def estimated_remaining_time(self) -> float:
        """Estimate remaining time in seconds"""
        if not self.is_active or self.progress_percentage <= 0:
            return None

        if self.duration_seconds and self.progress_percentage > 0:
            total_estimated = self.duration_seconds / (self.progress_percentage / 100)
            return max(0, total_estimated - self.duration_seconds)

        return None

    def queue_job(self, queue_name: str = None):
        """Queue the job for processing"""
        self.status = JobStatus.QUEUED
        self.queued_at = datetime.utcnow()
        if queue_name:
            self.queue_name = queue_name

    def start_job(self, worker_id: str = None, celery_task_id: str = None):
        """Mark job as started"""
        self.status = JobStatus.RUNNING
        self.started_at = datetime.now(dt_timezone.utc)
        if worker_id:
            self.worker_id = worker_id
        if celery_task_id:
            self.celery_task_id = celery_task_id

    def complete_job(
        self, result: dict = None, artifacts: dict = None, metrics: dict = None
    ):
        """Mark job as completed"""
        self.status = JobStatus.COMPLETED
        self.completed_at = datetime.now(dt_timezone.utc)
        self.progress_percentage = 100.0
        self.completed_steps = self.total_steps

        if result:
            self.result = result
        if artifacts:
            self.artifacts = artifacts
        if metrics:
            self.metrics = metrics

        # Calculate duration
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - _as_utc(self.started_at)
            ).total_seconds()

    def fail_job(self, error_message: str, error_type: str = None):
        """Mark job as failed"""
        self.status = JobStatus.FAILED
        self.completed_at = datetime.now(dt_timezone.utc)
        self.error_message = error_message
        if error_type:
            self.error_type = error_type

        # Calculate duration
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - _as_utc(self.started_at)
            ).total_seconds()

    def cancel_job(self):
        """Cancel the job"""
        self.status = JobStatus.CANCELLED
        self.completed_at = datetime.now(dt_timezone.utc)

        # Calculate duration
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - _as_utc(self.started_at)
            ).total_seconds()

    def retry_job(self):
        """Prepare job for retry"""
        if self.can_retry:
            self.status = JobStatus.RETRYING
            self.retry_count += 1
            self.error_message = None
            self.error_type = None
            self.progress_percentage = 0.0
            self.started_at = None
            self.completed_at = None

    def update_progress(
        self, current_step: str = None, progress_percentage: float = None
    ):
        """Update job progress"""
        if current_step:
            self.current_step = current_step
        if progress_percentage is not None:
            self.progress_percentage = min(100.0, max(0.0, progress_percentage))

        if progress_percentage and self.total_steps:
            self.completed_steps = int((progress_percentage / 100.0) * self.total_steps)

    def update_metrics(self, cpu_time: float = None, memory_mb: float = None):
        """Update performance metrics"""
        if cpu_time is not None:
            self.cpu_time_seconds = cpu_time
        if memory_mb is not None:
            self.memory_peak_mb = memory_mb

    def add_artifact(self, name: str, artifact_data):
        """Add an artifact to the job"""
        if not self.artifacts:
            self.artifacts = {}
        self.artifacts[name] = artifact_data

    def get_artifact(self, name: str, default=None):
        """Get an artifact from the job"""
        if not self.artifacts:
            return default
        return self.artifacts.get(name, default)

    def add_result(self, key: str, value):
        """Add a result value"""
        if not self.result:
            self.result = {}
        self.result[key] = value

    def get_result(self, key: str, default=None):
        """Get a result value"""
        if not self.result:
            return default
        return self.result.get(key, default)

    def to_dict(self, include_artifacts: bool = False) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data["job_type"] = self.job_type.value if self.job_type else None
        data["status"] = self.status.value if self.status else None
        data["priority"] = self.priority.value if self.priority else None

        # Add computed fields
        data["is_finished"] = self.is_finished
        data["is_active"] = self.is_active
        data["can_retry"] = self.can_retry
        data["estimated_remaining_time"] = self.estimated_remaining_time

        # Include artifacts if requested
        if include_artifacts:
            data["artifacts"] = self.artifacts
        else:
            data.pop("artifacts", None)

        return data

    @classmethod
    def get_jobs_by_status(
        cls, status: JobStatus, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get jobs by status"""
        query = cls.query.filter(cls.status == status, cls.is_deleted == False)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def get_jobs_by_type(
        cls, job_type: JobType, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get jobs by type"""
        query = cls.query.filter(cls.job_type == job_type, cls.is_deleted == False)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def get_queue_length(
        cls, queue_name: str = None, organization_id: Optional[uuid.UUID] = None
    ) -> int:
        """Get number of jobs in queue"""
        query = cls.query.filter(
            cls.status == JobStatus.QUEUED, cls.is_deleted == False
        )
        if queue_name:
            query = query.filter(cls.queue_name == queue_name)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.count()

    @classmethod
    def get_failed_jobs_to_retry(
        cls, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get failed jobs that can be retried"""
        query = cls.query.filter(
            cls.status == JobStatus.FAILED,
            cls.retry_count < cls.max_retries,
            cls.is_deleted == False,
        )
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()
