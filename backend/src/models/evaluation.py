"""
Evaluation models for RAG Triad metrics and evaluation workflows
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class EvaluationType(Enum):
    """Types of evaluations"""

    # The dataset-evaluation endpoint's canonical job type (its request
    # schema defaults to this and always dispatches run_rag_triad_evaluation).
    RAG_TRIAD = "rag_triad"
    ANSWER_RELEVANCY = "answer_relevancy"
    FAITHFULNESS = "faithfulness"
    CONTEXTUAL_RELEVANCY = "contextual_relevancy"
    CUSTOM_METRIC = "custom_metric"
    BATCH_EVALUATION = "batch_evaluation"
    REAL_TIME_EVALUATION = "real_time_evaluation"


class EvaluationStatus(Enum):
    """Evaluation job statuses"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MetricType(Enum):
    """Types of metrics that can be calculated"""

    RAG_TRIAD_ANSWER_RELEVANCY = "rag_triad_answer_relevancy"
    RAG_TRIAD_FAITHFULNESS = "rag_triad_faithfulness"
    RAG_TRIAD_CONTEXTUAL_RELEVANCY = "rag_triad_contextual_relevancy"
    RESPONSE_TIME = "response_time"
    HALLUCINATION_RATE = "hallucination_rate"
    CROSS_MODAL_COHERENCE = "cross_modal_coherence"
    PRECISION = "precision"
    RECALL = "recall"
    F1_SCORE = "f1_score"
    CUSTOM = "custom"


class EvaluationJob(BaseModel):
    """
    Main evaluation job model for tracking evaluation workflows
    """

    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        # Created by n9p2q3r4s5t6_add_missing_performance_indexes.
        Index("idx_evaluation_jobs_org_status", "organization_id", "status"),
        Index("idx_evaluation_jobs_user_created", "user_id", "created_at"),
        {"extend_existing": True},
    )

    # Basic information
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text)
    evaluation_type = Column(String(50), nullable=False, index=True)

    # Job status and timing
    status = Column(
        String(50), nullable=False, default=EvaluationStatus.PENDING.value, index=True
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)

    # Job configuration
    parameters = Column(JSON, nullable=False, default=dict)
    dataset_size = Column(Integer, default=0)
    processed_count = Column(Integer, default=0)

    # Ownership and organization
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True, index=True)
    organization_id = Column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )

    # Results summary
    overall_score = Column(Float, nullable=True)
    success_rate = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    # Relationships
    user = relationship("User", back_populates="evaluation_jobs")
    organization = relationship("Organization", back_populates="evaluation_jobs")
    metrics = relationship(
        "EvaluationMetric", back_populates="job", cascade="all, delete-orphan"
    )
    datasets = relationship(
        "EvaluationDataset", back_populates="job", cascade="all, delete-orphan"
    )

    def start_job(self):
        """Mark job as started"""
        self.status = EvaluationStatus.RUNNING.value
        self.started_at = datetime.utcnow()

    def complete_job(self, overall_score: float = None, success_rate: float = None):
        """Mark job as completed"""
        self.status = EvaluationStatus.COMPLETED.value
        self.completed_at = datetime.utcnow()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.overall_score = overall_score
        self.success_rate = success_rate

    def fail_job(self, error_message: str):
        """Mark job as failed"""
        self.status = EvaluationStatus.FAILED.value
        self.completed_at = datetime.utcnow()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.error_message = error_message

    def cancel_job(self):
        """Mark job as cancelled"""
        self.status = EvaluationStatus.CANCELLED.value
        self.completed_at = datetime.utcnow()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def update_progress(self, processed_count: int):
        """Update processing progress"""
        self.processed_count = processed_count

    def to_dict(self):
        """Convert model to dictionary with additional fields"""
        result = super().to_dict()
        result["evaluation_type"] = self.evaluation_type
        result["status"] = self.status
        return result


class EvaluationMetric(BaseModel):
    """
    Individual metric results from evaluations
    """

    __tablename__ = "evaluation_metrics"
    __table_args__ = {"extend_existing": True}

    # Job association
    job_id = Column(
        GUID(), ForeignKey("evaluation_jobs.id"), nullable=False, index=True
    )

    # Metric information
    metric_type = Column(String(100), nullable=False, index=True)
    metric_name = Column(String(255), nullable=False)

    # Metric values
    value = Column(Float, nullable=False)
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)
    mean_value = Column(Float, nullable=True)
    std_deviation = Column(Float, nullable=True)

    # Threshold and quality
    threshold_min = Column(Float, nullable=True)
    threshold_max = Column(Float, nullable=True)
    is_threshold_violation = Column(Boolean, default=False)

    # Evaluation context
    query = Column(Text, nullable=False)
    generated_answer = Column(Text, nullable=True)
    reference_answer = Column(Text, nullable=True)
    retrieved_context = Column(Text, nullable=True)

    # Metadata and configuration
    metric_metadata = Column(JSON, nullable=True, default=dict)
    calculation_method = Column(String(100), nullable=True)
    model_used = Column(String(255), nullable=True)
    additional_data = Column(JSON, nullable=True, default=dict)

    # Relationships
    job = relationship("EvaluationJob", back_populates="metrics")

    def calculate_statistics(self, values: List[float]):
        """Calculate statistical measures for a collection of values"""
        if not values:
            return

        import statistics

        self.min_value = min(values)
        self.max_value = max(values)
        self.mean_value = statistics.mean(values)
        if len(values) > 1:
            self.std_deviation = statistics.stdev(values)
        else:
            self.std_deviation = 0.0

    def check_threshold_violation(self):
        """Check if the metric value violates threshold conditions"""
        if self.threshold_min is not None and self.value < self.threshold_min:
            self.is_threshold_violation = True
        elif self.threshold_max is not None and self.value > self.threshold_max:
            self.is_threshold_violation = True
        else:
            self.is_threshold_violation = False


class EvaluationDataset(BaseModel):
    """
    Dataset for evaluation - contains test questions and reference answers
    """

    __tablename__ = "evaluation_datasets"
    __table_args__ = {"extend_existing": True}

    # Job association
    job_id = Column(
        GUID(), ForeignKey("evaluation_jobs.id"), nullable=False, index=True
    )

    # Dataset information
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    dataset_type = Column(String(50), nullable=False, default="qa_pairs")

    # Dataset content
    questions = Column(JSON, nullable=False)  # List of questions
    reference_answers = Column(JSON, nullable=True)  # List of reference answers
    contexts = Column(JSON, nullable=True)  # Expected contexts for each question

    # Dataset metadata
    source_type = Column(String(100), nullable=True)  # synthetic, human_annotated, etc.
    domain = Column(String(100), nullable=True)
    difficulty_level = Column(String(50), nullable=True)
    language = Column(String(10), default="en")

    # Processing status
    is_processed = Column(Boolean, default=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    job = relationship("EvaluationJob", back_populates="datasets")


class EvaluationThreshold(BaseModel):
    """
    Thresholds for evaluation metrics with organization-specific configurations
    """

    __tablename__ = "evaluation_thresholds"
    __table_args__ = {"extend_existing": True}

    # Threshold configuration
    metric_type = Column(String(100), nullable=False, index=True)
    threshold_min = Column(Float, nullable=True)
    threshold_max = Column(Float, nullable=True)

    # Organization and scope
    organization_id = Column(
        GUID(), ForeignKey("organizations.id"), nullable=True, index=True
    )
    search_type = Column(String(50), nullable=True, index=True)
    document_type = Column(String(50), nullable=True, index=True)

    # Threshold metadata
    is_enabled = Column(Boolean, default=True, index=True)
    description = Column(Text, nullable=True)
    severity_level = Column(String(20), default="medium")  # low, medium, high, critical

    # Alert configuration
    alert_on_violation = Column(Boolean, default=True)
    alert_cooldown_minutes = Column(Integer, default=60)

    # Relationships
    organization = relationship("Organization")


class EvaluationComparison(BaseModel):
    """
    Comparison results between different evaluations or configurations
    """

    __tablename__ = "evaluation_comparisons"
    __table_args__ = {"extend_existing": True}

    # Comparison information
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Reference jobs being compared
    baseline_job_id = Column(GUID(), ForeignKey("evaluation_jobs.id"), nullable=True)
    comparison_job_id = Column(GUID(), ForeignKey("evaluation_jobs.id"), nullable=True)

    # Comparison results
    baseline_score = Column(Float, nullable=True)
    comparison_score = Column(Float, nullable=True)
    improvement_percentage = Column(Float, nullable=True)
    statistical_significance = Column(Float, nullable=True)  # p-value

    # Detailed comparison data
    metric_comparisons = Column(JSON, nullable=True, default=dict)
    summary = Column(Text, nullable=True)
    recommendation = Column(Text, nullable=True)

    # Ownership
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True, index=True)
    organization_id = Column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )

    # Relationships
    baseline_job = relationship("EvaluationJob", foreign_keys=[baseline_job_id])
    comparison_job = relationship("EvaluationJob", foreign_keys=[comparison_job_id])
    user = relationship("User")
    organization = relationship("Organization")


class EvaluationReport(BaseModel):
    """
    Generated reports for evaluation results
    """

    __tablename__ = "evaluation_reports"
    __table_args__ = {"extend_existing": True}

    # Report information
    title = Column(String(255), nullable=False)
    report_type = Column(String(50), nullable=False)  # summary, detailed, comparison

    # Associated evaluation
    job_id = Column(
        GUID(), ForeignKey("evaluation_jobs.id"), nullable=False, index=True
    )

    # Report content
    content = Column(Text, nullable=False)
    executive_summary = Column(Text, nullable=True)
    key_findings = Column(JSON, nullable=True, default=list)
    recommendations = Column(JSON, nullable=True, default=list)

    # Report metadata
    format_type = Column(String(20), default="markdown")  # markdown, html, pdf
    template_used = Column(String(100), nullable=True)
    generated_by_model = Column(String(255), nullable=True)

    # File information
    file_path = Column(String(500), nullable=True)
    file_size_bytes = Column(Integer, nullable=True)

    # Ownership
    user_id = Column(GUID(), ForeignKey("users.id"), nullable=True, index=True)
    organization_id = Column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )

    # Relationships
    job = relationship("EvaluationJob")
    user = relationship("User")
    organization = relationship("Organization")


from .organization import Organization
from .user import User

# Add back-populates relationships to User and Organization.
# These must be set here (not in user.py/organization.py) to avoid
# circular import issues — evaluation.py depends on both models.
User.evaluation_jobs = relationship("EvaluationJob", back_populates="user")
Organization.evaluation_jobs = relationship(
    "EvaluationJob", back_populates="organization"
)
