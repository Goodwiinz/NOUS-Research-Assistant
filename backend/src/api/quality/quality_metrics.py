"""
Quality metrics and analytics API endpoints
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.quality import QualityMetric
from src.models.quality_metrics import MetricAggregation, QualityAlert, SearchEvent
from src.models.user import User
from src.schemas.quality_metrics import (
    AlertAcknowledgmentRequest,
    MetricAggregationResponse,
    QualityAlertResponse,
    QualityMetricResponse,
    QualityThresholdCreate,
    QualityThresholdResponse,
    SearchAnalyticsResponse,
)
from src.services.quality.quality_metrics_service import quality_metrics_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/metrics/search", response_model=List[QualityMetricResponse])
async def record_search_metrics(
    search_response_data: Dict[str, Any],
    search_query: str,
    search_type: str,
    session_id: str,
    user_id: Optional[str] = None,
    query_id: Optional[str] = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Record quality metrics for a search query
    """
    try:
        # Convert dict to SearchResponse object (simplified)
        import uuid

        from src.models.search_schemas import SearchResponse, SearchResult

        # This is a simplified conversion - in practice, you'd parse the full response
        search_response = SearchResponse(
            query=search_query,
            search_id=str(uuid.uuid4()),
            results=[],  # Would be populated from actual search results
            total_results=search_response_data.get("total_results", 0),
            query_time_ms=search_response_data.get("query_time_ms", 0),
            search_type=search_type,
        )

        # Track search session
        await quality_metrics_service.track_search_session(
            session_id=session_id,
            user_id=user_id or str(current_user.id),
            organization_id=str(current_user.organization_id),
        )

        # Record search event
        await quality_metrics_service.record_search_event(
            session_id=session_id,
            query=search_query,
            search_type=search_type,
            results_count=search_response.total_results,
            response_time=search_response.query_time_ms,
            user_id=user_id or str(current_user.id),
            organization_id=str(current_user.organization_id),
            search_query_id=query_id,
        )

        # Collect quality metrics
        metrics = await quality_metrics_service.collect_search_metrics(
            search_response=search_response,
            search_query=search_query,
            search_type=search_type,
            user_id=user_id or str(current_user.id),
            organization_id=str(current_user.organization_id),
            query_id=query_id,
        )

        return [
            QualityMetricResponse(
                id=str(metric.id),
                metric_type=metric.metric_type,
                metric_value=metric.metric_value,
                metric_unit=metric.metric_unit,
                query=metric.query,
                search_type=metric.search_type,
                measured_at=(
                    metric.measured_at.isoformat() if metric.measured_at else None
                ),
                is_threshold_violation=metric.is_threshold_violation,
                metadata=metric.evaluation_metadata,
            )
            for metric in metrics
        ]

    except ValueError:
        # track_search_session raises ValueError when the client-supplied
        # session_id belongs to another organization — a client conflict, not a
        # server fault. Surface 409 so legitimate cross-tenant rejections don't
        # page as 5xx.
        logger.warning("Rejected search-metrics record", exc_info=True)
        raise HTTPException(status_code=409, detail="Search metrics record rejected")
    except Exception as e:
        logger.error(f"Error recording search metrics: {e}")
        raise HTTPException(status_code=500, detail="Failed to record search metrics")


@router.get("/metrics", response_model=List[QualityMetricResponse])
async def get_quality_metrics(
    metric_types: Optional[List[str]] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    limit: int = Query(1000, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get quality metrics for the organization
    """
    try:
        # Default to last 24 hours if no time range specified
        if not end_time:
            end_time = datetime.utcnow()
        if not start_time:
            start_time = end_time - timedelta(hours=24)

        metrics = quality_metrics_service.get_quality_metrics(
            organization_id=str(current_user.organization_id),
            metric_types=metric_types,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        return [
            QualityMetricResponse(
                id=str(metric.id),
                metric_type=metric.metric_type,
                metric_value=metric.metric_value,
                metric_unit=metric.metric_unit,
                query=metric.query,
                search_type=metric.search_type,
                measured_at=(
                    metric.measured_at.isoformat() if metric.measured_at else None
                ),
                is_threshold_violation=metric.is_threshold_violation,
                metadata=metric.evaluation_metadata,
            )
            for metric in metrics
        ]

    except Exception as e:
        logger.error(f"Error getting quality metrics: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve quality metrics"
        )


@router.get("/metrics/aggregations", response_model=List[MetricAggregationResponse])
async def get_metric_aggregations(
    metric_types: Optional[List[str]] = Query(None),
    aggregation_type: str = Query("hourly", pattern="^(hourly|daily|weekly|monthly)$"),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get aggregated quality metrics for dashboard
    """
    try:
        # Default to last 7 days if no time range specified
        if not end_time:
            end_time = datetime.utcnow()
        if not start_time:
            start_time = end_time - timedelta(days=7)

        aggregations = quality_metrics_service.get_metric_aggregations(
            organization_id=str(current_user.organization_id),
            metric_types=metric_types,
            aggregation_type=aggregation_type,
            start_time=start_time,
            end_time=end_time,
        )

        return [
            MetricAggregationResponse(
                id=str(agg.id),
                metric_type=agg.metric_type,
                aggregation_type=agg.aggregation_type,
                period_start=agg.aggregation_period_start.isoformat(),
                period_end=agg.aggregation_period_end.isoformat(),
                avg_value=agg.avg_value,
                min_value=agg.min_value,
                max_value=agg.max_value,
                count_values=agg.count_values,
                percentiles=agg.percentiles,
            )
            for agg in aggregations
        ]

    except Exception as e:
        logger.error(f"Error getting metric aggregations: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve metric aggregations"
        )


@router.get("/alerts", response_model=List[QualityAlertResponse])
async def get_quality_alerts(
    severity: Optional[str] = Query(None, pattern="^(low|medium|high|critical)$"),
    status: Optional[str] = Query("active", pattern="^(active|acknowledged|resolved)$"),
    limit: int = Query(100, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get quality alerts for the organization
    """
    try:
        alerts = quality_metrics_service.get_active_alerts(
            organization_id=str(current_user.organization_id), severity=severity
        )

        # Filter by status if specified
        if status != "active":
            alerts = [alert for alert in alerts if alert.status == status]

        # Limit results
        alerts = alerts[:limit]

        return [
            QualityAlertResponse(
                id=str(alert.id),
                metric_type=alert.metric.metric_type if alert.metric else None,
                severity=alert.severity,
                title=alert.title,
                message=alert.message,
                status=alert.status,
                created_at=alert.created_at.isoformat(),
                acknowledged_at=(
                    alert.acknowledged_at.isoformat() if alert.acknowledged_at else None
                ),
                resolved_at=(
                    alert.resolved_at.isoformat() if alert.resolved_at else None
                ),
            )
            for alert in alerts
        ]

    except Exception as e:
        logger.error(f"Error getting quality alerts: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve quality alerts")


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    request: AlertAcknowledgmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Acknowledge a quality alert
    """
    try:
        success = quality_metrics_service.acknowledge_alert(
            alert_id=alert_id,
            acknowledged_by=str(current_user.id),
            organization_id=str(current_user.organization_id),
        )

        if success:
            return {"message": "Alert acknowledged successfully"}
        else:
            raise HTTPException(status_code=404, detail="Alert not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error acknowledging alert: {e}")
        raise HTTPException(status_code=500, detail="Failed to acknowledge alert")


@router.get("/analytics/search", response_model=SearchAnalyticsResponse)
async def get_search_analytics(
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get search analytics summary
    """
    try:
        # Default to last 7 days if no time range specified
        if not end_time:
            end_time = datetime.utcnow()
        if not start_time:
            start_time = end_time - timedelta(days=7)

        analytics = quality_metrics_service.get_search_analytics(
            organization_id=str(current_user.organization_id),
            start_time=start_time,
            end_time=end_time,
        )

        return SearchAnalyticsResponse(**analytics)

    except Exception as e:
        logger.error(f"Error getting search analytics: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve search analytics"
        )


@router.get("/dashboard")
@router.get("/analytics/dashboard")
async def get_dashboard_data(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """
    Get comprehensive dashboard data (accessible via /dashboard or /analytics/dashboard)
    """
    try:
        # Time ranges for different views
        now = datetime.utcnow()
        last_24h = now - timedelta(hours=24)
        last_7d = now - timedelta(days=7)
        last_30d = now - timedelta(days=30)

        # Get metrics for different time periods
        metrics_24h = quality_metrics_service.get_quality_metrics(
            organization_id=str(current_user.organization_id),
            start_time=last_24h,
            end_time=now,
            limit=1000,
        )

        metrics_7d = quality_metrics_service.get_quality_metrics(
            organization_id=str(current_user.organization_id),
            start_time=last_7d,
            end_time=now,
            limit=5000,
        )

        # Get aggregations
        hourly_aggregations = quality_metrics_service.get_metric_aggregations(
            organization_id=str(current_user.organization_id),
            aggregation_type="hourly",
            start_time=last_24h,
            end_time=now,
        )

        # Get alerts
        active_alerts = quality_metrics_service.get_active_alerts(
            organization_id=str(current_user.organization_id)
        )

        # Get search analytics
        search_analytics = quality_metrics_service.get_search_analytics(
            organization_id=str(current_user.organization_id),
            start_time=last_7d,
            end_time=now,
        )

        # Calculate summary statistics
        avg_response_time_24h = [
            m.metric_value for m in metrics_24h if m.metric_type == "response_time"
        ]
        avg_response_time_24h = (
            sum(avg_response_time_24h) / len(avg_response_time_24h)
            if avg_response_time_24h
            else 0
        )

        # Prepare dashboard data
        dashboard_data = {
            "summary": {
                "total_searches_24h": len(
                    [m for m in metrics_24h if m.metric_type == "response_time"]
                ),
                "avg_response_time_24h_ms": avg_response_time_24h,
                "active_alerts": len(active_alerts),
                "avg_relevance_score": 0,  # Would calculate from relevance metrics
            },
            "metrics": {
                "last_24h": [
                    {
                        "id": str(m.id),
                        "type": m.metric_type,
                        "value": m.metric_value,
                        "unit": m.metric_unit,
                        "timestamp": m.measured_at.isoformat(),
                        "is_violation": m.is_threshold_violation,
                    }
                    for m in metrics_24h
                ],
                "aggregations": [
                    {
                        "id": str(agg.id),
                        "type": agg.metric_type,
                        "period": agg.aggregation_period_start.isoformat(),
                        "avg": agg.avg_value,
                        "min": agg.min_value,
                        "max": agg.max_value,
                        "count": agg.count_values,
                    }
                    for agg in hourly_aggregations
                ],
            },
            "alerts": [
                {
                    "id": str(alert.id),
                    "severity": alert.severity,
                    "title": alert.title,
                    "message": alert.message,
                    "created_at": alert.created_at.isoformat(),
                    "status": alert.status,
                }
                for alert in active_alerts[:10]  # Limit to recent alerts
            ],
            "search_analytics": search_analytics,
            "time_ranges": {
                "last_24h": {"start": last_24h.isoformat(), "end": now.isoformat()},
                "last_7d": {"start": last_7d.isoformat(), "end": now.isoformat()},
                "last_30d": {"start": last_30d.isoformat(), "end": now.isoformat()},
            },
        }

        return dashboard_data

    except Exception as e:
        logger.error(f"Error getting dashboard data: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve dashboard data")


@router.get("/health")
async def health_check():
    """
    Health check for quality metrics service
    """
    try:
        # Basic health checks
        return {
            "status": "healthy",
            "service": "quality_metrics",
            "timestamp": datetime.utcnow().isoformat(),
            "features": {
                "metrics_collection": True,
                "alerting": True,
                "aggregations": True,
                "analytics": True,
            },
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "quality_metrics",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )


@router.get("/analytics/trends")
async def get_quality_trends(
    metric_type: str = Query(...),
    period: str = Query("7d", pattern="^(24h|7d|30d)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get quality trends for a specific metric
    """
    try:
        # Calculate time range
        now = datetime.utcnow()
        if period == "24h":
            start_time = now - timedelta(hours=24)
            aggregation_type = "hourly"
        elif period == "7d":
            start_time = now - timedelta(days=7)
            aggregation_type = "daily"
        else:  # 30d
            start_time = now - timedelta(days=30)
            aggregation_type = "weekly"

        # Get aggregations
        aggregations = quality_metrics_service.get_metric_aggregations(
            organization_id=str(current_user.organization_id),
            metric_types=[metric_type],
            aggregation_type=aggregation_type,
            start_time=start_time,
            end_time=now,
        )

        # Prepare trend data
        trend_data = [
            {
                "timestamp": agg.aggregation_period_start.isoformat(),
                "value": agg.avg_value,
                "min": agg.min_value,
                "max": agg.max_value,
                "count": agg.count_values,
            }
            for agg in aggregations
        ]

        return {
            "metric_type": metric_type,
            "period": period,
            "data": trend_data,
            "summary": {
                "current_value": trend_data[-1]["value"] if trend_data else None,
                "trend_direction": (
                    "up"
                    if len(trend_data) > 1
                    and trend_data[-1]["value"] > trend_data[0]["value"]
                    else "down"
                ),
                "data_points": len(trend_data),
            },
        }

    except Exception as e:
        logger.error(f"Error getting quality trends: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve quality trends")
