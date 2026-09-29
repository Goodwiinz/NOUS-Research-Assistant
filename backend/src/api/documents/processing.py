"""
Processing pipeline API endpoints
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from src.core.database import get_db
from src.core.dependencies import (
    get_current_organization,
    get_current_user,
    require_admin,
)
from src.models.document import Document
from src.models.organization import Organization
from src.models.processing import JobStatus, ProcessingJob
from src.models.user import User, UserRole
from src.services.processing.processing_service import (
    ProcessingPipeline,
    get_processing_service,
)

router = APIRouter(prefix="/processing", tags=["processing"])


# Request/Response Models
class ProcessingJobResponse(BaseModel):
    id: UUID
    job_type: str
    status: str
    progress_percentage: float
    current_step: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    error_message: Optional[str]
    result: Optional[dict] = None


class ProcessingStatusResponse(BaseModel):
    document_id: str
    processing_status: str
    is_embedded: bool
    is_indexed: bool
    processing_error: Optional[str]
    jobs: List[ProcessingJobResponse]


class BatchProcessingRequest(BaseModel):
    document_ids: List[str] = Field(..., max_length=100)
    priority: Optional[str] = "normal"


class RetryProcessingRequest(BaseModel):
    job_ids: Optional[List[str]] = None
    organization_wide: bool = False


@router.post("/documents/{document_id}/process")
async def start_document_processing(
    document_id: UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    processing_service: ProcessingPipeline = Depends(get_processing_service),
):
    """Start processing for a document"""
    try:
        job = await processing_service.process_document(
            document_id=str(document_id),
            user_id=str(current_user.id),
            organization_id=str(organization.id),
        )

        return {
            "message": "Document processing started",
            "job_id": str(job.id),
            "status": job.status.value,
        }

    except ValueError:
        logger.warning("Invalid processing request", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid processing request",
        )
    except Exception:
        logger.error("Failed to start processing", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start processing",
        )


@router.get("/documents/{document_id}/status", response_model=ProcessingStatusResponse)
async def get_document_processing_status(
    document_id: UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    processing_service: ProcessingPipeline = Depends(get_processing_service),
    db: AsyncSession = Depends(get_db),
):
    """Get processing status for a document"""
    # Check if document belongs to user's organization
    doc_result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == organization.id,
            Document.is_deleted == False,
        )
    )
    document = doc_result.scalar_one_or_none()

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    status_info = await processing_service.get_processing_status(str(document_id))

    return ProcessingStatusResponse(**status_info)


@router.get("/jobs/{job_id}", response_model=ProcessingJobResponse)
async def get_processing_job_status(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Get status of a specific processing job"""
    job_result = await db.execute(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id, ProcessingJob.organization_id == organization.id
        )
    )
    job = job_result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Processing job not found"
        )

    return ProcessingJobResponse(**job.to_dict())


@router.get("/jobs")
async def list_processing_jobs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    job_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """List processing jobs for the organization"""
    conditions = [
        ProcessingJob.organization_id == organization.id,
        ProcessingJob.is_deleted == False,  # noqa: E712 — SQLAlchemy column comparison
    ]

    if status_filter:
        try:
            job_status = JobStatus(status_filter)
            conditions.append(ProcessingJob.status == job_status)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}",
            )

    if job_type:
        from src.models.processing import JobType

        try:
            job_type_enum = JobType(job_type)
            conditions.append(ProcessingJob.job_type == job_type_enum)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid job type: {job_type}",
            )

    total_result = await db.execute(
        select(func.count(ProcessingJob.id)).where(*conditions)
    )
    total = total_result.scalar_one()

    jobs_result = await db.execute(
        select(ProcessingJob)
        .where(*conditions)
        .order_by(ProcessingJob.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    jobs = jobs_result.scalars().all()

    serialized: List[ProcessingJobResponse] = []
    for job in jobs:
        try:
            serialized.append(ProcessingJobResponse(**job.to_dict()))
        except Exception:
            # One bad row (stale enum value, NULL in a non-nullable column,
            # JSON column with a non-serializable payload) must not 500 the
            # whole list. Log job.id so we can pinpoint it after deploy.
            logger.exception(
                "processing_jobs.serialize_failed",
                extra={"job_id": str(getattr(job, "id", None))},
            )

    return {
        "jobs": serialized,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/batch", response_model=dict)
async def start_batch_processing(
    request: BatchProcessingRequest,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    processing_service: ProcessingPipeline = Depends(get_processing_service),
    db: AsyncSession = Depends(get_db),
):
    """Start batch processing for multiple documents"""
    results = []
    errors = []

    # Verify all documents belong to the organization
    documents_result = await db.execute(
        select(Document).where(
            Document.id.in_(request.document_ids),
            Document.organization_id == organization.id,
            Document.is_deleted == False,
        )
    )
    documents = documents_result.scalars().all()

    found_document_ids = {str(doc.id) for doc in documents}
    missing_document_ids = set(request.document_ids) - found_document_ids

    if missing_document_ids:
        errors.extend(
            [
                {"document_id": doc_id, "error": "Document not found"}
                for doc_id in missing_document_ids
            ]
        )

    # Process found documents
    for document in documents:
        try:
            # Pass org explicitly (defense-in-depth) — these documents were
            # pre-filtered by org above, but process_document shouldn't rely on
            # the caller having scoped first; a future refactor of that filter
            # would otherwise silently reopen the id-only-lookup IDOR.
            job = await processing_service.process_document(
                document_id=str(document.id),
                user_id=str(current_user.id),
                organization_id=str(organization.id),
            )
            results.append(
                {
                    "document_id": str(document.id),
                    "job_id": str(job.id),
                    "status": "queued",
                }
            )
        except Exception:
            logger.error(
                "Batch processing: failed to queue document", exc_info=True
            )
            errors.append(
                {"document_id": str(document.id), "error": "Failed to queue document"}
            )

    return {
        "message": f"Batch processing initiated for {len(results)} documents",
        "successful": results,
        "errors": errors,
        "total_requested": len(request.document_ids),
        "successful_count": len(results),
        "error_count": len(errors),
    }


@router.post("/retry", response_model=dict)
async def retry_failed_jobs(
    request: RetryProcessingRequest,
    current_user: User = Depends(require_admin),
    organization: Organization = Depends(get_current_organization),
    processing_service: ProcessingPipeline = Depends(get_processing_service),
    db: AsyncSession = Depends(get_db),
):
    """Retry failed processing jobs"""
    try:
        if request.organization_wide:
            # Retry all failed jobs for the organization
            retried_count = await processing_service.retry_failed_jobs(
                organization_id=str(organization.id)
            )
            message = f"Retried {retried_count} failed jobs organization-wide"
        elif request.job_ids:
            # Retry specific jobs
            retried_count = 0
            for job_id in request.job_ids:
                job_result = await db.execute(
                    select(ProcessingJob).where(
                        ProcessingJob.id == job_id,
                        ProcessingJob.organization_id == organization.id,
                    )
                )
                job = job_result.scalar_one_or_none()

                if job and job.can_retry:
                    job.retry_job()
                    await db.commit()
                    await processing_service.queue_processing_job(job_id)
                    retried_count += 1

            message = f"Retried {retried_count} specified jobs"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either job_ids or organization_wide must be specified",
            )

        return {"message": message, "retried_count": retried_count}

    except HTTPException:
        raise
    except Exception:
        # get_db hands back the request-scoped session shared with the rest
        # of this request (multi_tenancy middleware) -- leaving it dirty
        # after a mid-loop failure poisons whatever runs next on it.
        await db.rollback()
        logger.error("Failed to retry jobs", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retry jobs",
        )


@router.post("/jobs/{job_id}/cancel")
async def cancel_processing_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a processing job"""
    job_result = await db.execute(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id, ProcessingJob.organization_id == organization.id
        )
    )
    job = job_result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Processing job not found"
        )

    if not job.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job is not active and cannot be cancelled",
        )

    # Check permissions (job owner or admin)
    if job.created_by_user_id != current_user.id and not current_user.has_permission(
        UserRole.ADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only cancel your own jobs or require admin role",
        )

    try:
        # Cancel the job
        job.cancel_job()
        await db.commit()

        # Also try to cancel the Celery task if it exists
        if job.celery_task_id:
            from src.tasks.processing_tasks import current_app

            current_app.control.revoke(job.celery_task_id, terminate=True)

        return {
            "message": "Job cancelled successfully",
            "job_id": str(job.id),
            "status": job.status.value,
        }

    except Exception:
        await db.rollback()
        logger.error("Failed to cancel job", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel job",
        )


@router.get("/queue/status")
async def get_queue_status(
    current_user: User = Depends(require_admin),  # Admin only
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Get processing queue status (admin only)"""
    try:
        from src.models.processing import JobStatus, JobType

        async def _count(*conditions) -> int:
            result = await db.execute(
                select(func.count(ProcessingJob.id)).where(*conditions)
            )
            return result.scalar_one()

        # Get queue statistics
        queue_stats = {}

        for job_type in JobType:
            queued_count = await _count(
                ProcessingJob.job_type == job_type,
                ProcessingJob.status == JobStatus.QUEUED,
                ProcessingJob.organization_id == organization.id,
            )

            running_count = await _count(
                ProcessingJob.job_type == job_type,
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.organization_id == organization.id,
            )

            queue_stats[job_type.value] = {
                "queued": queued_count,
                "running": running_count,
                "total": queued_count + running_count,
            }

        # Get overall statistics
        total_queued = await _count(
            ProcessingJob.status == JobStatus.QUEUED,
            ProcessingJob.organization_id == organization.id,
        )

        total_running = await _count(
            ProcessingJob.status == JobStatus.RUNNING,
            ProcessingJob.organization_id == organization.id,
        )

        total_failed = await _count(
            ProcessingJob.status == JobStatus.FAILED,
            ProcessingJob.organization_id == organization.id,
        )

        return {
            "queue_stats": queue_stats,
            "overall_stats": {
                "queued": total_queued,
                "running": total_running,
                "failed": total_failed,
                "total": total_queued + total_running + total_failed,
            },
        }

    except Exception:
        logger.error("Failed to get queue status", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get queue status",
        )


@router.delete("/jobs/cleanup")
async def cleanup_old_jobs(
    days: int = 30,
    current_user: User = Depends(require_admin),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """Clean up old processing jobs (admin only)"""
    try:
        from datetime import timedelta

        cutoff_date = datetime.utcnow() - timedelta(days=days)

        old_jobs_result = await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.created_at < cutoff_date,
                ProcessingJob.organization_id == organization.id,
                ProcessingJob.status.in_(
                    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
                ),
            )
        )
        old_jobs = old_jobs_result.scalars().all()

        for job in old_jobs:
            await db.delete(job)

        await db.commit()

        return {
            "message": f"Cleaned up {len(old_jobs)} old processing jobs",
            "cleaned_count": len(old_jobs),
            "cutoff_days": days,
        }

    except Exception:
        await db.rollback()
        logger.error("Failed to cleanup jobs", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup jobs",
        )
