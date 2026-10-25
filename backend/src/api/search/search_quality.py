"""
Search Quality Evaluation API endpoints
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.search_schemas import SearchQuery, SearchResponse, SearchType
from src.models.user import User
from src.services.search.hybrid_search_service import hybrid_search_service
from src.services.search.search_quality_service import (
    QualityMetric,
    QualityMetricType,
    SearchEvaluation,
    search_quality_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search-quality", tags=["search-quality"])


class SearchEvaluationRequest(BaseModel):
    """Request for search evaluation"""

    search_query: SearchQuery
    search_response: SearchResponse
    ground_truth_docs: Optional[List[str]] = Field(
        None, description="List of relevant document IDs for recall calculation"
    )


class UserFeedbackRequest(BaseModel):
    """Request for user feedback submission"""

    query_id: str = Field(..., description="Query identifier")
    rating: int = Field(..., ge=1, le=5, description="User rating (1-5)")
    feedback_text: Optional[str] = Field(None, description="Optional feedback text")
    document_id: Optional[str] = Field(
        None, description="Document ID if feedback is specific to a result"
    )
    search_type: Optional[SearchType] = Field(
        None, description="Search type the feedback refers to"
    )


class BenchmarkRequest(BaseModel):
    """Request for search quality benchmark"""

    test_queries: List[str] = Field(
        ..., min_length=1, max_length=50, description="Test queries for benchmarking"
    )
    search_types: Optional[List[SearchType]] = Field(
        None, description="Search types to test (defaults to all)"
    )


class QualityAnalyticsResponse(BaseModel):
    """Response for quality analytics"""

    period_days: int
    organization_id: str
    search_type: str
    total_evaluations: int
    average_metrics: Dict[str, float]
    trends: Dict[str, Any]
    threshold_violations: List[Dict[str, Any]]
    top_improvements: List[str]


class SearchEvaluationResponse(BaseModel):
    """Response for search evaluation"""

    query_id: str
    query: str
    search_type: SearchType
    results: List[Dict[str, Any]]
    metrics: Dict[str, float]
    overall_score: float
    evaluation_time_ms: float
    timestamp: datetime
    recommendations: List[str]


class BenchmarkResponse(BaseModel):
    """Response for benchmark results"""

    benchmark_id: str
    test_queries: List[str]
    results: Dict[str, Dict[str, SearchEvaluationResponse]]
    summary: Dict[str, Any]
    timestamp: datetime


@router.post("/evaluate", response_model=SearchEvaluationResponse)
async def evaluate_search_quality(
    request: SearchEvaluationRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Evaluate the quality of a search response using multiple metrics

    This endpoint analyzes search results and calculates various quality metrics
    including relevancy, precision, recall, response time, and diversity.
    """
    try:
        evaluation = search_quality_service.evaluate_search(
            search_query=request.search_query,
            search_response=request.search_response,
            user_id=str(current_user.id),
            organization_id=str(current_user.organization_id),
            ground_truth_docs=request.ground_truth_docs,
        )

        # Convert results to dict for JSON serialization
        results_data = []
        for result in evaluation.results:
            results_data.append(
                {
                    "document_id": result.document_id,
                    "title": result.title,
                    "document_type": result.document_type.value,
                    "content_preview": result.content_preview,
                    "relevance_score": result.relevance_score,
                    "score_breakdown": result.score_breakdown,
                    "source_type": result.source_type.value,
                    "highlights": result.highlights,
                    "metadata": result.metadata,
                }
            )

        return SearchEvaluationResponse(
            query_id=evaluation.query_id,
            query=evaluation.query,
            search_type=evaluation.search_type,
            results=results_data,
            metrics={
                metric_type.value: value
                for metric_type, value in evaluation.metrics.items()
            },
            overall_score=evaluation.overall_score,
            evaluation_time_ms=evaluation.evaluation_time_ms,
            timestamp=evaluation.timestamp,
            recommendations=evaluation.recommendations,
        )

    except Exception as e:
        logger.error(f"Error evaluating search quality: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/feedback")
async def submit_user_feedback(
    request: UserFeedbackRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """
    Submit user feedback for search results

    This endpoint records user satisfaction ratings and feedback text,
    which are used to improve search quality metrics.
    """
    try:
        # R6-F3a: the service opens a sync session and commits inline —
        # calling it directly from this async handler blocked the event loop.
        metric = await run_in_threadpool(
            search_quality_service.record_user_feedback,
            query_id=request.query_id,
            user_id=str(current_user.id),
            rating=request.rating,
            feedback_text=request.feedback_text,
            document_id=request.document_id,
            organization_id=str(current_user.organization_id or ""),
            search_type=request.search_type,
        )

        # Log feedback for analytics
        background_tasks.add_task(
            logger.info,
            f"User feedback received: query_id={request.query_id}, user_id={current_user.id}, "
            f"rating={request.rating}, document_id={request.document_id}",
        )

        return {
            "message": "Feedback recorded successfully",
            "metric_id": f"{metric.metric_type.value}_{metric.timestamp.isoformat()}",
            "normalized_score": metric.value,
            "original_rating": request.rating,
        }

    except Exception as e:
        logger.error(f"Error recording user feedback: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/analytics", response_model=QualityAnalyticsResponse)
async def get_quality_analytics(
    days: int = Query(
        default=30, ge=1, le=365, description="Number of days for analytics"
    ),
    search_type: Optional[SearchType] = Query(
        None, description="Filter by search type"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Get quality analytics for the organization

    Returns aggregated quality metrics, trends, and improvement recommendations
    for the specified time period.
    """
    try:
        # R6-F3a: sync session work — off the event loop.
        analytics = await run_in_threadpool(
            search_quality_service.get_quality_analytics,
            organization_id=str(current_user.organization_id),
            days=days,
            search_type=search_type,
        )

        if "error" in analytics:
            # The service puts str(e) under "error"; keep it in logs only.
            logger.error(
                "Quality analytics failed for organization %s: %s",
                current_user.organization_id,
                analytics["error"],
            )
            raise HTTPException(status_code=500, detail="Internal server error")

        return QualityAnalyticsResponse(**analytics)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting quality analytics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/benchmark", response_model=BenchmarkResponse)
async def run_quality_benchmark(
    request: BenchmarkRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """
    Run comprehensive quality benchmark on test queries

    This endpoint executes multiple search types against test queries and
    provides detailed quality comparisons and recommendations.
    """
    try:
        # R6-M1: a serial multi-query × multi-type benchmark executed inline
        # pinned the event loop/worker for minutes — any authenticated user
        # could DoS the whole service. Admin-only + hard-capped + threadpool.
        if current_user.role.value != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        # Admin is a role boundary, not a tenant boundary: an org-less caller
        # would benchmark against the literal "None" tenant.
        if not current_user.organization_id:
            raise HTTPException(
                status_code=403,
                detail="No organization associated with this account",
            )
        if len(request.test_queries) > 10:
            raise HTTPException(
                status_code=400, detail="Maximum 10 test queries allowed per benchmark"
            )

        benchmark_results = await run_in_threadpool(
            search_quality_service.run_quality_benchmark,
            test_queries=request.test_queries,
            search_types=request.search_types,
            organization_id=str(current_user.organization_id),
        )

        # Convert results for JSON serialization
        serialized_results = {}
        for query, query_results in benchmark_results.items():
            serialized_results[query] = {}
            for search_type, evaluation in query_results.items():
                results_data = []
                for result in evaluation.results:
                    # R2-M17: several fields are optional on some result
                    # shapes; getattr fallbacks instead of AttributeError-500
                    # on any non-empty result set.
                    doc_type = getattr(result, "document_type", None)
                    src_type = getattr(result, "source_type", None)
                    results_data.append(
                        {
                            "document_id": getattr(result, "document_id", None),
                            "title": getattr(result, "title", "") or "",
                            "document_type": (
                                doc_type.value
                                if hasattr(doc_type, "value")
                                else (doc_type or "unknown")
                            ),
                            "content_preview": getattr(result, "content_preview", "")
                            or "",
                            "relevance_score": float(
                                getattr(result, "relevance_score", 0.0) or 0.0
                            ),
                            "score_breakdown": getattr(result, "score_breakdown", {})
                            or {},
                            "source_type": (
                                src_type.value
                                if hasattr(src_type, "value")
                                else (src_type or "unknown")
                            ),
                            "highlights": list(getattr(result, "highlights", []) or []),
                            "metadata": dict(getattr(result, "metadata", {}) or {}),
                        }
                    )

                serialized_results[query][search_type] = SearchEvaluationResponse(
                    query_id=evaluation.query_id,
                    query=evaluation.query,
                    search_type=evaluation.search_type,
                    results=results_data,
                    metrics={
                        metric_type.value: value
                        for metric_type, value in evaluation.metrics.items()
                    },
                    overall_score=evaluation.overall_score,
                    evaluation_time_ms=evaluation.evaluation_time_ms,
                    timestamp=evaluation.timestamp,
                    recommendations=evaluation.recommendations,
                )

        # Generate summary statistics
        summary = _generate_benchmark_summary(serialized_results)

        benchmark_id = f"benchmark_{int(datetime.utcnow().timestamp())}"

        # Log benchmark execution
        background_tasks.add_task(
            logger.info,
            f"Benchmark completed: benchmark_id={benchmark_id}, user_id={current_user.id}, "
            f"queries={len(request.test_queries)}, search_types={len(request.search_types) if request.search_types else 3}",
        )

        return BenchmarkResponse(
            benchmark_id=benchmark_id,
            test_queries=request.test_queries,
            results=serialized_results,
            summary=summary,
            timestamp=datetime.utcnow(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running quality benchmark: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/metrics/types")
async def get_quality_metric_types():
    """
    Get available quality metric types and descriptions

    Returns a list of all supported quality metrics with their
    descriptions and threshold values.
    """
    return {
        "metric_types": [
            {
                "type": metric_type.value,
                "name": metric_type.value.replace("_", " ").title(),
                "description": _get_metric_description(metric_type),
                "threshold": search_quality_service.metric_thresholds.get(
                    metric_type, 0.5
                ),
            }
            for metric_type in QualityMetricType
        ]
    }


@router.get("/health")
async def health_check():
    """
    Health check for search quality service

    Returns the status of the search quality evaluation system.
    """
    try:
        # Test basic functionality
        test_metrics = {
            QualityMetricType.RELEVANCY: 0.8,
            QualityMetricType.PRECISION: 0.7,
            QualityMetricType.RESPONSE_TIME: 1.2,
        }

        overall_score = search_quality_service._calculate_overall_score(test_metrics)
        recommendations = search_quality_service._generate_recommendations(test_metrics)

        return {
            "status": "healthy",
            "service": "search_quality_evaluation",
            "timestamp": datetime.utcnow().isoformat(),
            "test_score": overall_score,
            "test_recommendations": len(recommendations),
            "available_metrics": len(QualityMetricType),
        }

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "search_quality_evaluation",
                # Unauthenticated endpoint: internals stay in logs, not bodies.
                "error": "search quality service unavailable",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )


def _generate_benchmark_summary(
    results: Dict[str, Dict[str, SearchEvaluationResponse]],
) -> Dict[str, Any]:
    """Generate summary statistics for benchmark results"""
    summary = {
        "total_queries": len(results),
        "search_types": set(),
        "average_scores": {},
        "best_performing": {},
        "improvement_areas": [],
        "metric_averages": {},
    }

    all_scores = {}
    all_metrics = {}

    for query, query_results in results.items():
        for search_type, evaluation in query_results.items():
            summary["search_types"].add(search_type)

            # Collect overall scores
            if search_type not in all_scores:
                all_scores[search_type] = []
            all_scores[search_type].append(evaluation.overall_score)

            # Collect individual metrics
            for metric_name, value in evaluation.metrics.items():
                if metric_name not in all_metrics:
                    all_metrics[metric_name] = {}
                if search_type not in all_metrics[metric_name]:
                    all_metrics[metric_name][search_type] = []
                all_metrics[metric_name][search_type].append(value)

    # Calculate averages
    for search_type, scores in all_scores.items():
        summary["average_scores"][search_type] = sum(scores) / len(scores)

    # Find best performing search type
    if summary["average_scores"]:
        best_type = max(summary["average_scores"], key=summary["average_scores"].get)
        summary["best_performing"] = {
            "search_type": best_type,
            "average_score": summary["average_scores"][best_type],
        }

    # Calculate metric averages
    for metric_name, metric_data in all_metrics.items():
        summary["metric_averages"][metric_name] = {}
        for search_type, values in metric_data.items():
            summary["metric_averages"][metric_name][search_type] = sum(values) / len(
                values
            )

    # Identify improvement areas
    for search_type, avg_score in summary["average_scores"].items():
        if avg_score < 0.7:
            summary["improvement_areas"].append(
                {
                    "search_type": search_type,
                    "average_score": avg_score,
                    "recommendation": f"Consider optimizing {search_type} search performance",
                }
            )

    summary["search_types"] = list(summary["search_types"])
    return summary


def _get_metric_description(metric_type: QualityMetricType) -> str:
    """Get description for a quality metric type"""
    descriptions = {
        QualityMetricType.RELEVANCY: "Measures how well search results match the query intent",
        QualityMetricType.PRECISION: "Ratio of relevant results to total results returned",
        QualityMetricType.RECALL: "Ratio of relevant documents found to total relevant documents",
        QualityMetricType.F1_SCORE: "Harmonic mean of precision and recall",
        QualityMetricType.RESPONSE_TIME: "Time taken to return search results",
        QualityMetricType.RESULT_DIVERSITY: "Variety and diversity of search results",
        QualityMetricType.FACTUAL_ACCURACY: "Accuracy of factual information in results",
        QualityMetricType.CONTEXTUAL_PRECISION: "Precision considering query context",
        QualityMetricType.USER_SATISFACTION: "User satisfaction ratings and feedback",
    }
    return descriptions.get(metric_type, "Quality metric for search evaluation")
