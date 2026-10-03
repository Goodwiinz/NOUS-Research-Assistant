"""
Dashboard API routes
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.core.dependencies import get_current_user
from src.models.analytics.dashboard_models import (
    DashboardCreate,
    DashboardResponse,
    DashboardUpdate,
    WidgetCreate,
    WidgetResponse,
    WidgetUpdate,
)
from src.models.user import User
from src.services.analytics.dashboard_service import dashboard_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["analytics-dashboards"])


@router.post("/", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    request: DashboardCreate, current_user: User = Depends(get_current_user)
):
    """Create a new analytics dashboard"""
    try:
        dashboard = await dashboard_service.create_dashboard(
            request=request,
            owner_id=current_user.id,
            organization_id=current_user.organization_id,
        )
        return dashboard

    except Exception as e:
        logger.error(f"Error creating dashboard: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create dashboard",
        )


@router.get("/", response_model=List[DashboardResponse])
async def list_dashboards(
    category: Optional[str] = Query(None, description="Filter by category"),
    tags: Optional[str] = Query(None, description="Filter by tags (comma-separated)"),
    include_public: bool = Query(True, description="Include public dashboards"),
    include_templates: bool = Query(False, description="Include template dashboards"),
    limit: int = Query(50, ge=1, le=100, description="Number of dashboards to return"),
    offset: int = Query(0, ge=0, description="Number of dashboards to skip"),
    current_user: User = Depends(get_current_user),
):
    """List analytics dashboards"""
    try:
        tag_list = tags.split(",") if tags else None
        dashboards = await dashboard_service.list_dashboards(
            user_id=current_user.id,
            organization_id=current_user.organization_id,
            include_public=include_public,
            include_templates=include_templates,
            category=category,
            tags=tag_list,
            limit=limit,
            offset=offset,
        )
        return dashboards

    except Exception as e:
        logger.error(f"Error listing dashboards: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list dashboards",
        )


@router.get("/categories", response_model=List[str])
async def get_dashboard_categories(current_user: User = Depends(get_current_user)):
    """Get available dashboard categories"""
    try:
        categories = await dashboard_service.get_dashboard_categories(current_user.id)
        return categories

    except Exception as e:
        logger.error(f"Error getting dashboard categories: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get dashboard categories",
        )


@router.get("/tags", response_model=List[str])
async def get_dashboard_tags(current_user: User = Depends(get_current_user)):
    """Get available dashboard tags"""
    try:
        tags = await dashboard_service.get_dashboard_tags(current_user.id)
        return tags

    except Exception as e:
        logger.error(f"Error getting dashboard tags: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get dashboard tags",
        )


@router.get("/{dashboard_id}", response_model=DashboardResponse)
async def get_dashboard(
    dashboard_id: uuid.UUID,
    include_widgets: bool = Query(False, description="Include dashboard widgets"),
    current_user: User = Depends(get_current_user),
):
    """Get dashboard by ID"""
    try:
        if include_widgets:
            dashboard = await dashboard_service.get_dashboard(
                dashboard_id=dashboard_id, user_id=current_user.id, include_widgets=True
            )
        else:
            dashboard = await dashboard_service.get_dashboard(
                dashboard_id=dashboard_id,
                user_id=current_user.id,
                include_widgets=False,
            )
        return dashboard

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error getting dashboard {dashboard_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found"
        )


@router.put("/{dashboard_id}", response_model=DashboardResponse)
async def update_dashboard(
    dashboard_id: uuid.UUID,
    request: DashboardUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update dashboard"""
    try:
        dashboard = await dashboard_service.update_dashboard(
            dashboard_id=dashboard_id, request=request, user_id=current_user.id
        )
        return dashboard

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error updating dashboard {dashboard_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found"
        )


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dashboard(
    dashboard_id: uuid.UUID, current_user: User = Depends(get_current_user)
):
    """Delete dashboard"""
    try:
        success = await dashboard_service.delete_dashboard(
            dashboard_id=dashboard_id, user_id=current_user.id
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found"
            )

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error deleting dashboard {dashboard_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete dashboard",
        )


@router.post("/{dashboard_id}/duplicate", response_model=DashboardResponse)
async def duplicate_dashboard(
    dashboard_id: uuid.UUID,
    new_name: str = Query(..., description="Name for the duplicated dashboard"),
    current_user: User = Depends(get_current_user),
):
    """Duplicate a dashboard"""
    try:
        dashboard = await dashboard_service.duplicate_dashboard(
            dashboard_id=dashboard_id, new_name=new_name, user_id=current_user.id
        )
        return dashboard

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error duplicating dashboard {dashboard_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found"
        )


@router.post("/{dashboard_id}/share", status_code=status.HTTP_200_OK)
async def share_dashboard(
    dashboard_id: uuid.UUID,
    share_with_user_id: uuid.UUID,
    permissions: Dict[str, bool],
    current_user: User = Depends(get_current_user),
):
    """Share dashboard with another user"""
    try:
        success = await dashboard_service.share_dashboard(
            dashboard_id=dashboard_id,
            user_id=current_user.id,
            share_with_user_id=share_with_user_id,
            permissions=permissions,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to share dashboard",
            )
        return {"message": "Dashboard shared successfully"}

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error sharing dashboard {dashboard_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to share dashboard",
        )


# Widget endpoints


@router.post(
    "/{dashboard_id}/widgets",
    response_model=WidgetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_widget(
    dashboard_id: uuid.UUID,
    request: WidgetCreate,
    current_user: User = Depends(get_current_user),
):
    """Create a new widget for dashboard"""
    try:
        widget = await dashboard_service.create_widget(
            dashboard_id=dashboard_id, request=request, user_id=current_user.id
        )
        return widget

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error creating widget: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create widget",
        )


@router.put("/{dashboard_id}/widgets/{widget_id}", response_model=WidgetResponse)
async def update_widget(
    dashboard_id: uuid.UUID,
    widget_id: uuid.UUID,
    request: WidgetUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update widget"""
    try:
        widget = await dashboard_service.update_widget(
            widget_id=widget_id, request=request, user_id=current_user.id
        )
        return widget

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error updating widget {widget_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Widget not found"
        )


@router.delete(
    "/{dashboard_id}/widgets/{widget_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_widget(
    dashboard_id: uuid.UUID,
    widget_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
):
    """Delete widget"""
    try:
        success = await dashboard_service.delete_widget(
            widget_id=widget_id, user_id=current_user.id
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Widget not found"
            )

    except PermissionError:
        logger.warning("Access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except Exception as e:
        logger.error(f"Error deleting widget {widget_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete widget",
        )


@router.get("/widgets/types", response_model=Dict[str, Any])
async def get_widget_types(current_user: User = Depends(get_current_user)):
    """Get available widget types and their default configurations"""
    try:
        widget_configs = {
            "chart": {
                "display_name": "Chart",
                "description": "Line, bar, pie, and other chart types",
                "default_config": dashboard_service.get_widget_type_config("chart"),
            },
            "metric": {
                "display_name": "Metric Card",
                "description": "Single metric display with optional trend",
                "default_config": dashboard_service.get_widget_type_config("metric"),
            },
            "table": {
                "display_name": "Data Table",
                "description": "Tabular data display with sorting and filtering",
                "default_config": dashboard_service.get_widget_type_config("table"),
            },
            "graph": {
                "display_name": "Graph Visualization",
                "description": "Network graph and node-link diagrams",
                "default_config": {},
            },
            "text": {
                "display_name": "Text/HTML",
                "description": "Rich text or HTML content",
                "default_config": {},
            },
            "filter": {
                "display_name": "Filter",
                "description": "Interactive filter controls",
                "default_config": {},
            },
        }

        return widget_configs

    except Exception as e:
        logger.error(f"Error getting widget types: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get widget types",
        )
