"""
Real-time document processing status API endpoints
Enhanced with WebSocket integration and live progress tracking
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user
from src.models.document import Document
from src.models.document import ProcessingStatus as DocumentProcessingStatus
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.models.user import User
from src.models.websocket_status import StatusUpdate, UpdateType
from src.services.infrastructure.status_update_service import (
    Channel,
    ProcessingProgress,
    UpdateFrequency,
    status_update_service,
)
from src.services.websocket.websocket_manager import (
    MessageType,
    Priority,
    WebSocketMessage,
    connection_manager,
)
from src.shared.schemas import DocumentResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/realtime", tags=["realtime-processing"])


# Enhanced request/response models
class RealtimeDocumentStatusRequest(BaseModel):
    """Request for real-time document status updates"""

    document_ids: List[str] = Field(..., description="List of document IDs to track")
    include_progress: bool = Field(
        default=True, description="Include detailed progress information"
    )
    update_frequency: UpdateFrequency = Field(
        default=UpdateFrequency.NORMAL, description="Update frequency"
    )
    auto_refresh: bool = Field(default=False, description="Enable automatic refresh")


class ProcessingStageInfo(BaseModel):
    """Information about a processing stage"""

    id: str
    name: str
    description: str
    status: str
    progress: float
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class EnhancedDocumentStatus(BaseModel):
    """Enhanced document processing status with real-time information"""

    id: str
    filename: str
    title: str
    document_type: str
    file_size_bytes: int
    processing_status: str
    overall_progress: float
    current_stage: Optional[ProcessingStageInfo] = None
    stages: List[ProcessingStageInfo] = []
    upload_progress: float
    processing_started_at: Optional[str] = None
    processing_completed_at: Optional[str] = None
    estimated_completion_time: Optional[str] = None
    processing_error: Optional[str] = None
    retry_count: int
    can_retry: bool
    actions: Dict[str, bool]
    created_at: str
    updated_at: str

    # Real-time specific fields
    websocket_subscribers: int = 0
    last_status_update: Optional[str] = None
    update_frequency: Optional[str] = None
    is_realtime_enabled: bool = False


class RealtimeStatusSubscription(BaseModel):
    """Real-time status subscription configuration"""

    document_id: str
    channels: List[str] = Field(default=[Channel.DOCUMENT_PROCESSING.value])
    frequency: UpdateFrequency = Field(default=UpdateFrequency.NORMAL)
    filters: Optional[Dict[str, Any]] = None


class BulkStatusRequest(BaseModel):
    """Request for bulk document status"""

    document_ids: List[str] = Field(
        ..., max_length=100, description="List of document IDs"
    )
    include_jobs: bool = Field(
        default=False, description="Include processing job information"
    )
    include_stages: bool = Field(
        default=True, description="Include processing stage details"
    )
    group_by_status: bool = Field(default=False, description="Group results by status")


class SystemMetricsResponse(BaseModel):
    """System-wide processing metrics"""

    active_connections: int
    total_documents: int
    queued_documents: int
    processing_documents: int
    completed_documents_today: int
    failed_documents_today: int
    average_processing_time_seconds: float
    system_load_percentage: float
    websocket_connections_by_channel: Dict[str, int]
    messages_sent_last_hour: int
    error_rate_last_hour: float
    timestamp: str


@router.post("/documents/status/subscribe")
async def subscribe_document_status_updates(
    subscription: RealtimeStatusSubscription,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_db),
):
    """
    Subscribe to real-time status updates for a specific document

    This endpoint configures WebSocket subscriptions for document processing updates.
    The client will receive real-time updates through their WebSocket connection.
    """
    try:
        # Verify document belongs to user's organization
        result = await session.execute(
            select(Document).where(
                and_(
                    Document.id == subscription.document_id,
                    Document.organization_id == organization.id,
                    Document.is_deleted == False,
                )
            )
        )
        document = result.scalar_one_or_none()

        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )

        # Get user's WebSocket connections
        user_connections = connection_manager.get_user_connections(str(current_user.id))

        if not user_connections:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active WebSocket connections found. Please connect to WebSocket first.",
            )

        # Subscribe each connection to the document updates
        subscribed_connections = 0
        for connection_id in user_connections:
            # Subscribe to document processing channel
            await connection_manager.subscribe_to_channel(
                connection_id, Channel.DOCUMENT_PROCESSING.value
            )

            # Subscribe to specific document updates in status service
            await status_update_service.subscribe_to_updates(
                connection_id=connection_id,
                update_types=[Channel.DOCUMENT_PROCESSING.value],
                frequency=subscription.frequency,
            )

            subscribed_connections += 1

        # Send immediate status update
        await status_update_service.broadcast_document_update(
            document_id=subscription.document_id, status=document.processing_status
        )

        return {
            "success": True,
            "message": f"Subscribed to real-time updates for document {subscription.document_id}",
            "document_id": subscription.document_id,
            "subscribed_connections": subscribed_connections,
            "channels": subscription.channels,
            "frequency": subscription.frequency.value,
            "websocket_endpoint": f"{settings.API_URL}/api/v2/ws/connect",
        }

    except HTTPException:
        raise
    except Exception:
        logger.error("Error subscribing to document updates", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to subscribe to document updates",
        )


@router.get("/documents/{document_id}/status", response_model=EnhancedDocumentStatus)
async def get_realtime_document_status(
    document_id: str,
    include_progress: bool = Query(
        True, description="Include detailed progress information"
    ),
    include_stages: bool = Query(True, description="Include processing stage details"),
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_db),
):
    """
    Get enhanced real-time status for a specific document

    Returns comprehensive status information including processing stages,
    progress metrics, and real-time connection information.
    """
    try:
        # Get document with relationships
        result = await session.execute(
            select(Document)
            .options(selectinload(Document.uploaded_by_user))
            .options(selectinload(Document.processing_jobs))
            .where(
                and_(
                    Document.id == document_id,
                    Document.organization_id == organization.id,
                    Document.is_deleted == False,
                )
            )
        )
        document = result.scalar_one_or_none()

        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )

        # Get processing jobs for this document
        jobs_result = await session.execute(
            select(ProcessingJob)
            .where(ProcessingJob.document_id == document_id)
            .order_by(desc(ProcessingJob.created_at))
        )
        jobs = jobs_result.scalars().all()

        # Get active WebSocket subscribers for this document
        user_connections = connection_manager.get_user_connections(
            str(document.uploaded_by_user_id)
        )
        active_subscribers = len(
            [
                conn_id
                for conn_id in user_connections
                if connection_manager.active_connections.get(conn_id)
            ]
        )

        # Get latest status update for this document
        latest_update_result = await session.execute(
            select(StatusUpdate)
            .where(
                and_(
                    StatusUpdate.update_data.op("->>")("document_id") == document_id,
                    StatusUpdate.created_at >= datetime.utcnow() - timedelta(hours=24),
                )
            )
            .order_by(desc(StatusUpdate.created_at))
            .limit(1)
        )
        latest_update = latest_update_result.scalar_one_or_none()

        # Build processing stages information
        stages = []
        current_stage = None

        if include_stages and jobs:
            # Create stage information from jobs
            stage_order = {
                JobType.OCR_PROCESSING: 1,
                JobType.TEXT_EXTRACTION: 2,
                JobType.EMBEDDING_GENERATION: 3,
                JobType.VECTOR_INDEXING: 4,
                JobType.ENTITY_EXTRACTION: 5,
                JobType.KNOWLEDGE_GRAPH: 6,
            }

            for job in sorted(jobs, key=lambda j: stage_order.get(j.job_type, 999)):
                stage = ProcessingStageInfo(
                    id=str(job.id),
                    name=job.job_type.value.replace("_", " ").title(),
                    description=f"Processing {job.job_type.value.replace('_', ' ').lower()}",
                    status=job.status.value,
                    progress=job.progress_percentage,
                    started_at=job.started_at.isoformat() if job.started_at else None,
                    completed_at=(
                        job.completed_at.isoformat() if job.completed_at else None
                    ),
                    duration_seconds=job.duration_seconds,
                    error=job.error_message,
                    metadata={
                        "job_type": job.job_type.value,
                        "priority": job.priority.value,
                        "retry_count": job.retry_count,
                        "worker_id": job.worker_id,
                    },
                )
                stages.append(stage)

                if job.status in [JobStatus.RUNNING, JobStatus.QUEUED]:
                    current_stage = stage

        # Calculate overall progress
        overall_progress = 0.0
        if document.processing_status == DocumentProcessingStatus.COMPLETED:
            overall_progress = 100.0
        elif document.processing_status == DocumentProcessingStatus.PROCESSING:
            if jobs:
                overall_progress = sum(job.progress_percentage for job in jobs) / len(
                    jobs
                )
            else:
                overall_progress = 0.0
        elif document.processing_status == DocumentProcessingStatus.FAILED:
            overall_progress = 0.0

        # Estimate completion time
        estimated_completion = None
        if overall_progress > 0 and overall_progress < 100:
            # processing_started_at is timezone-aware (DateTime(timezone=True));
            # naive utcnow() minus an aware datetime raises TypeError, 500ing
            # this endpoint for any document mid-processing.
            now = datetime.now(dt_timezone.utc)
            started_at = document.processing_started_at
            if started_at is not None and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=dt_timezone.utc)
            elapsed_time = (now - started_at).total_seconds() if started_at else 0
            if elapsed_time > 0:
                estimated_total_time = elapsed_time * (100 / overall_progress)
                estimated_completion = now + timedelta(seconds=estimated_total_time)
                estimated_completion = estimated_completion.isoformat()

        # Build response
        enhanced_status = EnhancedDocumentStatus(
            id=str(document.id),
            filename=document.filename,
            title=document.title,
            document_type=document.document_type.value,
            file_size_bytes=document.file_size_bytes,
            processing_status=document.processing_status.value,
            overall_progress=overall_progress,
            current_stage=current_stage,
            stages=stages,
            upload_progress=100.0,  # Assume upload is complete for processing status
            processing_started_at=(
                document.processing_started_at.isoformat()
                if document.processing_started_at
                else None
            ),
            processing_completed_at=(
                document.processing_completed_at.isoformat()
                if document.processing_completed_at
                else None
            ),
            estimated_completion_time=estimated_completion,
            processing_error=document.processing_error,
            retry_count=document.processing_retry_count,
            can_retry=document.can_retry_processing(),
            actions={
                "pause": document.processing_status
                == DocumentProcessingStatus.PROCESSING,
                "resume": document.processing_status
                == DocumentProcessingStatus.PENDING,
                "cancel": document.processing_status
                in [
                    DocumentProcessingStatus.PENDING,
                    DocumentProcessingStatus.PROCESSING,
                ],
                "retry": document.can_retry_processing(),
                "download": document.processing_status
                == DocumentProcessingStatus.COMPLETED,
            },
            created_at=document.created_at.isoformat(),
            updated_at=document.updated_at.isoformat(),
            websocket_subscribers=active_subscribers,
            last_status_update=(
                latest_update.created_at.isoformat() if latest_update else None
            ),
            update_frequency="realtime" if active_subscribers > 0 else None,
            is_realtime_enabled=active_subscribers > 0,
        )

        return enhanced_status

    except HTTPException:
        raise
    except Exception:
        logger.error("Error getting realtime document status", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get document status",
        )


@router.post("/documents/bulk/status")
async def get_bulk_realtime_status(
    request: BulkStatusRequest,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_db),
):
    """
    Get real-time status for multiple documents in bulk

    Optimized for dashboard views and bulk status updates.
    """
    try:
        # Get documents
        result = await session.execute(
            select(Document).where(
                and_(
                    Document.id.in_(request.document_ids),
                    Document.organization_id == organization.id,
                    Document.is_deleted == False,
                )
            )
        )
        documents = result.scalars().all()

        found_document_ids = {str(doc.id) for doc in documents}
        missing_document_ids = set(request.document_ids) - found_document_ids

        # Build status responses
        document_statuses = {}

        # Fetch jobs for every found document in ONE query instead of one
        # query per document (N+1 — with the 100-id cap this was up to 100
        # round trips per request) and group the results in python, keeping
        # only the 5 most recent per document to match the old per-document
        # limit.
        jobs_by_document_id: Dict[str, List[ProcessingJob]] = {}
        if request.include_jobs and documents:
            jobs_result = await session.execute(
                select(ProcessingJob)
                .where(ProcessingJob.document_id.in_([doc.id for doc in documents]))
                .order_by(desc(ProcessingJob.created_at))
            )
            for job in jobs_result.scalars().all():
                jobs_by_document_id.setdefault(str(job.document_id), []).append(job)

        for document in documents:
            # Get basic status without full details for performance
            document_statuses[str(document.id)] = {
                "id": str(document.id),
                "filename": document.filename,
                "title": document.title,
                "document_type": document.document_type.value,
                "processing_status": document.processing_status.value,
                "processing_started_at": (
                    document.processing_started_at.isoformat()
                    if document.processing_started_at
                    else None
                ),
                "processing_completed_at": (
                    document.processing_completed_at.isoformat()
                    if document.processing_completed_at
                    else None
                ),
                "processing_error": document.processing_error,
                "retry_count": document.processing_retry_count,
                "updated_at": document.updated_at.isoformat(),
            }

            # Add job information if requested
            if request.include_jobs:
                jobs = jobs_by_document_id.get(str(document.id), [])[:5]

                document_statuses[str(document.id)]["jobs"] = [
                    {
                        "id": str(job.id),
                        "job_type": job.job_type.value,
                        "status": job.status.value,
                        "progress_percentage": job.progress_percentage,
                        "current_step": job.current_step,
                    }
                    for job in jobs
                ]

        response = {
            "documents": document_statuses,
            "total_requested": len(request.document_ids),
            "found_count": len(found_document_ids),
            "missing_count": len(missing_document_ids),
            "missing_document_ids": list(missing_document_ids),
        }

        # Group by status if requested
        if request.group_by_status:
            status_groups = {}
            for doc_status in document_statuses.values():
                doc_status_value = doc_status["processing_status"]
                if doc_status_value not in status_groups:
                    status_groups[doc_status_value] = []
                status_groups[doc_status_value].append(doc_status["id"])

            response["status_groups"] = status_groups

        return response

    except Exception:
        logger.error("Error getting bulk realtime status", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get bulk status",
        )


@router.get("/system/metrics", response_model=SystemMetricsResponse)
async def get_realtime_system_metrics(
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_db),
):
    """
    Get real-time system-wide processing metrics

    Returns comprehensive system status including WebSocket connections,
    processing statistics, and performance metrics.
    """
    try:
        # Get system status from status update service
        system_status = await status_update_service.get_system_status()

        # Get WebSocket connection statistics
        ws_stats = connection_manager.get_connection_stats()

        # Get document statistics for the organization
        current_time = datetime.utcnow()
        today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)

        # Total documents
        total_docs_result = await session.execute(
            select(func.count(Document.id)).where(
                and_(
                    Document.organization_id == organization.id,
                    Document.is_deleted == False,
                )
            )
        )
        total_documents = total_docs_result.scalar() or 0

        # Documents by status
        queued_docs_result = await session.execute(
            select(func.count(Document.id)).where(
                and_(
                    Document.organization_id == organization.id,
                    Document.processing_status == DocumentProcessingStatus.PENDING,
                    Document.is_deleted == False,
                )
            )
        )
        queued_documents = queued_docs_result.scalar() or 0

        processing_docs_result = await session.execute(
            select(func.count(Document.id)).where(
                and_(
                    Document.organization_id == organization.id,
                    Document.processing_status == DocumentProcessingStatus.PROCESSING,
                    Document.is_deleted == False,
                )
            )
        )
        processing_documents = processing_docs_result.scalar() or 0

        # Completed today
        completed_today_result = await session.execute(
            select(func.count(Document.id)).where(
                and_(
                    Document.organization_id == organization.id,
                    Document.processing_status == DocumentProcessingStatus.COMPLETED,
                    Document.processing_completed_at >= today_start,
                    Document.is_deleted == False,
                )
            )
        )
        completed_documents_today = completed_today_result.scalar() or 0

        # Failed today
        failed_today_result = await session.execute(
            select(func.count(Document.id)).where(
                and_(
                    Document.organization_id == organization.id,
                    Document.processing_status == DocumentProcessingStatus.FAILED,
                    Document.processing_completed_at >= today_start,
                    Document.is_deleted == False,
                )
            )
        )
        failed_documents_today = failed_today_result.scalar() or 0

        # Recent status updates (last hour) for message rate
        recent_updates_result = await session.execute(
            select(func.count(StatusUpdate.id)).where(
                and_(
                    StatusUpdate.target_organizations.contains([str(organization.id)]),
                    StatusUpdate.created_at >= current_time - timedelta(hours=1),
                )
            )
        )
        messages_last_hour = recent_updates_result.scalar() or 0

        # Calculate error rate (placeholder - would need proper error tracking)
        error_rate_last_hour = 0.0

        metrics = SystemMetricsResponse(
            active_connections=ws_stats["total_connections"],
            total_documents=total_documents,
            queued_documents=queued_documents,
            processing_documents=processing_documents,
            completed_documents_today=completed_documents_today,
            failed_documents_today=failed_documents_today,
            average_processing_time_seconds=system_status.average_processing_time_seconds,
            system_load_percentage=system_status.system_load_percentage,
            websocket_connections_by_channel=ws_stats["channel_subscriptions"],
            messages_sent_last_hour=messages_last_hour,
            error_rate_last_hour=error_rate_last_hour,
            timestamp=datetime.utcnow().isoformat(),
        )

        return metrics

    except Exception:
        logger.error("Error getting system metrics", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get system metrics",
        )


@router.post("/documents/{document_id}/broadcast-status")
async def trigger_document_status_broadcast(
    document_id: str,
    current_user: User = Depends(get_current_user),
    organization: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_db),
):
    """
    Trigger a manual status broadcast for a document

    Useful for refreshing client status or testing WebSocket connectivity.
    """
    try:
        result = await session.execute(
            select(Document).where(
                and_(
                    Document.id == document_id,
                    Document.organization_id == organization.id,
                    Document.is_deleted == False,
                )
            )
        )
        document = result.scalar_one_or_none()

        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
            )

        # Broadcast status update
        await status_update_service.broadcast_document_update(
            document_id=document_id, status=document.processing_status
        )

        return {
            "success": True,
            "message": f"Status broadcast triggered for document {document_id}",
            "document_id": document_id,
            "status": document.processing_status.value,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except HTTPException:
        raise
    except Exception:
        logger.error("Error triggering status broadcast", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to trigger status broadcast",
        )


@router.get("/connections/status")
async def get_realtime_connection_status(
    current_user: User = Depends(get_current_user),
):
    """
    Get real-time connection status for the current user

    Returns information about active WebSocket connections and subscriptions.
    """
    try:
        user_connections = connection_manager.get_user_connections(str(current_user.id))

        connection_details = []
        total_subscribers = 0

        for connection_id in user_connections:
            if connection_id in connection_manager.active_connections:
                conn_info = connection_manager.active_connections[connection_id]

                connection_details.append(
                    {
                        "connection_id": connection_id,
                        "connected_at": conn_info.connected_at.isoformat(),
                        "last_heartbeat": conn_info.last_heartbeat.isoformat(),
                        "subscribed_channels": list(conn_info.subscribed_channels),
                        "client_info": conn_info.client_info,
                        "message_filter": conn_info.message_filter,
                    }
                )

                total_subscribers += len(conn_info.subscribed_channels)

        return {
            "user_id": str(current_user.id),
            "active_connections": len(connection_details),
            "total_subscriptions": total_subscribers,
            "connections": connection_details,
            "websocket_endpoint": f"{settings.API_URL}/api/v2/ws/connect",
            "available_channels": [
                {
                    "name": channel.value,
                    "description": channel.value.replace("_", " ").title(),
                }
                for channel in Channel
            ],
        }

    except Exception:
        logger.error("Error getting connection status", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get connection status",
        )
