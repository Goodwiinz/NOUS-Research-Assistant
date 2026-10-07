"""
Pydantic schemas for quality metrics and analytics
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QualityMetricResponse(BaseModel):
    """Quality metric response"""

    id: str
    metric_type: str
    metric_value: Optional[float] = None
    metric_unit: Optional[str] = None
    query: Optional[str] = None
    search_type: Optional[str] = None
    measured_at: Optional[str] = None
    is_threshold_violation: bool = False
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class QualityAlertResponse(BaseModel):
    """Quality alert response"""

    id: str
    metric_type: Optional[str] = None
    severity: str
    title: str
    message: str
    status: str
    created_at: str
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None

    class Config:
        from_attributes = True


class MetricAggregationResponse(BaseModel):
    """Metric aggregation response"""

    id: str
    metric_type: str
    aggregation_type: str
    period_start: str
    period_end: str
    avg_value: float
    min_value: float
    max_value: float
    count_values: int
    percentiles: Optional[Dict[str, float]] = None

    class Config:
        from_attributes = True


class SearchAnalyticsResponse(BaseModel):
    """Search analytics response"""

    period: Dict[str, str]
    total_searches: int
    avg_response_time_ms: float
    unique_users: int
    top_queries: List[Dict[str, Any]]
    search_types: List[Dict[str, Any]]


class QueryAnalytics(BaseModel):
    """Query analytics data"""

    query: str
    count: int
    avg_response_time: float
    avg_results: float
    click_rate: float
    user_satisfaction: Optional[float] = None


class UserBehaviorAnalytics(BaseModel):
    """User behavior analytics"""

    user_id: str
    session_count: int
    total_searches: int
    avg_session_duration: float
    avg_response_time: float
    preferred_search_types: List[str]
    top_queries: List[str]
    last_active: datetime


class ContentUsageAnalytics(BaseModel):
    """Content usage analytics"""

    document_id: str
    title: str
    access_count: int
    search_appearances: int
    click_through_rate: float
    avg_relevance_score: float
    last_accessed: datetime


class PerformanceMetrics(BaseModel):
    """System performance metrics"""

    cpu_usage: float
    memory_usage: float
    disk_usage: float
    network_io: float
    response_time_p50: float
    response_time_p95: float
    error_rate: float
    timestamp: datetime


class QualityThresholdCreate(BaseModel):
    """Quality threshold creation request"""

    metric_type: str = Field(..., description="Type of metric")
    threshold_min: Optional[float] = Field(None, description="Minimum threshold value")
    threshold_max: Optional[float] = Field(None, description="Maximum threshold value")
    threshold_target: Optional[float] = Field(
        None, description="Target threshold value"
    )
    alert_severity: str = Field("medium", description="Alert severity level")
    is_enabled: bool = Field(True, description="Whether threshold is enabled")
    alert_cooldown_minutes: int = Field(60, description="Alert cooldown in minutes")
    search_type: Optional[str] = Field(None, description="Specific search type")
    description: Optional[str] = Field(None, description="Threshold description")


class QualityThresholdResponse(BaseModel):
    """Quality threshold response"""

    id: str
    metric_type: str
    threshold_min: Optional[float] = None
    threshold_max: Optional[float] = None
    threshold_target: Optional[float] = None
    alert_severity: str
    is_enabled: bool
    alert_cooldown_minutes: int
    search_type: Optional[str] = None
    description: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class AlertAcknowledgmentRequest(BaseModel):
    """Alert acknowledgment request"""

    note: Optional[str] = Field(None, description="Optional acknowledgment note")


class SearchEventCreate(BaseModel):
    """Search event creation request"""

    session_id: str
    query: str
    search_type: str
    results_count: int
    response_time: float
    user_id: Optional[str] = None
    search_query_id: Optional[str] = None
    page_number: int = Field(1, ge=1)
    filters_applied: Optional[Dict[str, Any]] = None
    sort_order: Optional[str] = None


class SearchEventUpdate(BaseModel):
    """Search event update request"""

    clicked_results: int = Field(0, ge=0)
    clicked_result_ids: Optional[List[str]] = None
    time_to_first_click: Optional[float] = Field(None, ge=0)
    dwell_time: Optional[float] = Field(None, ge=0)
    user_rating: Optional[int] = Field(None, ge=1, le=5)
    feedback_text: Optional[str] = None
    is_bookmarked: bool = False


class SessionCreate(BaseModel):
    """Search session creation request"""

    session_id: str
    user_id: Optional[str] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    referrer: Optional[str] = None


class SessionResponse(BaseModel):
    """Search session response"""

    id: str
    session_id: str
    user_id: Optional[str] = None
    organization_id: str
    start_time: str
    end_time: Optional[str] = None
    search_count: int
    total_response_time: float
    avg_response_time: Optional[float] = None
    session_duration: Optional[float] = None
    bounce_rate: bool
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    referrer: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class QualityReportRequest(BaseModel):
    """Quality report generation request"""

    report_type: str = Field(..., description="Type of report to generate")
    start_date: datetime = Field(..., description="Report start date")
    end_date: datetime = Field(..., description="Report end date")
    metric_types: Optional[List[str]] = Field(
        None, description="Specific metrics to include"
    )
    include_alerts: bool = Field(True, description="Include alerts in report")
    include_recommendations: bool = Field(
        True, description="Include improvement recommendations"
    )
    format: str = Field("json", description="Report format (json, pdf, csv)")


class QualityReportResponse(BaseModel):
    """Quality report response"""

    report_id: str
    report_type: str
    period: Dict[str, str]
    generated_at: str
    summary: Dict[str, Any]
    metrics: List[Dict[str, Any]]
    alerts: List[Dict[str, Any]]
    recommendations: List[Dict[str, Any]]
    download_url: Optional[str] = None


class RecommendationItem(BaseModel):
    """Quality improvement recommendation"""

    id: str
    category: str
    priority: str
    title: str
    description: str
    impact_assessment: str
    effort_required: str
    actionable_steps: List[str]
    expected_outcome: str
    due_date: Optional[datetime] = None
    status: str = "pending"
    created_at: str


class DashboardWidget(BaseModel):
    """Dashboard widget configuration"""

    id: str
    widget_type: str
    title: str
    metrics: List[str]
    time_range: str
    visualization_type: str
    position: Dict[str, int]
    size: Dict[str, int]
    config: Optional[Dict[str, Any]] = None


class DashboardConfiguration(BaseModel):
    """Dashboard configuration"""

    id: str
    name: str
    description: Optional[str] = None
    widgets: List[DashboardWidget]
    default_time_range: str = "7d"
    refresh_interval: int = 300  # seconds
    is_public: bool = False
    created_by: str
    created_at: str
    updated_at: str


class MetricThresholdViolation(BaseModel):
    """Metric threshold violation notification"""

    metric_id: str
    metric_type: str
    current_value: float
    threshold_value: float
    violation_type: str  # "above" or "below"
    severity: str
    query: str
    organization_id: str
    timestamp: datetime


class SystemHealthCheck(BaseModel):
    """System health check response"""

    status: str
    service: str
    timestamp: str
    version: Optional[str] = None
    uptime: Optional[float] = None
    dependencies: Dict[str, str]
    features: Dict[str, bool]
    metrics: Dict[str, Any]


class AnalyticsExportRequest(BaseModel):
    """Analytics data export request"""

    data_type: str = Field(..., description="Type of data to export")
    start_date: datetime = Field(..., description="Export start date")
    end_date: datetime = Field(..., description="Export end date")
    format: str = Field("csv", description="Export format")
    filters: Optional[Dict[str, Any]] = Field(None, description="Export filters")
    include_metadata: bool = Field(True, description="Include metadata in export")


class BehaviorReportRequest(BaseModel):
    """Body for POST /api/v1/analytics/behavior/reports/generate.

    ``user_id`` (optional) adds a per-user section to the org report; the route
    rejects it unless the target user belongs to the caller's organization.
    Unknown keys are ignored (pydantic default) for backward compatibility with
    the previous untyped ``Dict[str, Any]`` body.
    """

    user_id: Optional[uuid.UUID] = Field(
        None, description="Target user (must be in the caller's organization)"
    )
    days_back: int = Field(30, ge=1, le=365, description="Days of history to analyze")
    report_type: str = Field(
        "organization", max_length=64, description="Label echoed back in the report"
    )


class AnalyticsExportResponse(BaseModel):
    """Analytics export response"""

    export_id: str
    status: str
    download_url: Optional[str] = None
    file_size: Optional[int] = None
    expires_at: Optional[datetime] = None
    created_at: str
