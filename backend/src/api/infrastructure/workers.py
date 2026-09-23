"""
Celery workers monitoring API endpoints
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.core.dependencies import require_platform_operator
from src.models.user import User

router = APIRouter(prefix="/workers", tags=["workers"])


# Response Models
class WorkerInfo(BaseModel):
    """Information about a single Celery worker"""

    hostname: str
    status: str
    active_tasks: int
    processed_tasks: int
    queues: List[str]


class WorkerStatsResponse(BaseModel):
    """Celery worker statistics"""

    active_workers: int
    total_workers: int
    active_tasks: int
    pending_tasks: int
    registered_tasks: int
    worker_details: List[Dict[str, Any]]


class QueueStatsResponse(BaseModel):
    """Queue statistics"""

    queue_name: str
    pending_messages: int
    active_consumers: int


@router.get("/status", response_model=WorkerStatsResponse)
async def get_worker_status(current_user: User = Depends(require_platform_operator)):
    """
    Get Celery worker status and statistics

    Returns information about:
    - Number of active workers
    - Active tasks being processed
    - Pending tasks in queues
    - Registered task types
    """
    try:
        from src.tasks.processing_tasks import celery_app

        # Get worker stats from Celery
        inspect = celery_app.control.inspect()

        # Get active workers
        active_workers_data = inspect.active()
        registered_tasks_data = inspect.registered()
        stats_data = inspect.stats()
        reserved_data = inspect.reserved()

        # Initialize response
        active_workers = 0
        total_workers = 0
        active_tasks = 0
        pending_tasks = 0
        worker_details = []
        registered_tasks_set = set()

        # Process active workers
        if active_workers_data:
            total_workers = len(active_workers_data)
            active_workers = (
                total_workers  # All responding workers are considered active
            )

            for worker_name, tasks in active_workers_data.items():
                # Count active tasks for this worker
                worker_active_tasks = len(tasks) if tasks else 0
                active_tasks += worker_active_tasks

                # Get worker stats
                worker_stats = stats_data.get(worker_name, {}) if stats_data else {}
                worker_registered = (
                    registered_tasks_data.get(worker_name, [])
                    if registered_tasks_data
                    else []
                )
                worker_reserved = (
                    reserved_data.get(worker_name, []) if reserved_data else []
                )

                # Add to registered tasks set
                if worker_registered:
                    registered_tasks_set.update(worker_registered)

                # Count pending (reserved) tasks
                worker_pending_tasks = len(worker_reserved) if worker_reserved else 0
                pending_tasks += worker_pending_tasks

                worker_info = {
                    "hostname": worker_name,
                    "status": "online",
                    "active_tasks": worker_active_tasks,
                    "pending_tasks": worker_pending_tasks,
                    "total_processed": worker_stats.get("total", {}).get(
                        "tasks.completed", 0
                    ),
                    "pool": worker_stats.get("pool", {}),
                    "queues": worker_stats.get("broker", {}).get("queues", []),
                    "registered_tasks": len(worker_registered),
                }
                worker_details.append(worker_info)

        return WorkerStatsResponse(
            active_workers=active_workers,
            total_workers=total_workers,
            active_tasks=active_tasks,
            pending_tasks=pending_tasks,
            registered_tasks=len(registered_tasks_set),
            worker_details=worker_details,
        )

    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Celery is not configured or not available",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get worker status: {str(e)}",
        )


@router.get("/queues", response_model=List[QueueStatsResponse])
async def get_queue_status(current_user: User = Depends(require_platform_operator)):
    """
    Get queue statistics (admin only)

    Returns information about:
    - Pending messages in each queue
    - Active consumers for each queue
    """
    try:
        from src.tasks.processing_tasks import celery_app

        # Get queue names from worker configuration
        queues = [
            "document_processing",
            "text_processing",
            "vector_processing",
            "entity_processing",
            "graph_processing",
        ]

        queue_stats = []

        # Try to get queue stats from broker
        try:
            # For Redis broker
            from celery.app.control import Inspect

            inspect = celery_app.control.inspect()
            active_queues = inspect.active_queues()

            if active_queues:
                # Build queue stats from active queues info
                for worker_name, worker_queues in active_queues.items():
                    for queue_info in worker_queues:
                        queue_name = queue_info.get("name", "")
                        if queue_name and queue_name not in [
                            q.queue_name for q in queue_stats
                        ]:
                            queue_stats.append(
                                QueueStatsResponse(
                                    queue_name=queue_name,
                                    pending_messages=0,  # Would need broker connection to get this
                                    active_consumers=1,
                                )
                            )
            else:
                # Fallback: return configured queues
                for queue_name in queues:
                    queue_stats.append(
                        QueueStatsResponse(
                            queue_name=queue_name,
                            pending_messages=0,
                            active_consumers=0,
                        )
                    )

        except Exception as e:
            # Fallback: return configured queues with minimal info
            for queue_name in queues:
                queue_stats.append(
                    QueueStatsResponse(
                        queue_name=queue_name, pending_messages=0, active_consumers=0
                    )
                )

        return queue_stats

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get queue status: {str(e)}",
        )


@router.post("/ping")
async def ping_workers(current_user: User = Depends(require_platform_operator)):
    """
    Ping all workers to check connectivity (admin only)

    Returns list of workers that responded
    """
    try:
        from src.tasks.processing_tasks import celery_app

        # Ping all workers
        result = celery_app.control.ping(timeout=1.0)

        if result:
            responding_workers = []
            for worker_response in result:
                for worker_name, pong in worker_response.items():
                    responding_workers.append(
                        {
                            "worker": worker_name,
                            "response": pong,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )

            return {
                "message": f"{len(responding_workers)} workers responded",
                "workers": responding_workers,
                "total": len(responding_workers),
            }
        else:
            return {"message": "No workers responded", "workers": [], "total": 0}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to ping workers: {str(e)}",
        )


@router.get("/registered-tasks")
async def get_registered_tasks(
    current_user: User = Depends(require_platform_operator),
):
    """
    Get list of all registered tasks across all workers (admin only)
    """
    try:
        from src.tasks.processing_tasks import celery_app

        inspect = celery_app.control.inspect()
        registered = inspect.registered()

        if registered:
            # Combine tasks from all workers
            all_tasks = set()
            for worker_name, tasks in registered.items():
                all_tasks.update(tasks)

            return {
                "total_tasks": len(all_tasks),
                "tasks": sorted(list(all_tasks)),
                "workers": len(registered),
            }
        else:
            return {"total_tasks": 0, "tasks": [], "workers": 0}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get registered tasks: {str(e)}",
        )


@router.post("/shutdown/{worker_name}")
async def shutdown_worker(
    worker_name: str, current_user: User = Depends(require_platform_operator)
):
    """
    Shutdown a specific worker (admin only)

    WARNING: This will terminate the worker process
    """
    try:
        from src.tasks.processing_tasks import celery_app

        # Send shutdown command to specific worker
        celery_app.control.shutdown(destination=[worker_name])

        return {
            "message": f"Shutdown command sent to {worker_name}",
            "worker": worker_name,
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to shutdown worker: {str(e)}",
        )


@router.get("/health")
async def get_workers_health(current_user: User = Depends(require_platform_operator)):
    """
    Get overall health status of worker infrastructure

    Returns:
    - healthy: bool - Whether the worker infrastructure is healthy
    - workers_online: int - Number of workers online
    - issues: List[str] - List of any issues detected
    """
    try:
        from src.tasks.processing_tasks import celery_app

        inspect = celery_app.control.inspect()

        # Check if any workers are active
        active_workers = inspect.active()
        stats = inspect.stats()

        workers_online = 0
        issues = []

        if active_workers:
            workers_online = len(active_workers)
        else:
            issues.append("No workers are currently online")

        # Check if workers are processing tasks
        if stats:
            for worker_name, worker_stats in stats.items():
                pool_info = worker_stats.get("pool", {})
                max_concurrency = pool_info.get("max-concurrency", 0)

                if max_concurrency == 0:
                    issues.append(f"Worker {worker_name} has zero concurrency")

        healthy = workers_online > 0 and len(issues) == 0

        return {
            "healthy": healthy,
            "workers_online": workers_online,
            "issues": issues,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        return {
            "healthy": False,
            "workers_online": 0,
            "issues": [f"Failed to check worker health: {str(e)}"],
            "timestamp": datetime.utcnow().isoformat(),
        }
