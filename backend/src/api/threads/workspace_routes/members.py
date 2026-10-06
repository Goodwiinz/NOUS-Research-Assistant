"""
Workspace member management endpoints (Task 4.2 split of the former
monolithic ``backend/src/api/threads/workspaces.py``; Task 4.3 moved the
persistence logic into ``src/services/threads/workspace_service.py``; PR 3
Task 3.2 moved the single request commit UP to these handlers — the member
service functions flush, each mutating handler ends with one
``await db.commit()``). Transport + transaction-boundary only.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.chat import (
    WorkspaceMemberCreate,
    WorkspaceMemberResponse,
    WorkspaceMemberUpdate,
)
from src.services.threads import workspace_service

from .presenters import _member_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# ============================================================================
# Workspace Member Endpoints
# ============================================================================


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_member(
    workspace_id: UUID,
    request: WorkspaceMemberCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceMemberResponse:
    """Add a member to a workspace"""
    try:
        member = await workspace_service.add_member(
            db, workspace_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    except ValueError:
        logger.warning("Invalid workspace member request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid workspace member request")
    if not member:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    return _member_to_response(member)


@router.patch(
    "/{workspace_id}/members/{user_id}", response_model=WorkspaceMemberResponse
)
async def update_member_role(
    workspace_id: UUID,
    user_id: UUID,
    request: WorkspaceMemberUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WorkspaceMemberResponse:
    """Update a member's role"""
    try:
        member = await workspace_service.update_member_role(
            db, workspace_id, user_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    except ValueError:
        logger.warning("Invalid workspace member request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid workspace member request")
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    await db.commit()
    return _member_to_response(member)


@router.delete(
    "/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_workspace_member(
    workspace_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Remove a member from a workspace"""
    try:
        removed = await workspace_service.remove_member(
            db, workspace_id, user_id, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    except ValueError:
        logger.warning("Invalid workspace member request", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid workspace member request")
    if not removed:
        raise HTTPException(status_code=404, detail="Member not found")

    await db.commit()
