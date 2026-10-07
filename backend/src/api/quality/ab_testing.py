"""
A/B Testing API Endpoints for Multimodal Enterprise RAG System

This module provides comprehensive API endpoints for managing A/B testing experiments,
including experiment lifecycle management, real-time query routing, metrics collection,
and statistical analysis.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.ab_testing import (
    Experiment,
    ExperimentAssignment,
    ExperimentMetric,
    ExperimentStatus,
    ExperimentType,
    MetricType,
    QueryRouting,
    StatisticalTest,
    SuccessCriterion,
    TrafficSplitType,
    UserSegment,
    Variant,
)
from src.models.organization import Organization
from src.models.user import User
from src.services.ab_testing import ABIntegrationService, ABStatisticalAnalysisService
from src.services.core.cache import get_redis_client

# Create service instances
ab_testing_service = ABIntegrationService()
statistical_analysis_service = ABStatisticalAnalysisService()
from src.schemas.ab_testing import (
    BulkMetricSubmissionRequest,
    ExperimentAssignmentResponse,
    ExperimentCreateRequest,
    ExperimentResponse,
    ExperimentSummaryRequest,
    ExperimentSummaryResponse,
    ExperimentUpdateRequest,
    MetricSubmissionRequest,
    QueryRoutingResponse,
    StatisticalAnalysisRequest,
    StatisticalAnalysisResponse,
    UserSegmentCreateRequest,
    UserSegmentResponse,
    VariantCreateRequest,
    VariantResponse,
    VariantUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ab-testing", tags=["A/B Testing"])

# ============================================================================
# EXPERIMENT MANAGEMENT ENDPOINTS
# ============================================================================


@router.post("/experiments", response_model=ExperimentResponse, status_code=201)
async def create_experiment(
    experiment_data: ExperimentCreateRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Create a new A/B testing experiment

    Creates a new experiment with specified configuration, variants, and success criteria.
    The experiment starts in DRAFT status and must be explicitly started.

    Args:
        experiment_data: Experiment configuration details
        current_user: Authenticated user creating the experiment
        db: Database session

    Returns:
        Created experiment details

    Raises:
        HTTPException: If experiment creation fails
    """
    try:
        experiment = await ab_testing_service.create_experiment(
            experiment_data=experiment_data,
            creator_user_id=current_user.id,
            organization_id=current_user.organization_id,
            db=db,
        )

        logger.info(
            f"Created experiment {experiment.id}: {experiment.name} by user {current_user.id}"
        )

        return ExperimentResponse.from_experiment(experiment)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to create experiment: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/experiments", response_model=List[ExperimentResponse])
async def list_experiments(
    status: Optional[ExperimentStatus] = Query(
        None, description="Filter by experiment status"
    ),
    experiment_type: Optional[ExperimentType] = Query(
        None, description="Filter by experiment type"
    ),
    limit: int = Query(
        default=50, ge=1, le=100, description="Number of experiments to return"
    ),
    offset: int = Query(default=0, ge=0, description="Number of experiments to skip"),
    include_results: bool = Query(
        default=False, description="Include experimental results"
    ),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    List A/B testing experiments

    Retrieves experiments for the current user's organization with optional filtering.

    Args:
        status: Filter by experiment status
        experiment_type: Filter by experiment type
        limit: Maximum number of experiments to return
        offset: Number of experiments to skip for pagination
        include_results: Whether to include experimental results
        current_user: Authenticated user
        db: Database session

    Returns:
        List of experiments matching criteria
    """
    try:
        experiments = await ab_testing_service.list_experiments(
            organization_id=current_user.organization_id,
            status=status,
            experiment_type=experiment_type,
            limit=limit,
            offset=offset,
            include_results=include_results,
            db=db,
        )

        return [ExperimentResponse.from_experiment(exp) for exp in experiments]

    except Exception as e:
        logger.error(f"Failed to list experiments: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    include_results: bool = Query(
        default=False, description="Include experimental results"
    ),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Get experiment details

    Retrieves detailed information about a specific experiment including variants,
    configuration, and optionally results.

    Args:
        experiment_id: UUID of the experiment
        include_results: Whether to include experimental results
        current_user: Authenticated user
        db: Database session

    Returns:
        Experiment details

    Raises:
        HTTPException: If experiment not found or access denied
    """
    try:
        experiment = await ab_testing_service.get_experiment(
            experiment_id=experiment_id,
            organization_id=current_user.organization_id,
            include_results=include_results,
            db=db,
        )

        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return ExperimentResponse.from_experiment(experiment)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def update_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    experiment_data: ExperimentUpdateRequest = ...,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Update experiment configuration

    Updates experiment configuration. Can only be done while experiment is in DRAFT status.

    Args:
        experiment_id: UUID of the experiment
        experiment_data: Updated experiment configuration
        current_user: Authenticated user
        db: Database session

    Returns:
        Updated experiment details

    Raises:
        HTTPException: If experiment not found, access denied, or invalid status for updates
    """
    try:
        experiment = await ab_testing_service.update_experiment(
            experiment_id=experiment_id,
            experiment_data=experiment_data,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Updated experiment {experiment_id} by user {current_user.id}")

        return ExperimentResponse.from_experiment(experiment)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/experiments/{experiment_id}/start", response_model=ExperimentResponse)
async def start_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Start an experiment

    Starts the experiment, making it active for user assignments and metric collection.

    Args:
        experiment_id: UUID of the experiment
        current_user: Authenticated user
        db: Database session

    Returns:
        Updated experiment details

    Raises:
        HTTPException: If experiment not found, access denied, or cannot be started
    """
    try:
        experiment = await ab_testing_service.start_experiment(
            experiment_id=experiment_id,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Started experiment {experiment_id} by user {current_user.id}")

        return ExperimentResponse.from_experiment(experiment)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/experiments/{experiment_id}/stop", response_model=ExperimentResponse)
async def stop_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Stop an experiment

    Stops the experiment, preventing new user assignments but keeping existing data.

    Args:
        experiment_id: UUID of the experiment
        current_user: Authenticated user
        db: Database session

    Returns:
        Updated experiment details

    Raises:
        HTTPException: If experiment not found, access denied, or cannot be stopped
    """
    try:
        experiment = await ab_testing_service.stop_experiment(
            experiment_id=experiment_id,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Stopped experiment {experiment_id} by user {current_user.id}")

        return ExperimentResponse.from_experiment(experiment)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to stop experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/experiments/{experiment_id}", status_code=204)
async def delete_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    force: bool = Query(
        default=False, description="Force delete even if experiment has data"
    ),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Delete an experiment

    Deletes an experiment and all associated data. Only allowed in DRAFT status
    unless force parameter is used.

    Args:
        experiment_id: UUID of the experiment
        force: Force delete even if experiment has data
        current_user: Authenticated user
        db: Database session

    Raises:
        HTTPException: If experiment not found, access denied, or cannot be deleted
    """
    try:
        success = await ab_testing_service.delete_experiment(
            experiment_id=experiment_id,
            organization_id=current_user.organization_id,
            force=force,
            db=db,
        )

        if not success:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Deleted experiment {experiment_id} by user {current_user.id}")

        return JSONResponse(status_code=204, content=None)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# VARIANT MANAGEMENT ENDPOINTS
# ============================================================================


@router.post(
    "/experiments/{experiment_id}/variants",
    response_model=VariantResponse,
    status_code=201,
)
async def create_variant(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    variant_data: VariantCreateRequest = ...,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Create a new variant for an experiment

    Adds a new variant to an existing experiment. Only allowed in DRAFT status.

    Args:
        experiment_id: UUID of the experiment
        variant_data: Variant configuration details
        current_user: Authenticated user
        db: Database session

    Returns:
        Created variant details

    Raises:
        HTTPException: If experiment not found, access denied, or invalid status
    """
    try:
        variant = await ab_testing_service.create_variant(
            experiment_id=experiment_id,
            variant_data=variant_data,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not variant:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Created variant {variant.id} for experiment {experiment_id}")

        return VariantResponse.from_variant(variant)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create variant for experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/experiments/{experiment_id}/variants", response_model=List[VariantResponse]
)
async def list_variants(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    List variants for an experiment

    Retrieves all variants for a specific experiment.

    Args:
        experiment_id: UUID of the experiment
        current_user: Authenticated user
        db: Database session

    Returns:
        List of variants for the experiment

    Raises:
        HTTPException: If experiment not found or access denied
    """
    try:
        variants = await ab_testing_service.list_variants(
            experiment_id=experiment_id,
            organization_id=current_user.organization_id,
            db=db,
        )

        return [VariantResponse.from_variant(variant) for variant in variants]

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to list variants for experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/variants/{variant_id}", response_model=VariantResponse)
async def update_variant(
    variant_id: uuid.UUID = Path(..., description="Variant ID"),
    variant_data: VariantUpdateRequest = ...,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Update variant configuration

    Updates variant configuration. Only allowed while parent experiment is in DRAFT status.

    Args:
        variant_id: UUID of the variant
        variant_data: Updated variant configuration
        current_user: Authenticated user
        db: Database session

    Returns:
        Updated variant details

    Raises:
        HTTPException: If variant not found, access denied, or invalid status
    """
    try:
        variant = await ab_testing_service.update_variant(
            variant_id=variant_id,
            variant_data=variant_data,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not variant:
            raise HTTPException(status_code=404, detail="Variant not found")

        logger.info(f"Updated variant {variant_id} by user {current_user.id}")

        return VariantResponse.from_variant(variant)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update variant {variant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/variants/{variant_id}", status_code=204)
async def delete_variant(
    variant_id: uuid.UUID = Path(..., description="Variant ID"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Delete a variant

    Deletes a variant from its experiment. Only allowed in DRAFT status.

    Args:
        variant_id: UUID of the variant
        current_user: Authenticated user
        db: Database session

    Raises:
        HTTPException: If variant not found, access denied, or cannot be deleted
    """
    try:
        success = await ab_testing_service.delete_variant(
            variant_id=variant_id, organization_id=current_user.organization_id, db=db
        )

        if not success:
            raise HTTPException(status_code=404, detail="Variant not found")

        logger.info(f"Deleted variant {variant_id} by user {current_user.id}")

        return JSONResponse(status_code=204, content=None)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete variant {variant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# REAL-TIME QUERY ROUTING ENDPOINTS
# ============================================================================


@router.post("/routing/assign", response_model=QueryRoutingResponse)
async def assign_experiment_variant(
    user_id: Optional[uuid.UUID] = None,
    session_id: Optional[str] = None,
    query_context: Optional[Dict[str, Any]] = None,
    current_user: Optional[User] = Depends(get_current_user),
    db=Depends(get_db),
    redis_client=Depends(get_redis_client),
):
    """
    Assign user to experiment variant for query processing

    Real-time endpoint that assigns users to experiment variants based on
    configured targeting and traffic allocation rules. This is called during
    query processing to determine which variant should be used.

    Args:
        user_id: User ID (optional for anonymous users)
        session_id: Session ID for anonymous users
        query_context: Context information for targeting
        current_user: Authenticated user (if available)
        db: Database session
        redis_client: Redis client for caching

    Returns:
        Variant assignment information

    Raises:
        HTTPException: If assignment fails
    """
    try:
        # Determine effective user ID
        effective_user_id = current_user.id if current_user else user_id

        assignment = await ab_testing_service.assign_variant(
            user_id=effective_user_id,
            session_id=session_id,
            organization_id=current_user.organization_id if current_user else None,
            query_context=query_context or {},
            db=db,
            redis_client=redis_client,
        )

        if not assignment:
            # No active experiments for this user
            return QueryRoutingResponse(
                experiment_id=None,
                variant_id=None,
                assignment_type="none",
                routing_reason="no_active_experiments",
            )

        return QueryRoutingResponse(
            experiment_id=assignment.experiment_id,
            variant_id=assignment.variant_id,
            assignment_type=assignment.assignment_type,
            routing_reason=assignment.routing_reason or "standard_assignment",
            variant_config=assignment.variant.config,
        )

    except Exception as e:
        logger.error(f"Failed to assign experiment variant: {e}")
        # Return default routing on error to avoid breaking search
        return QueryRoutingResponse(
            experiment_id=None,
            variant_id=None,
            assignment_type="error_fallback",
            routing_reason="assignment_failed",
        )


@router.post("/routing/bulk-assign", response_model=List[QueryRoutingResponse])
async def bulk_assign_experiment_variants(
    assignments: List[Dict[str, Any]],
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
    redis_client=Depends(get_redis_client),
):
    """
    Bulk assign users to experiment variants

    Endpoint for batch processing multiple user assignments efficiently.

    Args:
        assignments: List of assignment requests
        current_user: Authenticated user
        db: Database session
        redis_client: Redis client for caching

    Returns:
        List of assignment responses

    Raises:
        HTTPException: If bulk assignment fails
    """
    try:
        if len(assignments) > 1000:
            raise HTTPException(
                status_code=400, detail="Maximum 1000 assignments per request"
            )

        results = await ab_testing_service.bulk_assign_variants(
            assignments=assignments,
            organization_id=current_user.organization_id,
            db=db,
            redis_client=redis_client,
        )

        return results

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to bulk assign experiment variants: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# METRICS COLLECTION ENDPOINTS
# ============================================================================


@router.post("/metrics", status_code=201)
async def submit_metric(
    metric_data: MetricSubmissionRequest,
    background_tasks: BackgroundTasks,
    current_user: Optional[User] = Depends(get_current_user),
    db=Depends(get_db),
    redis_client=Depends(get_redis_client),
):
    """
    Submit metric data for experiment analysis

    Endpoint for submitting performance metrics collected during query processing.
    Metrics are processed asynchronously to avoid impacting query latency.

    Args:
        metric_data: Metric data to submit
        background_tasks: FastAPI background tasks for async processing
        current_user: Authenticated user (if available)
        db: Database session
        redis_client: Redis client for caching

    Returns:
        Success response

    Raises:
        HTTPException: If metric submission fails
    """
    try:
        # Queue metric for async processing
        background_tasks.add_task(
            ab_testing_service.process_metric_async,
            metric_data=metric_data,
            user_id=current_user.id if current_user else metric_data.user_id,
            organization_id=(
                current_user.organization_id
                if current_user
                else metric_data.organization_id
            ),
            db=db,
            redis_client=redis_client,
        )

        logger.debug(f"Queued metric {metric_data.metric_type} for processing")

        return JSONResponse(
            status_code=201,
            content={
                "message": "Metric submitted for processing",
                "metric_id": str(uuid.uuid4()),
            },
        )

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to submit metric: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/metrics/bulk", status_code=201)
async def submit_metrics_bulk(
    metrics_data: BulkMetricSubmissionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
    redis_client=Depends(get_redis_client),
):
    """
    Submit multiple metrics in bulk

    Endpoint for efficiently submitting multiple metrics at once.

    Args:
        metrics_data: Bulk metric data
        background_tasks: FastAPI background tasks for async processing
        current_user: Authenticated user
        db: Database session
        redis_client: Redis client for caching

    Returns:
        Success response with processing summary

    Raises:
        HTTPException: If bulk submission fails
    """
    try:
        if len(metrics_data.metrics) > 10000:
            raise HTTPException(
                status_code=400, detail="Maximum 10000 metrics per bulk request"
            )

        # Queue metrics for async processing
        background_tasks.add_task(
            ab_testing_service.process_metrics_bulk_async,
            metrics_data=metrics_data,
            organization_id=current_user.organization_id,
            db=db,
            redis_client=redis_client,
        )

        logger.info(f"Queued {len(metrics_data.metrics)} metrics for bulk processing")

        return JSONResponse(
            status_code=201,
            content={
                "message": "Metrics submitted for bulk processing",
                "metrics_count": len(metrics_data.metrics),
                "batch_id": str(uuid.uuid4()),
            },
        )

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to submit bulk metrics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# STATISTICAL ANALYSIS ENDPOINTS
# ============================================================================


@router.post(
    "/experiments/{experiment_id}/analyze", response_model=StatisticalAnalysisResponse
)
async def analyze_experiment(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    analysis_request: StatisticalAnalysisRequest = ...,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Perform statistical analysis on experiment results

    Analyzes experiment data to determine statistical significance, effect sizes,
    and confidence intervals for the specified metrics.

    Args:
        experiment_id: UUID of the experiment
        analysis_request: Analysis configuration
        current_user: Authenticated user
        db: Database session

    Returns:
        Statistical analysis results

    Raises:
        HTTPException: If experiment not found or analysis fails
    """
    try:
        analysis_result = await statistical_analysis_service.analyze_experiment(
            experiment_id=experiment_id,
            analysis_request=analysis_request,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not analysis_result:
            raise HTTPException(status_code=404, detail="Experiment not found")

        logger.info(f"Completed statistical analysis for experiment {experiment_id}")

        return analysis_result

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to analyze experiment {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/experiments/{experiment_id}/summary", response_model=ExperimentSummaryResponse
)
async def get_experiment_summary(
    experiment_id: uuid.UUID = Path(..., description="Experiment ID"),
    summary_request: ExperimentSummaryRequest = ...,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Get experiment summary with key metrics and insights

    Provides a comprehensive summary of experiment performance including
    participant statistics, metric comparisons, and recommendations.

    Args:
        experiment_id: UUID of the experiment
        summary_request: Summary configuration
        current_user: Authenticated user
        db: Database session

    Returns:
        Experiment summary with insights

    Raises:
        HTTPException: If experiment not found or summary generation fails
    """
    try:
        summary = await ab_testing_service.get_experiment_summary(
            experiment_id=experiment_id,
            summary_request=summary_request,
            organization_id=current_user.organization_id,
            db=db,
        )

        if not summary:
            raise HTTPException(status_code=404, detail="Experiment not found")

        return summary

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get experiment summary {experiment_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# USER SEGMENTATION ENDPOINTS
# ============================================================================


@router.post("/segments", response_model=UserSegmentResponse, status_code=201)
async def create_user_segment(
    segment_data: UserSegmentCreateRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Create a new user segment

    Creates a user segment for targeted experiments based on specified criteria.

    Args:
        segment_data: Segment configuration details
        current_user: Authenticated user
        db: Database session

    Returns:
        Created segment details

    Raises:
        HTTPException: If segment creation fails
    """
    try:
        segment = await ab_testing_service.create_user_segment(
            segment_data=segment_data,
            creator_user_id=current_user.id,
            organization_id=current_user.organization_id,
            db=db,
        )

        logger.info(f"Created user segment {segment.id}: {segment.name}")

        return UserSegmentResponse.from_segment(segment)

    except ValueError:
        logger.warning("Invalid request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid request")
    except Exception as e:
        logger.error(f"Failed to create user segment: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/segments", response_model=List[UserSegmentResponse])
async def list_user_segments(
    segment_type: Optional[str] = Query(None, description="Filter by segment type"),
    limit: int = Query(
        default=50, ge=1, le=100, description="Number of segments to return"
    ),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    List user segments

    Retrieves user segments for the current user's organization.

    Args:
        segment_type: Filter by segment type
        limit: Maximum number of segments to return
        current_user: Authenticated user
        db: Database session

    Returns:
        List of user segments
    """
    try:
        segments = await ab_testing_service.list_user_segments(
            organization_id=current_user.organization_id,
            segment_type=segment_type,
            limit=limit,
            db=db,
        )

        return [UserSegmentResponse.from_segment(segment) for segment in segments]

    except Exception as e:
        logger.error(f"Failed to list user segments: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# HEALTH AND MONITORING ENDPOINTS
# ============================================================================


@router.get("/health")
async def ab_testing_health_check(
    db=Depends(get_db), redis_client=Depends(get_redis_client)
):
    """
    Health check for A/B testing system

    Checks the health of A/B testing components including database connectivity,
    Redis caching, and critical services.

    Returns:
        Health status information
    """
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "components": {},
        }

        # Check database connectivity
        try:
            db.execute(text("SELECT 1"))
            health_status["components"]["database"] = {
                "status": "healthy",
                "message": "Database connection successful",
            }
        except Exception as e:
            health_status["components"]["database"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        # Check Redis connectivity
        try:
            redis_client.ping()
            health_status["components"]["redis"] = {
                "status": "healthy",
                "message": "Redis connection successful",
            }
        except Exception as e:
            health_status["components"]["redis"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        # Check service availability
        try:
            # Test basic service functionality
            active_experiments = await ab_testing_service.get_active_experiments_count(
                db
            )
            health_status["components"]["ab_testing_service"] = {
                "status": "healthy",
                "active_experiments": active_experiments,
            }
        except Exception as e:
            health_status["components"]["ab_testing_service"] = {
                "status": "unhealthy",
                "error": str(e),
            }
            health_status["status"] = "degraded"

        return health_status

    except Exception as e:
        logger.error(f"A/B testing health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )


@router.get("/public/health")
async def public_ab_testing_health_check():
    """
    Public health check for A/B testing system

    Basic health check that doesn't require authentication.
    """
    try:
        return {
            "status": "healthy",
            "service": "A/B Testing System",
            "timestamp": datetime.utcnow().isoformat(),
            "message": "A/B testing API is accessible",
        }

    except Exception as e:
        logger.error(f"Public A/B testing health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
