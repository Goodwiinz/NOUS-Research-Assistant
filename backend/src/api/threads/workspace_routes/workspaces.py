"""
Workspace CRUD endpoints (Task 4.2 split of the former monolithic
``backend/src/api/threads/workspaces.py``; Task 4.3 moved the persistence
logic into ``src/services/threads/workspace_service.py``; PR 3 Task 3.2 moved
the single request commit UP to these handlers — the service flushes, each
mutating handler ends with one ``await db.commit()``). Transport +
transaction-boundary: auth/permission-error -> HTTP status mapping, request/
response shaping, and the request commit.
"""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.chat import (
    WorkspaceCreate,
    WorkspaceDetailResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from src.services.threads import workspace_service

from .dependencies import _get_workspace_or_404
from .presenters import _workspace_to_detail_response, _workspace_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# ============================================================================
# Workspace Endpoints
# ============================================================================


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceResponse:
    """Create a new workspace"""
    user_org_id = getattr(current_user, "organization_id", None)
    try:
        workspace = await workspace_service.create_workspace(
            db, request, current_user.id, user_org_id, enforce_org_match=True
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Workspace access denied"
        )

    await db.commit()
    logger.info(f"Workspace '{workspace.name}' created by user {current_user.id}")
    return _workspace_to_response(workspace)


@router.get("", response_model=List[WorkspaceResponse])
async def list_workspaces(
    include_archived: bool = Query(False, description="Include archived workspaces"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[WorkspaceResponse]:
    """List workspaces accessible by the current user"""
    workspaces, _total = await workspace_service.list_workspaces(
        db,
        current_user.id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        filter_deleted_memberships=True,
    )
    return [_workspace_to_response(w) for w in workspaces]


@router.get("/{workspace_id}", response_model=WorkspaceDetailResponse)
async def get_workspace(
    workspace_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceDetailResponse:
    """Get workspace details with members"""
    workspace = await _get_workspace_or_404(db, workspace_id, current_user)

    return _workspace_to_detail_response(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    request: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceResponse:
    """Update workspace details"""
    try:
        workspace = await workspace_service.update_workspace(
            db, workspace_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    return _workspace_to_response(workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a workspace"""
    try:
        deleted = await workspace_service.delete_workspace(
            db, workspace_id, current_user.id, stamp_deleted_at=True
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    logger.info(f"Workspace {workspace_id} deleted by user {current_user.id}")
