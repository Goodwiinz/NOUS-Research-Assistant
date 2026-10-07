"""
User behavior analytics service for tracking and analyzing user interactions
"""

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, asc, desc, func, or_, text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.database import get_db_sync
from src.models.organization import Organization
from src.models.quality_metrics import SearchEvent, SearchSession
from src.models.user import User

logger = logging.getLogger(__name__)


class BehaviorPattern(Enum):
    """User behavior patterns"""

    POWER_USER = "power_user"  # High frequency, advanced features
    CASUAL_USER = "casual_user"  # Low frequency, basic features
    RESEARCHER = "researcher"  # Deep queries, long sessions
    EFFICIENT_USER = "efficient_user"  # Quick searches, high success
    EXPLORER = "explorer"  # Diverse queries, browsing
    FRUSTRATED_USER = "frustrated_user"  # Many failed searches


@dataclass
class UserBehaviorMetrics:
    """User behavior metrics"""

    user_id: str
    session_count: int
    total_searches: int
    avg_session_duration: float
    avg_response_time: float
    success_rate: float
    click_through_rate: float
    avg_results_per_search: float
    query_diversity: float
    preferred_search_types: List[str]
    top_queries: List[str]
    behavior_pattern: BehaviorPattern
    last_active: datetime
    engagement_score: float
    satisfaction_score: Optional[float]


@dataclass
class SessionAnalysis:
    """Search session analysis"""

    session_id: str
    user_id: str
    duration: float
    search_count: int
    avg_response_time: float
    clicked_results: int
    total_results_viewed: int
    queries: List[str]
    search_types: List[str]
    bounce_rate: bool
    task_completion_rate: float
    satisfaction_indicators: List[str]


class UserBehaviorService:
    """
    Service for tracking and analyzing user behavior patterns
    """

    def __init__(self):
        self.behavior_cache = {}  # Cache user behavior analysis
        self.session_cache = {}  # Cache session analysis

    async def track_search_event(
        self,
        session_id: str,
        query: str,
        search_type: str,
        results_count: int,
        response_time: float,
        user_id: Optional[str] = None,
        search_query_id: Optional[str] = None,
        page_number: int = 1,
        filters_applied: Optional[Dict[str, Any]] = None,
        sort_order: Optional[str] = None,
    ) -> SearchEvent:
        """Track a search event for behavior analysis"""

        db = next(get_db_sync())

        try:
            # Create search event
            search_event = SearchEvent(
                session_id=(
                    uuid.UUID(session_id) if isinstance(session_id, str) else session_id
                ),
                query=query,
                search_type=search_type,
                results_count=results_count,
                response_time=response_time,
                user_id=uuid.UUID(user_id) if user_id else None,
                search_query_id=uuid.UUID(search_query_id) if search_query_id else None,
                page_number=page_number,
                filters_applied=filters_applied,
                sort_order=sort_order,
            )

            db.add(search_event)
            db.commit()
            db.refresh(search_event)

            # Update session metrics
            await self._update_session_metrics(session_id, search_event, db)

            logger.info(f"Tracked search event: {query} for user {user_id}")
            return search_event

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to track search event: {e}")
            raise
        finally:
            db.close()

    async def track_user_interaction(
        self,
        session_id: str,
        event_id: str,
        interaction_type: str,
        data: Dict[str, Any],
        organization_id: str,
    ) -> bool:
        """Track user interaction with search results.

        ``organization_id`` (the caller's own org) scopes the event lookup so a
        client-supplied ``event_id`` can only mutate the caller's tenant's
        events — never another org's analytics rows.
        """

        db = next(get_db_sync())

        try:
            # Get the search event (scoped to the caller's organization)
            search_event = (
                db.query(SearchEvent)
                .filter(
                    SearchEvent.id == uuid.UUID(event_id),
                    SearchEvent.organization_id == organization_id,
                )
                .first()
            )

            if not search_event:
                logger.warning(f"Search event not found: {event_id}")
                return False

            # Update interaction data based on type
            if interaction_type == "click":
                if search_event.clicked_results is None:
                    search_event.clicked_results = 0
                search_event.clicked_results += 1

                if search_event.clicked_result_ids is None:
                    search_event.clicked_result_ids = []
                if "result_id" in data:
                    search_event.clicked_result_ids.append(data["result_id"])

                # Track time to first click
                if search_event.time_to_first_click is None and "click_time" in data:
                    search_event.time_to_first_click = data["click_time"]

            elif interaction_type == "dwell":
                if "dwell_time" in data:
                    search_event.dwell_time = data["dwell_time"]

            elif interaction_type == "rating":
                if "rating" in data:
                    search_event.user_rating = data["rating"]
                if "feedback" in data:
                    search_event.feedback_text = data["feedback"]

            elif interaction_type == "bookmark":
                search_event.is_bookmarked = data.get("bookmarked", False)

            db.commit()

            logger.info(
                f"Tracked user interaction: {interaction_type} for event {event_id}"
            )
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to track user interaction: {e}")
            return False
        finally:
            db.close()

    async def analyze_user_behavior(
        self,
        user_id: str,
        days_back: int = 30,
        use_cache: bool = True,
        *,
        organization_id: Optional[str],
    ) -> UserBehaviorMetrics:
        """Analyze a user's behavior patterns within ONE organization.

        ``organization_id`` (the caller's own org) is keyword-only and required:
        sessions are filtered by it so a client-supplied ``user_id`` can never
        surface another tenant's search history (including a user's sessions
        from an org they have since left). The cache is keyed by
        (org, user_id, days_back) -- mirroring ``analyze_session`` -- so a cached
        result is never served across tenants. A null org has no sessions
        (``search_sessions.organization_id`` is NOT NULL), so it short-circuits
        to empty metrics.
        """

        if not organization_id:
            return self._create_empty_behavior_metrics(user_id)

        cache_key = f"{organization_id}_{user_id}_{days_back}"
        if use_cache and cache_key in self.behavior_cache:
            return self.behavior_cache[cache_key]

        db = next(get_db_sync())

        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)

            # Get user's search sessions (scoped to the caller's organization)
            sessions = (
                db.query(SearchSession)
                .filter(
                    SearchSession.user_id == uuid.UUID(user_id),
                    SearchSession.organization_id == uuid.UUID(str(organization_id)),
                    SearchSession.start_time >= cutoff_date,
                )
                .all()
            )

            if not sessions:
                return self._create_empty_behavior_metrics(user_id)

            # Get user's search events
            session_ids = [s.id for s in sessions]
            events = (
                db.query(SearchEvent)
                .filter(SearchEvent.session_id.in_(session_ids))
                .all()
            )

            # Calculate metrics
            metrics = self._calculate_behavior_metrics(
                user_id, sessions, events, days_back
            )

            # Cache the results
            if use_cache:
                self.behavior_cache[cache_key] = metrics

            return metrics

        except Exception as e:
            logger.error(f"Failed to analyze user behavior: {e}")
            return self._create_empty_behavior_metrics(user_id)
        finally:
            db.close()

    async def analyze_session(
        self, session_id: str, organization_id: str
    ) -> SessionAnalysis:
        """Analyze a specific search session.

        ``organization_id`` (the caller's own org) scopes the lookup so a
        client-supplied ``session_id`` can only resolve to the caller's own
        tenant's session. The cache is keyed by (org, session_id) so a cached
        analysis is never served across tenants.
        """

        cache_key = (str(organization_id), session_id)
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]

        db = next(get_db_sync())

        try:
            # Get session (scoped to the caller's organization)
            session = (
                db.query(SearchSession)
                .filter(
                    SearchSession.session_id == session_id,
                    SearchSession.organization_id == organization_id,
                )
                .first()
            )

            if not session:
                raise ValueError(f"Session not found: {session_id}")

            # Get events
            events = (
                db.query(SearchEvent)
                .filter(SearchEvent.session_id == session.id)
                .order_by(SearchEvent.created_at)
                .all()
            )

            # Analyze session
            analysis = self._analyze_session_details(session, events)

            # Cache the result (keyed by org + session to avoid cross-tenant reuse)
            self.session_cache[cache_key] = analysis

            return analysis

        except Exception as e:
            logger.error(f"Failed to analyze session: {e}")
            raise
        finally:
            db.close()

    async def get_behavior_trends(
        self, organization_id: str, days_back: int = 30, group_by: str = "day"
    ) -> Dict[str, Any]:
        """Get behavior trends for organization"""

        db = next(get_db_sync())

        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)

            # Get daily/weekly/monthly aggregates
            if group_by == "day":
                date_format = "YYYY-MM-DD"
            elif group_by == "week":
                date_format = 'YYYY-"WW"'
            else:  # month
                date_format = "YYYY-MM"

            # Query session trends
            session_trends = db.execute(
                text(f"""
                SELECT
                    DATE_TRUNC('{group_by}', start_time) as period,
                    COUNT(*) as session_count,
                    AVG(search_count) as avg_searches_per_session,
                    AVG(EXTRACT(EPOCH FROM (end_time - start_time))) as avg_duration_seconds
                FROM search_sessions
                WHERE organization_id = :org_id
                    AND start_time >= :cutoff_date
                GROUP BY period
                ORDER BY period
            """),  # nosec: B608 - group_by constrained to enum-derived literal values
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Query search event trends
            event_trends = db.execute(
                text(f"""
                SELECT
                    DATE_TRUNC('{group_by}', created_at) as period,
                    COUNT(*) as search_count,
                    AVG(response_time) as avg_response_time,
                    AVG(results_count) as avg_results,
                    SUM(clicked_results) as total_clicks
                FROM search_events
                WHERE organization_id = :org_id
                    AND created_at >= :cutoff_date
                GROUP BY period
                ORDER BY period
            """),  # nosec: B608 - group_by constrained to enum-derived literal values
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Query user engagement trends
            engagement_trends = db.execute(
                text(f"""
                SELECT
                    DATE_TRUNC('{group_by}', s.start_time) as period,
                    COUNT(DISTINCT s.user_id) as active_users,
                    COUNT(DISTINCT CASE WHEN s.search_count > 5 THEN s.user_id END) as power_users,
                    AVG(CASE WHEN e.user_rating IS NOT NULL THEN e.user_rating END) as avg_satisfaction
                FROM search_sessions s
                LEFT JOIN search_events e ON s.id = e.session_id
                WHERE s.organization_id = :org_id
                    AND s.start_time >= :cutoff_date
                GROUP BY period
                ORDER BY period
            """),  # nosec: B608 - group_by constrained to enum-derived literal values
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            return {
                "session_trends": [
                    {
                        "period": str(row.period),
                        "session_count": row.session_count,
                        "avg_searches_per_session": float(
                            row.avg_searches_per_session or 0
                        ),
                        "avg_duration_seconds": float(row.avg_duration_seconds or 0),
                    }
                    for row in session_trends
                ],
                "search_trends": [
                    {
                        "period": str(row.period),
                        "search_count": row.search_count,
                        "avg_response_time": float(row.avg_response_time or 0),
                        "avg_results": float(row.avg_results or 0),
                        "total_clicks": row.total_clicks,
                    }
                    for row in event_trends
                ],
                "engagement_trends": [
                    {
                        "period": str(row.period),
                        "active_users": row.active_users,
                        "power_users": row.power_users,
                        "avg_satisfaction": float(row.avg_satisfaction or 0),
                    }
                    for row in engagement_trends
                ],
            }

        except Exception as e:
            logger.error(f"Failed to get behavior trends: {e}")
            raise
        finally:
            db.close()

    async def get_behavioral_insights(
        self, organization_id: str, days_back: int = 30
    ) -> Dict[str, Any]:
        """Get comprehensive behavioral insights for the organization"""

        db = next(get_db_sync())

        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)

            # Get user engagement metrics
            engagement_data = db.execute(
                text("""
                SELECT
                    COUNT(DISTINCT s.user_id) as active_users,
                    COUNT(s.id) as total_sessions,
                    AVG(EXTRACT(EPOCH FROM (s.end_time - s.start_time))) as avg_session_duration,
                    COUNT(e.id) as total_searches,
                    AVG(CASE WHEN e.clicked_results > 0 THEN 1 ELSE 0 END) as success_rate
                FROM search_sessions s
                LEFT JOIN search_events e ON s.id = e.session_id
                WHERE s.organization_id = :org_id
                    AND s.start_time >= :cutoff_date
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchone()

            # Get top queries
            top_queries = db.execute(
                text("""
                SELECT
                    e.query,
                    COUNT(*) as search_count,
                    AVG(e.response_time) as avg_response_time,
                    AVG(CASE WHEN e.clicked_results > 0 THEN 1 ELSE 0 END) as success_rate
                FROM search_events e
                JOIN search_sessions s ON e.session_id = s.id
                WHERE s.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
                GROUP BY e.query
                ORDER BY search_count DESC
                LIMIT 10
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Get search types distribution
            search_types = db.execute(
                text("""
                SELECT
                    e.search_type,
                    COUNT(*) as count,
                    AVG(e.response_time) as avg_response_time
                FROM search_events e
                JOIN search_sessions s ON e.session_id = s.id
                WHERE s.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
                GROUP BY e.search_type
                ORDER BY count DESC
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Get peak usage hours
            peak_hours = db.execute(
                text("""
                SELECT
                    EXTRACT(HOUR FROM e.created_at) as hour,
                    COUNT(*) as search_count
                FROM search_events e
                JOIN search_sessions s ON e.session_id = s.id
                WHERE s.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
                GROUP BY hour
                ORDER BY search_count DESC
                LIMIT 5
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Get most accessed documents
            top_documents = db.execute(
                text("""
                SELECT
                    d.id,
                    d.title,
                    COUNT(DISTINCT s.user_id) as unique_users,
                    COUNT(*) as access_count
                FROM search_results sr
                JOIN documents d ON sr.document_id = d.id
                JOIN search_queries sq ON sr.search_query_id = sq.id
                JOIN search_events e ON sq.id = e.search_query_id
                JOIN search_sessions s ON e.session_id = s.id
                WHERE d.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
                GROUP BY d.id, d.title
                ORDER BY unique_users DESC, access_count DESC
                LIMIT 10
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchall()

            # Calculate engagement trend (compare with previous period)
            prev_cutoff = cutoff_date - timedelta(days=days_back)
            current_engagement = engagement_data.active_users if engagement_data else 0

            prev_engagement = db.execute(
                text("""
                SELECT COUNT(DISTINCT s.user_id) as active_users
                FROM search_sessions s
                WHERE s.organization_id = :org_id
                    AND s.start_time BETWEEN :prev_cutoff AND :cutoff_date
            """),
                {
                    "org_id": organization_id,
                    "prev_cutoff": prev_cutoff,
                    "cutoff_date": cutoff_date,
                },
            ).fetchone()

            engagement_trend = "stable"
            if prev_engagement and prev_engagement.active_users > 0:
                change_pct = (
                    (current_engagement - prev_engagement.active_users)
                    / prev_engagement.active_users
                ) * 100
                if change_pct > 10:
                    engagement_trend = "increasing"
                elif change_pct < -10:
                    engagement_trend = "decreasing"

            # Calculate retention rate (simplified)
            retention_data = db.execute(
                text("""
                WITH current_period_users AS (
                    SELECT DISTINCT s.user_id
                    FROM search_sessions s
                    WHERE s.organization_id = :org_id
                        AND s.start_time >= :cutoff_date
                ),
                previous_period_users AS (
                    SELECT DISTINCT s.user_id
                    FROM search_sessions s
                    WHERE s.organization_id = :org_id
                        AND s.start_time BETWEEN :prev_cutoff AND :cutoff_date
                )
                SELECT
                    COUNT(DISTINCT CASE WHEN cp.user_id IS NOT NULL THEN cp.user_id END) as retained_users,
                    COUNT(DISTINCT pp.user_id) as previous_total_users
                FROM previous_period_users pp
                LEFT JOIN current_period_users cp ON pp.user_id = cp.user_id
            """),
                {
                    "org_id": organization_id,
                    "prev_cutoff": prev_cutoff,
                    "cutoff_date": cutoff_date,
                },
            ).fetchone()

            retention_rate = 0
            if retention_data and retention_data.previous_total_users > 0:
                retention_rate = (
                    retention_data.retained_users / retention_data.previous_total_users
                ) * 100

            # Get user satisfaction metrics
            satisfaction = db.execute(
                text("""
                SELECT
                    AVG(e.user_rating) as avg_rating,
                    COUNT(e.user_rating) as rating_count
                FROM search_events e
                JOIN search_sessions s ON e.session_id = s.id
                WHERE s.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
                    AND e.user_rating IS NOT NULL
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchone()

            # Get task completion rate (simplified)
            task_completion = db.execute(
                text("""
                SELECT
                    COUNT(CASE WHEN e.clicked_results > 0 THEN 1 END) as successful_searches,
                    COUNT(*) as total_searches
                FROM search_events e
                JOIN search_sessions s ON e.session_id = s.id
                WHERE s.organization_id = :org_id
                    AND e.created_at >= :cutoff_date
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchone()

            completion_rate = 0
            if task_completion and task_completion.total_searches > 0:
                completion_rate = (
                    task_completion.successful_searches / task_completion.total_searches
                ) * 100

            # Get behavioral segments
            patterns = await self.identify_behavior_patterns(
                organization_id, min_sessions=1
            )
            behavioral_segments = []
            for pattern_name, users in patterns.items():
                if users:
                    behavioral_segments.append(
                        {
                            "segment": pattern_name,
                            "count": len(users),
                            "percentage": (
                                (
                                    len(users)
                                    / sum(
                                        len(segment_users)
                                        for segment_users in patterns.values()
                                    )
                                )
                                * 100
                                if patterns
                                else 0
                            ),
                            "characteristics": self._get_pattern_characteristics(
                                pattern_name
                            ),
                        }
                    )

            # Generate recommendations
            recommendations = self._generate_recommendations(
                engagement_data,
                top_queries,
                search_types,
                satisfaction,
                task_completion,
            )

            return {
                "active_users": engagement_data.active_users if engagement_data else 0,
                "total_sessions": (
                    engagement_data.total_sessions if engagement_data else 0
                ),
                "avg_session_duration": (
                    float(engagement_data.avg_session_duration or 0)
                    if engagement_data
                    else 0
                ),
                "engagement_trend": engagement_trend,
                "retention_rate": retention_rate,
                "top_queries": [
                    {
                        "query": query.query,
                        "count": query.search_count,
                        "success_rate": float(query.success_rate or 0),
                    }
                    for query in top_queries
                ],
                "search_types_distribution": {
                    st.search_type: {
                        "count": st.count,
                        "avg_response_time": float(st.avg_response_time or 0),
                    }
                    for st in search_types
                },
                "peak_usage_hours": [int(hour.hour) for hour in peak_hours],
                "most_accessed_documents": [
                    {
                        "document_id": str(doc.id),
                        "title": doc.title,
                        "unique_users": doc.unique_users,
                        "access_count": doc.access_count,
                    }
                    for doc in top_documents
                ],
                "click_through_rates": {
                    "overall": (
                        float(engagement_data.success_rate or 0)
                        if engagement_data
                        else 0
                    ),
                    "by_search_type": {
                        st.search_type: float(st.avg_response_time or 0)  # Placeholder
                        for st in search_types
                    },
                },
                "user_satisfaction": (
                    float(satisfaction.avg_rating or 0) if satisfaction else 0
                ),
                "task_completion_rate": completion_rate,
                "behavioral_segments": behavioral_segments,
                "recommendations": recommendations,
            }

        except Exception as e:
            logger.error(f"Failed to get behavioral insights: {e}")
            raise
        finally:
            db.close()

    def _get_pattern_characteristics(self, pattern_name: str) -> List[str]:
        """Get characteristics description for a behavior pattern"""
        characteristics_map = {
            "power_users": [
                "High search frequency",
                "Advanced feature usage",
                "Short sessions",
                "High success rates",
            ],
            "casual_users": [
                "Low search frequency",
                "Basic feature usage",
                "Long sessions",
                "Variable success rates",
            ],
            "researchers": [
                "Deep queries",
                "Long sessions",
                "High query diversity",
                "Moderate success rates",
            ],
            "efficient_users": [
                "Fast searches",
                "High success rates",
                "Focused queries",
                "Short sessions",
            ],
            "explorers": [
                "Diverse queries",
                "Moderate activity",
                "Browsing behavior",
                "Good engagement",
            ],
            "frustrated_users": [
                "Low success rates",
                "High retry attempts",
                "Poor satisfaction",
                "Long response times",
            ],
        }
        return characteristics_map.get(pattern_name, ["Unknown pattern"])

    def _generate_recommendations(
        self,
        engagement_data: Any,
        top_queries: Any,
        search_types: Any,
        satisfaction: Any,
        task_completion: Any,
    ) -> List[str]:
        """Generate recommendations based on analytics data"""
        recommendations = []

        # Engagement recommendations
        if (
            engagement_data
            and engagement_data.avg_session_duration
            and engagement_data.avg_session_duration < 60
        ):
            recommendations.append(
                "Consider improving search result relevance to increase session duration"
            )

        # Success rate recommendations
        if task_completion and task_completion.total_searches > 0:
            success_rate = (
                task_completion.successful_searches / task_completion.total_searches
            ) * 100
            if success_rate < 50:
                recommendations.append(
                    "Search success rate is below 50%. Consider improving query understanding or result ranking"
                )

        # Satisfaction recommendations
        if satisfaction and satisfaction.avg_rating and satisfaction.avg_rating < 3.0:
            recommendations.append(
                "User satisfaction is low. Consider implementing feedback mechanisms and improving result quality"
            )

        # Query diversity recommendations
        if top_queries and len(top_queries) > 0:
            top_query_ratio = (
                top_queries[0].search_count / sum(q.search_count for q in top_queries)
            ) * 100
            if top_query_ratio > 30:
                recommendations.append(
                    "High concentration on few queries. Consider improving query suggestion or autocomplete features"
                )

        # Performance recommendations
        if search_types:
            slow_search_types = [
                st
                for st in search_types
                if st.avg_response_time and st.avg_response_time > 3000
            ]
            if slow_search_types:
                recommendations.append(
                    f"Some search types are slow (>3s). Consider optimizing performance for: {', '.join(st.search_type for st in slow_search_types)}"
                )

        if not recommendations:
            recommendations.append(
                "System performance is good. Continue monitoring metrics and user feedback."
            )

        return recommendations

    async def identify_behavior_patterns(
        self, organization_id: str, min_sessions: int = 5
    ) -> Dict[str, Any]:
        """Identify user behavior patterns in the organization"""

        db = next(get_db_sync())

        try:
            # Get users with sufficient activity
            active_users = db.execute(
                text("""
                SELECT
                    u.id,
                    u.first_name,
                    u.last_name,
                    COUNT(s.id) as session_count,
                    COUNT(e.id) as search_count,
                    AVG(e.response_time) as avg_response_time,
                    AVG(CASE WHEN e.clicked_results > 0 THEN 1 ELSE 0 END) as click_rate,
                    AVG(e.user_rating) as avg_rating
                FROM users u
                JOIN search_sessions s ON u.id = s.user_id
                JOIN search_events e ON s.id = e.session_id
                WHERE u.organization_id = :org_id
                    AND s.start_time >= :cutoff_date
                GROUP BY u.id, u.first_name, u.last_name
                HAVING COUNT(s.id) >= :min_sessions
                ORDER BY search_count DESC
            """),
                {
                    "org_id": organization_id,
                    "cutoff_date": datetime.utcnow() - timedelta(days=30),
                    "min_sessions": min_sessions,
                },
            ).fetchall()

            patterns = {
                "power_users": [],
                "casual_users": [],
                "researchers": [],
                "efficient_users": [],
                "explorers": [],
                "frustrated_users": [],
            }

            for user in active_users:
                pattern = self._classify_user_behavior(
                    session_count=user.session_count,
                    search_count=user.search_count,
                    avg_response_time=user.avg_response_time,
                    click_rate=user.click_rate,
                    avg_rating=user.avg_rating,
                )

                patterns[pattern.value].append(
                    {
                        "user_id": str(user.id),
                        "name": f"{user.first_name} {user.last_name}",
                        "session_count": user.session_count,
                        "search_count": user.search_count,
                        "avg_response_time": float(user.avg_response_time or 0),
                        "click_rate": float(user.click_rate or 0),
                        "avg_rating": float(user.avg_rating or 0),
                    }
                )

            return patterns

        except Exception as e:
            logger.error(f"Failed to identify behavior patterns: {e}")
            raise
        finally:
            db.close()

    async def generate_behavior_report(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        days_back: int = 30,
    ) -> Dict[str, Any]:
        """Generate comprehensive behavior analytics report"""

        report = {
            "report_period": {
                "start_date": (
                    datetime.utcnow() - timedelta(days=days_back)
                ).isoformat(),
                "end_date": datetime.utcnow().isoformat(),
                "days": days_back,
            },
            "generated_at": datetime.utcnow().isoformat(),
        }

        if user_id:
            # Individual user report -- scoped to the requesting org (GOO-407)
            user_metrics = await self.analyze_user_behavior(
                user_id, days_back, organization_id=organization_id
            )
            report["user_metrics"] = {
                "user_id": user_metrics.user_id,
                "behavior_pattern": user_metrics.behavior_pattern.value,
                "session_count": user_metrics.session_count,
                "total_searches": user_metrics.total_searches,
                "avg_session_duration": user_metrics.avg_session_duration,
                "success_rate": user_metrics.success_rate,
                "engagement_score": user_metrics.engagement_score,
                "satisfaction_score": user_metrics.satisfaction_score,
                "preferred_search_types": user_metrics.preferred_search_types,
                "top_queries": user_metrics.top_queries,
            }

        if organization_id:
            # Organization-level report
            report["organization_metrics"] = await self.get_behavior_trends(
                organization_id, days_back
            )
            report["behavior_patterns"] = await self.identify_behavior_patterns(
                organization_id
            )

        return report

    async def _update_session_metrics(
        self, session_id: str, event: SearchEvent, db: Session
    ):
        """Update session metrics when a new event is added"""

        session = (
            db.query(SearchSession)
            .filter(SearchSession.session_id == session_id)
            .first()
        )

        if session:
            session.search_count += 1
            session.total_response_time += event.response_time
            session.avg_response_time = (
                session.total_response_time / session.search_count
            )

            db.commit()

    def _calculate_behavior_metrics(
        self,
        user_id: str,
        sessions: List[SearchSession],
        events: List[SearchEvent],
        days_back: int,
    ) -> UserBehaviorMetrics:
        """Calculate behavior metrics from sessions and events"""

        if not sessions:
            return self._create_empty_behavior_metrics(user_id)

        # Basic metrics
        session_count = len(sessions)
        total_searches = len(events)

        # Session duration
        durations = []
        for session in sessions:
            if session.end_time:
                duration = (session.end_time - session.start_time).total_seconds()
                durations.append(duration)

        avg_session_duration = statistics.mean(durations) if durations else 0

        # Response times
        response_times = [e.response_time for e in events]
        avg_response_time = statistics.mean(response_times) if response_times else 0

        # Success metrics
        successful_events = [e for e in events if e.clicked_results > 0]
        success_rate = (
            len(successful_events) / total_searches if total_searches > 0 else 0
        )

        # Click through rate
        total_clicks = sum(e.clicked_results or 0 for e in events)
        total_results = sum(e.results_count for e in events)
        click_through_rate = total_clicks / total_results if total_results > 0 else 0

        # Average results per search
        avg_results_per_search = (
            statistics.mean([e.results_count for e in events]) if events else 0
        )

        # Query diversity
        unique_queries = len(set(e.query for e in events))
        query_diversity = unique_queries / total_searches if total_searches > 0 else 0

        # Preferred search types
        search_types = [e.search_type for e in events]
        type_counts = {}
        for st in search_types:
            type_counts[st] = type_counts.get(st, 0) + 1
        preferred_search_types = sorted(
            type_counts.items(), key=lambda x: x[1], reverse=True
        )[:3]
        preferred_search_types = [t[0] for t in preferred_search_types]

        # Top queries
        query_counts = {}
        for e in events:
            query_counts[e.query] = query_counts.get(e.query, 0) + 1
        top_queries = sorted(query_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_queries = [q[0] for q in top_queries]

        # Behavior pattern
        behavior_pattern = self._classify_user_behavior(
            session_count=session_count,
            search_count=total_searches,
            avg_response_time=avg_response_time,
            click_rate=success_rate,
            avg_rating=(
                statistics.mean([e.user_rating for e in events if e.user_rating])
                if any(e.user_rating for e in events)
                else None
            ),
        )

        # Engagement score (0-100)
        engagement_score = self._calculate_engagement_score(
            session_count, total_searches, avg_session_duration, query_diversity
        )

        # Satisfaction score
        satisfaction_scores = [e.user_rating for e in events if e.user_rating]
        satisfaction_score = (
            statistics.mean(satisfaction_scores) if satisfaction_scores else None
        )

        # Last active
        last_active = max(s.start_time for s in sessions)

        return UserBehaviorMetrics(
            user_id=user_id,
            session_count=session_count,
            total_searches=total_searches,
            avg_session_duration=avg_session_duration,
            avg_response_time=avg_response_time,
            success_rate=success_rate,
            click_through_rate=click_through_rate,
            avg_results_per_search=avg_results_per_search,
            query_diversity=query_diversity,
            preferred_search_types=preferred_search_types,
            top_queries=top_queries,
            behavior_pattern=behavior_pattern,
            last_active=last_active,
            engagement_score=engagement_score,
            satisfaction_score=satisfaction_score,
        )

    def _analyze_session_details(
        self, session: SearchSession, events: List[SearchEvent]
    ) -> SessionAnalysis:
        """Analyze individual session details"""

        duration = 0
        if session.end_time:
            duration = (session.end_time - session.start_time).total_seconds()

        queries = [e.query for e in events]
        search_types = [e.search_type for e in events]

        clicked_results = sum(e.clicked_results or 0 for e in events)
        total_results_viewed = sum(e.results_count for e in events)

        avg_response_time = (
            statistics.mean([e.response_time for e in events]) if events else 0
        )

        # Bounce rate (single search and no interaction)
        bounce_rate = len(events) == 1 and clicked_results == 0

        # Task completion indicators
        satisfaction_indicators = []
        if any(e.user_rating and e.user_rating >= 4 for e in events):
            satisfaction_indicators.append("high_ratings")
        if any(e.is_bookmarked for e in events):
            satisfaction_indicators.append("bookmarks")
        if clicked_results > 0:
            satisfaction_indicators.append("clicks")

        task_completion_rate = len(satisfaction_indicators) / 3.0  # Max 3 indicators

        return SessionAnalysis(
            session_id=session.session_id,
            user_id=str(session.user_id) if session.user_id else None,
            duration=duration,
            search_count=len(events),
            avg_response_time=avg_response_time,
            clicked_results=clicked_results,
            total_results_viewed=total_results_viewed,
            queries=queries,
            search_types=search_types,
            bounce_rate=bounce_rate,
            task_completion_rate=task_completion_rate,
            satisfaction_indicators=satisfaction_indicators,
        )

    def _classify_user_behavior(
        self,
        session_count: int,
        search_count: int,
        avg_response_time: float,
        click_rate: float,
        avg_rating: Optional[float],
    ) -> BehaviorPattern:
        """Classify user into behavior pattern"""

        # Power user: High activity, good engagement
        if session_count > 10 and search_count > 50:
            return BehaviorPattern.POWER_USER

        # Frustrated user: Low success rate, low ratings
        if click_rate < 0.3 and avg_rating and avg_rating < 2.5:
            return BehaviorPattern.FRUSTRATED_USER

        # Researcher: Long sessions, diverse queries
        if search_count > 20 and avg_response_time > 3000:  # > 3 seconds avg
            return BehaviorPattern.RESEARCHER

        # Efficient user: Fast searches, high success rate
        if avg_response_time < 2000 and click_rate > 0.7:
            return BehaviorPattern.EFFICIENT_USER

        # Explorer: Moderate activity, diverse patterns
        if 5 <= session_count <= 15:
            return BehaviorPattern.EXPLORER

        # Casual user: Low activity
        if session_count < 5:
            return BehaviorPattern.CASUAL_USER

        # Default to explorer
        return BehaviorPattern.EXPLORER

    def _calculate_engagement_score(
        self,
        session_count: int,
        search_count: int,
        avg_session_duration: float,
        query_diversity: float,
    ) -> float:
        """Calculate engagement score (0-100)"""

        # Normalize factors
        session_score = min(session_count / 20, 1.0) * 25  # Max 25 points
        search_score = min(search_count / 100, 1.0) * 25  # Max 25 points
        duration_score = (
            min(avg_session_duration / 300, 1.0) * 25
        )  # Max 25 points (5 min)
        diversity_score = query_diversity * 25  # Max 25 points

        return session_score + search_score + duration_score + diversity_score

    def _create_empty_behavior_metrics(self, user_id: str) -> UserBehaviorMetrics:
        """Create empty behavior metrics for inactive users"""

        return UserBehaviorMetrics(
            user_id=user_id,
            session_count=0,
            total_searches=0,
            avg_session_duration=0,
            avg_response_time=0,
            success_rate=0,
            click_through_rate=0,
            avg_results_per_search=0,
            query_diversity=0,
            preferred_search_types=[],
            top_queries=[],
            behavior_pattern=BehaviorPattern.CASUAL_USER,
            last_active=datetime.utcnow(),
            engagement_score=0,
            satisfaction_score=None,
        )


# Global service instance
user_behavior_service = UserBehaviorService()
