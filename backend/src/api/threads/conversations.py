"""
Conversations API endpoints for Terminal Observatory chat system.

Provides REST endpoints for workspace and conversation management.
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.chat import (  # Workspace schemas; Conversation schemas
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
    ConversationUpdate,
    WorkspaceCreate,
    WorkspaceDetailResponse,
    WorkspaceMemberCreate,
    WorkspaceMemberResponse,
    WorkspaceMemberUpdate,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from src.services.threads.chat_service import ChatService, get_chat_service

router = APIRouter(prefix="/conversations", tags=["Conversations"])


# =============================================================================
# Workspace Endpoints
# =============================================================================


@router.post(
    "/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED
)
async def create_workspace(
    data: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new workspace.

    A workspace is a project container that holds conversations, threads, and collections.
    """
    service = get_chat_service(db)
    try:
        workspace = await service.create_workspace(data, current_user.id)
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_public=workspace.is_public,
        is_archived=workspace.is_archived,
        owner_id=workspace.owner_id,
        organization_id=workspace.organization_id,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        member_count=len(workspace.members),
        conversation_count=(
            len(workspace.conversations) if workspace.conversations else 0
        ),
    )


@router.get("/workspaces", response_model=List[WorkspaceResponse])
async def list_workspaces(
    include_archived: bool = Query(False, description="Include archived workspaces"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List all workspaces accessible to the current user.
    """
    service = get_chat_service(db)
    workspaces, total = await service.list_workspaces(
        user_id=current_user.id,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )

    return [
        WorkspaceResponse(
            id=w.id,
            name=w.name,
            description=w.description,
            is_public=w.is_public,
            is_archived=w.is_archived,
            owner_id=w.owner_id,
            organization_id=w.organization_id,
            created_at=w.created_at,
            updated_at=w.updated_at,
            member_count=len(w.members) if w.members else 0,
            conversation_count=len(w.conversations) if w.conversations else 0,
        )
        for w in workspaces
    ]


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceDetailResponse)
async def get_workspace(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get workspace details by ID.
    """
    service = get_chat_service(db)
    workspace = await service.get_workspace(
        workspace_id, current_user.id, load_conversations=True
    )

    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or access denied",
        )

    members = [
        WorkspaceMemberResponse(
            id=m.id,
            workspace_id=m.workspace_id,
            user_id=m.user_id,
            role=m.role,
            joined_at=m.joined_at,
            invited_by_id=m.invited_by_id,
            created_at=m.created_at,
            updated_at=m.updated_at,
            user_email=m.user.email if m.user else None,
            user_name=f"{m.user.first_name} {m.user.last_name}" if m.user else None,
        )
        for m in workspace.members
        if not m.is_deleted
    ]

    return WorkspaceDetailResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_public=workspace.is_public,
        is_archived=workspace.is_archived,
        owner_id=workspace.owner_id,
        organization_id=workspace.organization_id,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        member_count=len(members),
        conversation_count=(
            len(workspace.conversations) if workspace.conversations else 0
        ),
        members=members,
    )


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    data: WorkspaceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update workspace properties.

    Requires admin permission on the workspace.
    """
    service = get_chat_service(db)
    workspace = await service.update_workspace(workspace_id, data, current_user.id)

    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or insufficient permissions",
        )

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        is_public=workspace.is_public,
        is_archived=workspace.is_archived,
        owner_id=workspace.owner_id,
        organization_id=workspace.organization_id,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        member_count=len(workspace.members) if workspace.members else 0,
        conversation_count=(
            len(workspace.conversations) if workspace.conversations else 0
        ),
    )


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a workspace (soft delete).

    Only the workspace owner can delete it.
    """
    service = get_chat_service(db)
    success = await service.delete_workspace(workspace_id, current_user.id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or insufficient permissions",
        )


@router.get("/workspaces/{workspace_id}/stats")
async def get_workspace_stats(
    workspace_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get workspace statistics.
    """
    service = get_chat_service(db)
    stats = await service.get_workspace_stats(workspace_id, current_user.id)

    if not stats:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or access denied",
        )

    return stats


# =============================================================================
# Conversation Endpoints
# =============================================================================


@router.post(
    "", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED
)
async def create_conversation(
    data: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new conversation in a workspace.

    A conversation is a high-level topic container (e.g., "Q3 Financial Analysis").
    """
    service = get_chat_service(db)
    conversation = await service.create_conversation(data, current_user.id)

    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found or insufficient permissions",
        )

    return ConversationResponse(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        description=conversation.description,
        is_archived=conversation.is_archived,
        is_pinned=conversation.is_pinned,
        last_activity_at=conversation.last_activity_at,
        created_by_id=conversation.created_by_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        thread_count=conversation.thread_count,
    )


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    workspace_id: UUID = Query(
        ..., description="Workspace ID to list conversations from"
    ),
    include_archived: bool = Query(False, description="Include archived conversations"),
    search: Optional[str] = Query(
        None, description="Search query for title/description"
    ),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List conversations in a workspace.
    """
    service = get_chat_service(db)
    conversations, total = await service.list_conversations(
        workspace_id=workspace_id,
        user_id=current_user.id,
        include_archived=include_archived,
        search_query=search,
        limit=limit,
        offset=offset,
    )

    page = (offset // limit) + 1 if limit > 0 else 1
    has_more = (offset + len(conversations)) < total

    serialized: List[ConversationResponse] = []
    for c in conversations:
        try:
            serialized.append(
                ConversationResponse(
                    id=c.id,
                    workspace_id=c.workspace_id,
                    title=c.title,
                    description=c.description,
                    is_archived=c.is_archived,
                    is_pinned=c.is_pinned,
                    last_activity_at=c.last_activity_at,
                    created_by_id=c.created_by_id,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                    thread_count=c.thread_count,
                )
            )
        except Exception:
            # One bad row (NULL created_by_id from a deleted user, lazy-load
            # failure on a relationship, etc.) must not 500 the whole list and
            # cascade into getOrCreateDefaultConversation minting fresh empty
            # conversations on every page load.
            logger.exception(
                "list_conversations.serialize_failed",
                extra={"conversation_id": str(getattr(c, "id", None))},
            )

    return ConversationListResponse(
        conversations=serialized,
        total=total,
        page=page,
        limit=limit,
        has_more=has_more,
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get conversation details by ID.
    """
    service = get_chat_service(db)
    conversation = await service.get_conversation(conversation_id, current_user.id)

    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or access denied",
        )

    return ConversationResponse(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        description=conversation.description,
        is_archived=conversation.is_archived,
        is_pinned=conversation.is_pinned,
        last_activity_at=conversation.last_activity_at,
        created_by_id=conversation.created_by_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        thread_count=conversation.thread_count,
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: UUID,
    data: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update conversation properties.
    """
    service = get_chat_service(db)
    conversation = await service.update_conversation(
        conversation_id, data, current_user.id
    )

    if not conversation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or insufficient permissions",
        )

    return ConversationResponse(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        description=conversation.description,
        is_archived=conversation.is_archived,
        is_pinned=conversation.is_pinned,
        last_activity_at=conversation.last_activity_at,
        created_by_id=conversation.created_by_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        thread_count=conversation.thread_count,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a conversation (soft delete).

    Requires admin permission on the workspace.
    """
    service = get_chat_service(db)
    success = await service.delete_conversation(conversation_id, current_user.id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or insufficient permissions",
        )


@router.post("/{conversation_id}/archive", response_model=ConversationResponse)
async def archive_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Archive a conversation.
    """
    from src.schemas.chat import ConversationUpdate

    return await update_conversation(
        conversation_id, ConversationUpdate(is_archived=True), current_user, db
    )


@router.post("/{conversation_id}/unarchive", response_model=ConversationResponse)
async def unarchive_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Unarchive a conversation.
    """
    from src.schemas.chat import ConversationUpdate

    return await update_conversation(
        conversation_id, ConversationUpdate(is_archived=False), current_user, db
    )


@router.post("/{conversation_id}/pin", response_model=ConversationResponse)
async def pin_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Pin a conversation to the top of the list.
    """
    from src.schemas.chat import ConversationUpdate

    return await update_conversation(
        conversation_id, ConversationUpdate(is_pinned=True), current_user, db
    )


@router.post("/{conversation_id}/unpin", response_model=ConversationResponse)
async def unpin_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Unpin a conversation.
    """
    from src.schemas.chat import ConversationUpdate

    return await update_conversation(
        conversation_id, ConversationUpdate(is_pinned=False), current_user, db
    )
