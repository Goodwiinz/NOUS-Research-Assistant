"""
User Behavior Analytics API endpoints
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_db, get_db_sync
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.quality_metrics import (
    AnalyticsExportRequest,
    AnalyticsExportResponse,
    BehaviorReportRequest,
    ContentUsageAnalytics,
    UserBehaviorAnalytics,
)
from src.services.quality.user_behavior_service import user_behavior_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/sessions", response_model=dict)
def create_search_session(
    session_data: Dict[str, Any],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """Create a new search session for tracking"""
    try:
        from src.models.quality_metrics import SearchSession

        # Check if session already exists
        existing_session = (
            db.query(SearchSession)
            .filter(SearchSession.session_id == session_data["session_id"])
            .first()
        )

        if existing_session:
            return {"session_id": existing_session.session_id, "status": "existing"}

        # Create new session
        session = SearchSession(
            session_id=session_data["session_id"],
            user_id=current_user.id,
            organization_id=current_user.organization_id,
            user_agent=session_data.get("user_agent"),
            ip_address=session_data.get("ip_address"),
            referrer=session_data.get("referrer"),
        )

        db.add(session)
        db.commit()
        db.refresh(session)

        logger.info(f"Created search session: {session.session_id}")

        return {
            "session_id": session.session_id,
            "status": "created",
            "created_at": session.created_at.isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to create search session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/events")
async def track_search_event(
    event_data: Dict[str, Any], current_user: User = Depends(get_current_user)
):
    """Track a search event for behavior analysis"""
    try:
        event = await user_behavior_service.track_search_event(
            session_id=event_data["session_id"],
            query=event_data["query"],
            search_type=event_data["search_type"],
            results_count=event_data["results_count"],
            response_time=event_data["response_time"],
            user_id=str(current_user.id),
            search_query_id=event_data.get("search_query_id"),
            page_number=event_data.get("page_number", 1),
            filters_applied=event_data.get("filters_applied"),
            sort_order=event_data.get("sort_order"),
        )

        return {
            "event_id": str(event.id),
            "session_id": event_data["session_id"],
            "status": "tracked",
        }

    except Exception as e:
        logger.error(f"Failed to track search event: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/interactions")
async def track_user_interaction(
    interaction_data: Dict[str, Any], current_user: User = Depends(get_current_user)
):
    """Track user interaction with search results"""
    try:
        success = await user_behavior_service.track_user_interaction(
            session_id=interaction_data["session_id"],
            event_id=interaction_data["event_id"],
            interaction_type=interaction_data["interaction_type"],
            data=interaction_data.get("data", {}),
            organization_id=str(current_user.organization_id),
        )

        if success:
            return {
                "status": "tracked",
                "interaction_type": interaction_data["interaction_type"],
            }
        else:
            return {"status": "failed", "message": "Event not found"}

    except Exception as e:
        logger.error(f"Failed to track user interaction: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/my-behavior", response_model=UserBehaviorAnalytics)
async def get_my_behavior_analytics(
    days_back: int = Query(30, ge=1, le=365, description="Days of history to analyze"),
    current_user: User = Depends(get_current_user),
):
    """Get behavior analytics for the current user"""
    try:
        metrics = await user_behavior_service.analyze_user_behavior(
            user_id=str(current_user.id),
            days_back=days_back,
            organization_id=(
                str(current_user.organization_id)
                if current_user.organization_id
                else None
            ),
        )

        return UserBehaviorAnalytics(
            user_id=str(current_user.id),
            session_count=metrics.session_count,
            total_searches=metrics.total_searches,
            avg_session_duration=metrics.avg_session_duration,
            avg_response_time=metrics.avg_response_time,
            preferred_search_types=metrics.preferred_search_types,
            top_queries=metrics.top_queries,
            last_active=metrics.last_active,
        )

    except Exception as e:
        logger.error(f"Failed to get user behavior analytics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/users/{user_id}/behavior", response_model=UserBehaviorAnalytics)
async def get_user_behavior_analytics(
    user_id: str,
    days_back: int = Query(30, ge=1, le=365, description="Days of history to analyze"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """Get behavior analytics for a specific user (admin/org manager only)"""
    try:
        # Tenant scope: the target user MUST belong to the caller's organization —
        # for EVERY role. UserRole.ADMIN is a per-organization role (role and
        # organization_id are independent columns; there is no global superuser),
        # so an org-A admin must not read an org-B user's behavior. The service
        # filters only by user_id, so the org boundary is enforced here.
        from src.models.user import User as UserModel

        # Reject when the caller has no org, or the target is missing / in a
        # different org. Guard against None==None: a null-org caller must not
        # read other null-org users' behavior.
        target_user = db.query(UserModel).filter(UserModel.id == user_id).first()
        if (
            not current_user.organization_id
            or not target_user
            or target_user.organization_id != current_user.organization_id
        ):
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        # Intra-tenant horizontal authz (mirrors get_session_analysis below):
        # within the caller's org, only an admin may read ANOTHER user's
        # behavior analytics; a non-admin may read only their own. The org
        # check above stops cross-tenant reads; this stops same-org peer reads.
        if str(current_user.id) != user_id and current_user.role.value != "admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        metrics = await user_behavior_service.analyze_user_behavior(
            user_id=user_id,
            days_back=days_back,
            organization_id=str(current_user.organization_id),
        )

        return UserBehaviorAnalytics(
            user_id=metrics.user_id,
            session_count=metrics.session_count,
            total_searches=metrics.total_searches,
            avg_session_duration=metrics.avg_session_duration,
            avg_response_time=metrics.avg_response_time,
            preferred_search_types=metrics.preferred_search_types,
            top_queries=metrics.top_queries,
            last_active=metrics.last_active,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get user behavior analytics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sessions/{session_id}/analysis")
async def get_session_analysis(
    session_id: str, current_user: User = Depends(get_current_user)
):
    """Get detailed analysis of a specific search session"""
    try:
        # Org-scoped: analyze_session only resolves a session in the caller's
        # own organization, so a foreign session_id raises not-found (404 below)
        # rather than leaking another tenant's session — the admin role no
        # longer crosses the org boundary.
        try:
            analysis = await user_behavior_service.analyze_session(
                session_id, organization_id=str(current_user.organization_id)
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="Session not found")

        # Within the caller's org, a non-admin may only read their own session.
        if (
            analysis.user_id
            and str(current_user.id) != analysis.user_id
            and current_user.role.value not in ["admin"]
        ):
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        return {
            "session_id": analysis.session_id,
            "user_id": analysis.user_id,
            "duration": analysis.duration,
            "search_count": analysis.search_count,
            "avg_response_time": analysis.avg_response_time,
            "clicked_results": analysis.clicked_results,
            "total_results_viewed": analysis.total_results_viewed,
            "queries": analysis.queries,
            "search_types": analysis.search_types,
            "bounce_rate": analysis.bounce_rate,
            "task_completion_rate": analysis.task_completion_rate,
            "satisfaction_indicators": analysis.satisfaction_indicators,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session analysis: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/organization/trends")
async def get_organization_behavior_trends(
    days_back: int = Query(30, ge=1, le=365, description="Days of history to analyze"),
    group_by: str = Query(
        "day", pattern="^(day|week|month)$", description="Grouping period"
    ),
    current_user: User = Depends(get_current_user),
):
    """Get behavior trends for the organization"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        trends = await user_behavior_service.get_behavior_trends(
            organization_id=str(current_user.organization_id),
            days_back=days_back,
            group_by=group_by,
        )

        return trends

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get organization behavior trends: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/insights")
async def get_behavioral_insights(
    days_back: int = Query(30, ge=1, le=365, description="Days of history to analyze"),
    current_user: User = Depends(get_current_user),
):
    """Get behavioral insights for the organization"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        # Get behavioral insights combining multiple data sources
        insights = await user_behavior_service.get_behavioral_insights(
            organization_id=str(current_user.organization_id), days_back=days_back
        )

        # Format response to match notebook expectations
        return {
            "user_engagement": {
                "active_users": insights.get("active_users", 0),
                "total_sessions": insights.get("total_sessions", 0),
                "avg_session_duration": insights.get("avg_session_duration", 0),
                "engagement_trend": insights.get("engagement_trend", "stable"),
                "retention_rate": insights.get("retention_rate", 0),
            },
            "search_patterns": {
                "top_queries": insights.get("top_queries", []),
                "search_types_distribution": insights.get(
                    "search_types_distribution", {}
                ),
                "peak_usage_hours": insights.get("peak_usage_hours", []),
                "avg_queries_per_session": insights.get("avg_queries_per_session", 0),
            },
            "content_interactions": {
                "most_accessed_documents": insights.get("most_accessed_documents", []),
                "click_through_rates": insights.get("click_through_rates", {}),
                "user_satisfaction": insights.get("user_satisfaction", 0),
                "task_completion_rate": insights.get("task_completion_rate", 0),
            },
            "behavioral_segments": insights.get("behavioral_segments", []),
            "recommendations": insights.get("recommendations", []),
            "period_analyzed": f"Last {days_back} days",
            "timestamp": datetime.utcnow().isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get behavioral insights: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/organization/patterns")
async def get_organization_behavior_patterns(
    min_sessions: int = Query(5, ge=1, description="Minimum sessions per user"),
    current_user: User = Depends(get_current_user),
):
    """Identify user behavior patterns in the organization"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        patterns = await user_behavior_service.identify_behavior_patterns(
            organization_id=str(current_user.organization_id), min_sessions=min_sessions
        )

        return patterns

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to identify behavior patterns: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/organization/users")
async def get_organization_user_behavior(
    limit: int = Query(50, ge=1, le=200, description="Maximum users to return"),
    sort_by: str = Query(
        "engagement_score",
        pattern="^(engagement_score|session_count|total_searches|last_active)$",
    ),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """Get behavior analytics for all users in the organization"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        # Get all users in the organization

        users = db.execute(
            text("""
            SELECT DISTINCT u.id
            FROM users u
            WHERE u.organization_id = :org_id
            AND EXISTS (
                SELECT 1 FROM search_sessions s
                WHERE s.user_id = u.id
                AND s.start_time >= :cutoff_date
            )
            ORDER BY u.created_at DESC
            LIMIT :limit
        """),
            {
                "org_id": current_user.organization_id,
                "cutoff_date": datetime.utcnow() - timedelta(days=90),
                "limit": limit * 2,  # Get more to filter
            },
        ).fetchall()

        user_analytics = []

        for user in users:
            try:
                metrics = await user_behavior_service.analyze_user_behavior(
                    user_id=str(user.id),
                    days_back=90,
                    use_cache=True,
                    organization_id=str(current_user.organization_id),
                )

                if metrics.session_count > 0:  # Only include active users
                    user_analytics.append(
                        {
                            "user_id": metrics.user_id,
                            "behavior_pattern": metrics.behavior_pattern.value,
                            "session_count": metrics.session_count,
                            "total_searches": metrics.total_searches,
                            "avg_session_duration": metrics.avg_session_duration,
                            "avg_response_time": metrics.avg_response_time,
                            "success_rate": metrics.success_rate,
                            "engagement_score": metrics.engagement_score,
                            "satisfaction_score": metrics.satisfaction_score,
                            "preferred_search_types": metrics.preferred_search_types,
                            "top_queries": metrics.top_queries,
                            "last_active": metrics.last_active.isoformat(),
                        }
                    )
            except Exception as e:
                logger.warning(f"Failed to analyze user {user.id}: {e}")
                continue

        # Sort results
        reverse_order = order == "desc"
        user_analytics.sort(key=lambda x: x.get(sort_by, 0), reverse=reverse_order)

        # Apply limit
        user_analytics = user_analytics[:limit]

        return {
            "users": user_analytics,
            "total_count": len(user_analytics),
            "sort_by": sort_by,
            "order": order,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get organization user behavior: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/content-usage")
async def get_content_usage_analytics(
    days_back: int = Query(30, ge=1, le=365, description="Days of history to analyze"),
    limit: int = Query(50, ge=1, le=200, description="Maximum documents to return"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """Get content usage analytics for the organization"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        # Query content usage from search events
        content_usage = db.execute(
            text("""
            SELECT
                d.id as document_id,
                d.title,
                COUNT(DISTINCT s.user_id) as unique_users,
                COUNT(DISTINCT se.session_id) as search_appearances,
                COUNT(CASE WHEN se.clicked_results > 0 THEN 1 END) as total_clicks,
                AVG(se.results_count) as avg_results_in_search,
                MAX(se.created_at) as last_accessed
            FROM documents d
            JOIN search_results sr ON d.id = sr.document_id
            JOIN search_queries sq ON sr.search_query_id = sq.id
            JOIN search_events se ON sq.id = se.search_query_id
            JOIN search_sessions s ON se.session_id = s.id
            WHERE d.organization_id = :org_id
                AND se.created_at >= :cutoff_date
            GROUP BY d.id, d.title
            HAVING COUNT(DISTINCT s.user_id) > 0
            ORDER BY unique_users DESC, total_clicks DESC
            LIMIT :limit
        """),
            {
                "org_id": current_user.organization_id,
                "cutoff_date": datetime.utcnow() - timedelta(days=days_back),
                "limit": limit,
            },
        ).fetchall()

        content_analytics = []

        for content in content_usage:
            # Calculate click-through rate
            ctr = (
                content.total_clicks / content.search_appearances
                if content.search_appearances > 0
                else 0
            )

            content_analytics.append(
                {
                    "document_id": str(content.document_id),
                    "title": content.title,
                    "access_count": content.unique_users,
                    "search_appearances": content.search_appearances,
                    "total_clicks": content.total_clicks,
                    "click_through_rate": float(ctr),
                    "avg_results_in_search": float(content.avg_results_in_search or 0),
                    "last_accessed": (
                        content.last_accessed.isoformat()
                        if content.last_accessed
                        else None
                    ),
                }
            )

        return {
            "content_usage": content_analytics,
            "period_days": days_back,
            "total_documents": len(content_analytics),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get content usage analytics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/reports/generate")
async def generate_behavior_report(
    report_request: BehaviorReportRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """Generate comprehensive behavior analytics report"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        user_id: Optional[str] = None
        if report_request.user_id is not None:
            # Tenant scope (mirrors get_user_behavior_analytics): UserRole.ADMIN
            # is per-organization, so the body-supplied target user MUST belong
            # to the caller's org. Reject a null-org caller (None == None) and a
            # missing target with the same 403 so this is not an existence oracle.
            from src.models.user import User as UserModel

            target_user = (
                db.query(UserModel)
                .filter(UserModel.id == report_request.user_id)
                .first()
            )
            if (
                not current_user.organization_id
                or not target_user
                or target_user.organization_id != current_user.organization_id
            ):
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            user_id = str(report_request.user_id)

        # Generate report
        report = await user_behavior_service.generate_behavior_report(
            user_id=user_id,
            organization_id=str(current_user.organization_id),
            days_back=report_request.days_back,
        )

        report["report_type"] = report_request.report_type
        report["requested_by"] = str(current_user.id)

        return report

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate behavior report: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/export")
async def export_behavior_data(
    export_request: AnalyticsExportRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Export behavior analytics data"""
    try:
        # Check permissions
        from src.models.user import UserRole

        if current_user.role not in [UserRole.ADMIN]:
            raise HTTPException(status_code=403, detail="Insufficient permissions")

        # Generate export data based on type
        export_data = {}

        if export_request.data_type == "user_behavior":
            # Get organization user behavior
            trends = await user_behavior_service.get_behavior_trends(
                organization_id=str(current_user.organization_id),
                days_back=(export_request.end_date - export_request.start_date).days,
                group_by="day",
            )
            export_data["trends"] = trends

            patterns = await user_behavior_service.identify_behavior_patterns(
                organization_id=str(current_user.organization_id)
            )
            export_data["patterns"] = patterns

        elif export_request.data_type == "content_usage":
            # Get content usage
            content_data = await get_content_usage_analytics(
                days_back=(export_request.end_date - export_request.start_date).days,
                limit=500,
                current_user=current_user,
            )
            export_data["content_usage"] = content_data["content_usage"]

        # Create export response
        export_response = AnalyticsExportResponse(
            export_id=str(uuid.uuid4()),
            status="completed",
            created_at=datetime.utcnow().isoformat(),
        )

        # In a real implementation, you would save the export file
        # For now, return the data directly
        return {
            "export_id": export_response.export_id,
            "status": export_response.status,
            "data": export_data,
            "format": export_request.format,
            "created_at": export_response.created_at,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export behavior data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health")
async def behavior_analytics_health():
    """Health check for behavior analytics service"""
    return {
        "status": "healthy",
        "service": "user_behavior_analytics",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }
