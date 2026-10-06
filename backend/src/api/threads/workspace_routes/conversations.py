"""
Conversation endpoints, nested and standalone (Task 4.2 split of the former
monolithic ``backend/src/api/threads/workspaces.py``; Task 4.3 moved the
persistence logic into ``src/services/threads/conversation_service.py``; PR 3
Task 3.2 moved the single request commit UP to these handlers — the service
flushes, each mutating handler ends with one ``await db.commit()``). Transport
+ transaction-boundary only.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.chat import (
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
    ConversationUpdate,
)
from src.services.threads import conversation_service, workspace_access

from .dependencies import _get_conversation_or_404
from .presenters import _conversation_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# Standalone router for flat API paths (used by frontend)
standalone_router = APIRouter(prefix="/api/v2", tags=["workspaces-flat"])

# ============================================================================
# Conversation Endpoints
# ============================================================================


@router.post(
    "/{workspace_id}/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    workspace_id: UUID,
    request: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationResponse:
    """Create a new conversation in a workspace"""
    request.workspace_id = workspace_id
    try:
        conversation = await conversation_service.create_conversation(
            db, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not conversation:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await db.commit()
    return _conversation_to_response(conversation)


@router.get("/{workspace_id}/conversations", response_model=ConversationListResponse)
async def list_conversations(
    workspace_id: UUID,
    include_archived: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationListResponse:
    """List conversations in a workspace"""
    offset = (page - 1) * limit
    result = await conversation_service.list_conversations(
        db,
        workspace_id,
        current_user.id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        order_pinned_first=False,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    conversations, total, counts = result

    return ConversationListResponse(
        conversations=[
            _conversation_to_response(
                c,
                thread_count=counts.get(c.id, (0, 0))[0],
                message_count=counts.get(c.id, (0, 0))[1],
            )
            for c in conversations
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=(offset + len(conversations)) < total,
    )


@router.get(
    "/{workspace_id}/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def get_conversation(
    workspace_id: UUID,
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationResponse:
    """Get conversation details"""
    conversation = await _get_conversation_or_404(
        db, workspace_id, conversation_id, current_user
    )
    return _conversation_to_response(conversation)


@router.patch(
    "/{workspace_id}/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def update_conversation(
    workspace_id: UUID,
    conversation_id: UUID,
    request: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationResponse:
    """Update conversation details"""
    try:
        conversation = await conversation_service.update_conversation(
            db, conversation_id, request, current_user.id, workspace_id=workspace_id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()
    return _conversation_to_response(conversation)


@router.delete(
    "/{workspace_id}/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    workspace_id: UUID,
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a conversation"""
    try:
        deleted = await conversation_service.delete_conversation(
            db,
            conversation_id,
            current_user.id,
            workspace_id=workspace_id,
            stamp_deleted_at=True,
            require_admin=False,
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()


# ============================================================================
# Standalone (Flat) Conversation Routes
# These routes allow direct access without full path hierarchy
# ============================================================================


@standalone_router.get(
    "/conversations/{conversation_id}", response_model=ConversationResponse
)
async def get_conversation_standalone(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationResponse:
    """Get conversation details (standalone route)"""
    conversation = await workspace_access.get_conversation(
        db, conversation_id, current_user.id
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return _conversation_to_response(conversation)


@standalone_router.patch(
    "/conversations/{conversation_id}", response_model=ConversationResponse
)
async def update_conversation_standalone(
    conversation_id: UUID,
    request: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationResponse:
    """Update conversation details (standalone route)"""
    try:
        conversation = await conversation_service.update_conversation(
            db, conversation_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()
    return _conversation_to_response(conversation)


@standalone_router.delete(
    "/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation_standalone(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a conversation (standalone route)"""
    try:
        deleted = await conversation_service.delete_conversation(
            db,
            conversation_id,
            current_user.id,
            stamp_deleted_at=True,
            require_admin=False,
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()
