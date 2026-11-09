"""
Quality metrics model for search and processing evaluation
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


class MetricType(PyEnum):
    """Types of quality metrics"""

    PRECISION = "precision"
    RECALL = "recall"
    F1_SCORE = "f1_score"
    ACCURACY = "accuracy"
    RELEVANCE = "relevance"
    COHERENCE = "coherence"
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    ERROR_RATE = "error_rate"
    USER_SATISFACTION = "user_satisfaction"
    BUSINESS_IMPACT = "business_impact"


class EvaluationType(PyEnum):
    """Types of evaluations"""

    AUTOMATED = "automated"
    HUMAN = "human"
    HYBRID = "hybrid"
    A_B_TEST = "ab_test"
    CONTINUOUS = "continuous"


class MetricScope(PyEnum):
    """Scope of metrics"""

    SYSTEM = "system"
    ORGANIZATION = "organization"
    USER = "user"
    DOCUMENT = "document"
    SEARCH_QUERY = "search_query"
    PROCESSING_JOB = "processing_job"


class QualityMetric(BaseModel):
    """Quality metric model for system evaluation"""

    __tablename__ = "quality_metrics"
    __table_args__ = (
        # z4a5b6c7d8e9_add_quality_metrics_measured_at (duplicates the
        # ix_quality_metrics_measured_at that measured_at's index=True makes).
        Index("idx_quality_metrics_measured_at", "measured_at"),
        # p3q4r5s6t7u8_add_dashboard_performance_indexes:
        Index(
            "idx_quality_metrics_org_created_type",
            "organization_id",
            "created_at",
            "metric_type",
        ),
        Index(
            "idx_quality_metrics_org_measured_type",
            "organization_id",
            "measured_at",
            "metric_type",
        ),
    )

    # Metric information
    # String, not Enum(MetricType): the search-analytics writer stores
    # free-form metric-type labels (response_time, result_count,
    # result_diversity, avg_relevance_score, freshness) that aren't members
    # of the MetricType enum, so a native PG enum column rejected them and
    # 500'd POST /metrics/search. The API response already types this as str.
    metric_type = Column(String(50), nullable=False, index=True)
    metric_name = Column(String(255), nullable=True, index=True)
    value = Column(Float, nullable=True)
    unit = Column(String(50), nullable=True)  # e.g., "ms", "percentage", "count"

    # Evaluation context
    evaluation_type = Column(Enum(EvaluationType), nullable=True)
    scope = Column(Enum(MetricScope), nullable=True, index=True)
    scope_id = Column(GUID(), nullable=True, index=True)  # ID of scoped entity

    # Evaluation details
    evaluation_parameters = Column(
        JSON, nullable=True
    )  # Parameters used for evaluation
    ground_truth = Column(JSON, nullable=True)  # Ground truth data if available
    predictions = Column(JSON, nullable=True)  # System predictions
    evaluation_metadata = Column(JSON, nullable=True)  # Additional evaluation metadata

    # Thresholds and targets
    threshold_min = Column(Float, nullable=True)  # Minimum acceptable value
    threshold_max = Column(Float, nullable=True)  # Maximum acceptable value
    target_value = Column(Float, nullable=True)  # Target value for optimization

    # Quality assessment
    quality_score = Column(Float, nullable=True)  # Overall quality score (0-1)
    passes_threshold = Column(Boolean, nullable=True)
    confidence_interval = Column(JSON, nullable=True)  # Statistical confidence interval

    # Temporal information
    evaluation_period_start = Column(DateTime(timezone=True), nullable=True)
    evaluation_period_end = Column(DateTime(timezone=True), nullable=True)
    sample_size = Column(Integer, nullable=True)  # Number of samples in evaluation

    # Organization
    organization_id = Column(GUID(), ForeignKey("organizations.id"), nullable=False)
    created_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)

    # Search-quality-metric fields (populated by
    # QualityMetricsService.collect_search_metrics / read by the
    # /analytics/quality endpoints). These duplicate value/unit/created_at with
    # search-specific naming the API contract expects; the older
    # evaluation-oriented columns above stay nullable so both row shapes coexist.
    metric_value = Column(Float, nullable=True)
    metric_unit = Column(String(50), nullable=True)
    query = Column(Text, nullable=True)
    search_type = Column(String(50), nullable=True)
    search_query_id = Column(GUID(), nullable=True, index=True)
    user_id = Column(GUID(), nullable=True, index=True)
    measured_at = Column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=True, index=True
    )
    is_threshold_violation = Column(Boolean, nullable=True, default=False)

    # Relationships
    organization = relationship("Organization")
    created_by_user = relationship("User")

    def __repr__(self):
        return (
            f"<QualityMetric(name={self.metric_name}, value={self.value}, "
            f"scope={self.scope.value if self.scope else None})>"
        )

    @property
    def is_within_threshold(self) -> bool:
        """Check if metric value is within acceptable thresholds"""
        if self.threshold_min is not None and self.value < self.threshold_min:
            return False
        if self.threshold_max is not None and self.value > self.threshold_max:
            return False
        return True

    @property
    def deviation_from_target(self) -> float:
        """Calculate deviation from target value"""
        if self.target_value is None:
            return 0.0
        return abs(self.value - self.target_value)

    @property
    def percentage_of_target(self) -> float:
        """Calculate percentage of target value achieved"""
        if self.target_value is None or self.target_value == 0:
            return 0.0
        return (self.value / self.target_value) * 100.0

    def assess_quality(self) -> float:
        """Assess overall quality score based on thresholds and targets"""
        if not self.is_within_threshold:
            return 0.0

        # Calculate quality based on proximity to target
        if self.target_value is not None:
            # Quality score based on how close to target
            deviation_ratio = (
                self.deviation_from_target / abs(self.target_value)
                if self.target_value != 0
                else 0
            )
            quality = max(0.0, 1.0 - deviation_ratio)
        else:
            # Quality score based on being within thresholds
            if self.threshold_min is not None and self.threshold_max is not None:
                range_size = self.threshold_max - self.threshold_min
                if range_size > 0:
                    # Center quality in the middle of acceptable range
                    center = (self.threshold_min + self.threshold_max) / 2
                    deviation = abs(self.value - center)
                    quality = max(0.0, 1.0 - (deviation / (range_size / 2)))
                else:
                    quality = 1.0
            else:
                quality = 1.0

        self.quality_score = min(1.0, max(0.0, quality))
        self.passes_threshold = self.is_within_threshold
        return self.quality_score

    def set_thresholds(
        self, min_value: float = None, max_value: float = None, target: float = None
    ):
        """Set threshold and target values"""
        if min_value is not None:
            self.threshold_min = min_value
        if max_value is not None:
            self.threshold_max = max_value
        if target is not None:
            self.target_value = target

    def add_evaluation_parameter(self, key: str, value):
        """Add evaluation parameter"""
        if not self.evaluation_parameters:
            self.evaluation_parameters = {}
        self.evaluation_parameters[key] = value

    def add_ground_truth(self, data: dict):
        """Add ground truth data"""
        if not self.ground_truth:
            self.ground_truth = {}
        self.ground_truth.update(data)

    def add_prediction(self, data: dict):
        """Add prediction data"""
        if not self.predictions:
            self.predictions = {}
        self.predictions.update(data)

    def set_confidence_interval(
        self, lower: float, upper: float, confidence: float = 0.95
    ):
        """Set statistical confidence interval"""
        self.confidence_interval = {
            "lower": lower,
            "upper": upper,
            "confidence": confidence,
        }

    def is_statistically_significant(self, alpha: float = 0.05) -> bool:
        """Check if result is statistically significant"""
        if not self.confidence_interval:
            return False
        confidence = self.confidence_interval.get("confidence", 0.95)
        return confidence >= (1 - alpha)

    def calculate_improvement(self, previous_metric) -> dict:
        """Calculate improvement compared to a previous metric"""
        if not previous_metric or previous_metric.metric_type != self.metric_type:
            return {
                "improvement": 0.0,
                "percentage_change": 0.0,
                "is_improvement": False,
            }

        change = self.value - previous_metric.value
        percentage_change = (
            (change / previous_metric.value * 100.0)
            if previous_metric.value != 0
            else 0.0
        )

        # Determine if this is an improvement based on metric type
        metrics_where_higher_is_better = [
            MetricType.PRECISION,
            MetricType.RECALL,
            MetricType.F1_SCORE,
            MetricType.ACCURACY,
            MetricType.RELEVANCE,
            MetricType.COHERENCE,
            MetricType.THROUGHPUT,
            MetricType.USER_SATISFACTION,
            MetricType.BUSINESS_IMPACT,
        ]

        is_improvement = (
            self.metric_type in metrics_where_higher_is_better and change > 0
        ) or (self.metric_type not in metrics_where_higher_is_better and change < 0)

        return {
            "improvement": change,
            "percentage_change": percentage_change,
            "is_improvement": is_improvement,
            "previous_value": previous_metric.value,
            "current_value": self.value,
        }

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data["metric_type"] = getattr(
            self.metric_type, "value", self.metric_type
        )  # metric_type is a plain str column now; tolerate a legacy enum too
        data["evaluation_type"] = (
            self.evaluation_type.value if self.evaluation_type else None
        )
        data["scope"] = self.scope.value if self.scope else None

        # Add computed fields
        data["is_within_threshold"] = self.is_within_threshold
        data["deviation_from_target"] = self.deviation_from_target
        data["percentage_of_target"] = self.percentage_of_target

        # Ensure quality score is calculated
        if self.quality_score is None:
            self.assess_quality()
        data["quality_score"] = self.quality_score

        return data

    @classmethod
    def get_metrics_by_type(
        cls, metric_type: MetricType, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get metrics by type"""
        # metric_type is a plain str column; accept an enum arg for back-compat.
        mt = getattr(metric_type, "value", metric_type)
        query = cls.query.filter(cls.metric_type == mt, cls.is_deleted == False)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def get_metrics_by_scope(
        cls,
        scope: MetricScope,
        scope_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
    ) -> list:
        """Get metrics by scope"""
        query = cls.query.filter(cls.scope == scope, cls.is_deleted == False)
        if scope_id:
            query = query.filter(cls.scope_id == scope_id)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def get_failed_metrics(cls, organization_id: Optional[uuid.UUID] = None) -> list:
        """Get metrics that don't meet thresholds"""
        query = cls.query.filter(cls.passes_threshold == False, cls.is_deleted == False)
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def get_metrics_in_period(
        cls,
        start_date: datetime,
        end_date: datetime,
        organization_id: Optional[uuid.UUID] = None,
    ) -> list:
        """Get metrics within a time period"""
        query = cls.query.filter(
            cls.evaluation_period_start >= start_date,
            cls.evaluation_period_end <= end_date,
            cls.is_deleted == False,
        )
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def calculate_aggregate_metrics(
        cls,
        metric_type: MetricType,
        organization_id: Optional[uuid.UUID] = None,
        period_days: int = 30,
    ) -> dict:
        """Calculate aggregate statistics for a metric type"""
        from datetime import timedelta

        from sqlalchemy import func

        cutoff_date = datetime.utcnow() - timedelta(days=period_days)

        query = cls.session.query(
            func.avg(cls.value).label("average"),
            func.min(cls.value).label("minimum"),
            func.max(cls.value).label("maximum"),
            func.count(cls.id).label("count"),
        ).filter(
            cls.metric_type == metric_type,
            cls.created_at >= cutoff_date,
            cls.is_deleted == False,
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        result = query.first()

        return {
            "metric_type": getattr(metric_type, "value", metric_type),
            "period_days": period_days,
            "average": float(result.average) if result.average else 0.0,
            "minimum": float(result.minimum) if result.minimum else 0.0,
            "maximum": float(result.maximum) if result.maximum else 0.0,
            "count": int(result.count) if result.count else 0,
        }
