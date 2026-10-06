"""
Reports API routes
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import update

from src.core.database import get_async_session
from src.core.dependencies import get_current_user
from src.models.analytics.analytics_models import AnalyticsReport
from src.models.user import User
from src.services.analytics.report_service import report_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["analytics-reports"])


# response_model omitted: endpoints return the AnalyticsReport ORM object, which
# is not a valid Pydantic response field (no report response schema exists).
@router.post("/", response_model=None, status_code=status.HTTP_201_CREATED)
async def create_report(
    name: str,
    title: str,
    report_config: Dict[str, Any],
    description: Optional[str] = None,
    schedule_config: Optional[Dict[str, Any]] = None,
    output_format: str = Query("pdf", pattern="^(pdf|csv|xlsx|json)$"),
    delivery_config: Optional[Dict[str, Any]] = None,
    current_user: User = Depends(get_current_user),
):
    """Create a new analytics report"""
    try:
        report = await report_service.create_report(
            name=name,
            title=title,
            description=description,
            owner_id=current_user.id,
            organization_id=current_user.organization_id,
            report_config=report_config,
            schedule_config=schedule_config,
            output_format=output_format,
            delivery_config=delivery_config,
        )
        return report

    except Exception as e:
        logger.error(f"Error creating report: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create report",
        )


@router.get("/", response_model=None)
async def list_reports(
    limit: int = Query(50, ge=1, le=100, description="Number of reports to return"),
    offset: int = Query(0, ge=0, description="Number of reports to skip"),
    current_user: User = Depends(get_current_user),
):
    """List analytics reports"""
    try:
        reports = await report_service.list_reports(
            user_id=current_user.id,
            organization_id=current_user.organization_id,
            limit=limit,
            offset=offset,
        )
        return reports

    except Exception as e:
        logger.error(f"Error listing reports: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list reports",
        )


@router.get("/{report_id}", response_model=None)
async def get_report(
    report_id: uuid.UUID, current_user: User = Depends(get_current_user)
):
    """Get report by ID"""
    try:
        report = await report_service.get_report(
            report_id=report_id, user_id=current_user.id
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )
        return report

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting report {report_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get report",
        )


@router.post("/{report_id}/generate", status_code=status.HTTP_200_OK)
async def generate_report(
    report_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    force: bool = Query(
        False, description="Force regeneration even if recently generated"
    ),
    current_user: User = Depends(get_current_user),
):
    """Generate a report"""
    try:
        # Check if report exists and user has permission
        report = await report_service.get_report(
            report_id=report_id, user_id=current_user.id
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )

        # Run generation in background
        background_tasks.add_task(
            report_service.generate_report, report_id=report_id, force=force
        )

        return {"message": "Report generation started", "report_id": str(report_id)}

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error starting report generation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start report generation",
        )


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: uuid.UUID, current_user: User = Depends(get_current_user)
):
    """Delete report"""
    try:
        success = await report_service.delete_report(
            report_id=report_id, user_id=current_user.id
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error deleting report {report_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete report",
        )


@router.get("/templates", response_model=Dict[str, Any])
async def get_report_templates(current_user: User = Depends(get_current_user)):
    """Get available report templates"""
    try:
        templates = {
            "executive_summary": {
                "name": "Executive Summary",
                "description": "High-level overview for executives",
                "sections": [
                    {
                        "type": "summary",
                        "title": "Executive Summary",
                        "dashboard_id": None,
                    },
                    {
                        "type": "metrics",
                        "title": "Key Performance Indicators",
                        "metrics": [
                            {
                                "name": "Total Users",
                                "type": "counter",
                                "time_range": "30d",
                            },
                            {
                                "name": "Active Users",
                                "type": "gauge",
                                "time_range": "7d",
                            },
                        ],
                    },
                    {
                        "type": "charts",
                        "title": "Growth Trends",
                        "charts": [
                            {
                                "type": "line",
                                "title": "User Growth",
                                "data_source": "users_over_time",
                            }
                        ],
                    },
                ],
            },
            "analytics_deep_dive": {
                "name": "Analytics Deep Dive",
                "description": "Detailed analytics report",
                "sections": [
                    {
                        "type": "dashboard",
                        "title": "Analytics Dashboard",
                        "dashboard_id": None,
                    },
                    {
                        "type": "tables",
                        "title": "Top Performing Content",
                        "tables": [
                            {
                                "title": "Most Viewed Documents",
                                "data_source": "document_views",
                            }
                        ],
                    },
                ],
            },
            "graph_analysis": {
                "name": "Graph Analysis Report",
                "description": "Knowledge graph insights",
                "sections": [
                    {"type": "summary", "title": "Graph Overview"},
                    {
                        "type": "charts",
                        "title": "Graph Metrics",
                        "charts": [
                            {
                                "type": "bar",
                                "title": "Node Type Distribution",
                                "data_source": "node_types",
                            }
                        ],
                    },
                ],
            },
        }

        return {
            "templates": templates,
            "output_formats": ["pdf", "csv", "xlsx", "json"],
            "delivery_methods": ["email", "webhook", "s3"],
            "schedule_types": ["once", "daily", "weekly", "monthly"],
        }

    except Exception as e:
        logger.error(f"Error getting report templates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get report templates",
        )


@router.post("/{report_id}/schedule", status_code=status.HTTP_200_OK)
async def schedule_report(
    report_id: uuid.UUID,
    schedule_config: Dict[str, Any],
    current_user: User = Depends(get_current_user),
):
    """Schedule automatic report generation"""
    try:
        # Check if report exists and user has permission
        report = await report_service.get_report(
            report_id=report_id, user_id=current_user.id
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )

        # Update schedule configuration
        async with get_async_session() as db:
            await db.execute(
                update(AnalyticsReport)
                .where(AnalyticsReport.id == report_id)
                .values(schedule_config=schedule_config)
            )
            await db.commit()

        return {"message": "Report schedule updated successfully"}

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error scheduling report: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to schedule report",
        )


@router.get("/{report_id}/history", response_model=List[Dict[str, Any]])
async def get_report_history(
    report_id: uuid.UUID,
    limit: int = Query(
        10, ge=1, le=50, description="Number of history entries to return"
    ),
    current_user: User = Depends(get_current_user),
):
    """Get report generation history"""
    try:
        # Check if report exists and user has permission
        report = await report_service.get_report(
            report_id=report_id, user_id=current_user.id
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )

        # This is a simplified implementation
        # In production, you'd have a separate table for report generation history
        history = [
            {
                "id": str(uuid.uuid4()),
                "report_id": str(report_id),
                "generated_at": report.last_run_at or datetime.utcnow(),
                "status": report.last_run_status or "pending",
                "file_size": 1024,  # Would be actual file size
                "download_url": f"/api/v1/analytics/reports/{report_id}/download/latest",
            }
        ]

        return history

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error getting report history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get report history",
        )


@router.get("/{report_id}/download/{history_id}")
async def download_report(
    report_id: uuid.UUID,
    history_id: str,
    current_user: User = Depends(get_current_user),
):
    """Download a generated report"""
    try:
        # Check if report exists and user has permission
        report = await report_service.get_report(
            report_id=report_id, user_id=current_user.id
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            )

        # This is a simplified implementation
        # In production, you'd retrieve the actual file from storage
        if history_id == "latest" and report.last_run_status == "completed":
            # For demo purposes, create a simple text file
            import tempfile
            from pathlib import Path

            temp_dir = Path(tempfile.gettempdir())
            file_path = temp_dir / f"report_{report_id}.{report.output_format}"

            # Create a simple file
            with open(file_path, "w") as f:
                f.write(f"Report: {report.title}\n")
                f.write(f"Generated: {datetime.utcnow()}\n")
                f.write("This is a placeholder report file.\n")

            return FileResponse(
                path=file_path,
                filename=f"{report.name}.{report.output_format}",
                media_type="application/octet-stream",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report file not found"
            )

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error downloading report: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download report",
        )
