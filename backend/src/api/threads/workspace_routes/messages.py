"""
Message endpoints, nested and standalone (Task 4.2 split of the former
monolithic ``backend/src/api/threads/workspaces.py``).

Task 4.3 moved the read/update/delete persistence logic into
``src/services/threads/message_service.py``; PR 3 Task 3.2 moved the single
request commit UP to the update/delete handlers — the message service flushes,
those handlers end with one ``await db.commit()``.

``create_message``/``create_message_standalone`` are unchanged and deliberately
issue NO route commit: they delegate to the canonical
``ChatService.create_message`` (consolidated in #1051, audit finding C4), which
is a DIRECT transaction owner (commits internally, out of scope for this task).
Adding a route commit there would double-commit. The read-only ``select(...)``
re-fetch after that call is a response-shaping query, not a transaction.
"""

import logging
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.document import Document
from src.models.message_attachment import MessageAttachment
from src.models.user import User
from src.schemas.chat import (
    ChatMessageCreate,
    ChatMessageListResponse,
    ChatMessageResponse,
    ChatMessageUpdate,
)
from src.services.threads import message_service, workspace_access

from .dependencies import _get_thread_or_404
from .presenters import _message_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# Standalone router for flat API paths (used by frontend)
standalone_router = APIRouter(prefix="/api/v2", tags=["workspaces-flat"])

# ============================================================================
# Message Endpoints
# ============================================================================


@router.post(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def create_message(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    request: ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    """[DEPRECATED] Create a new message in a thread.

    Audit finding C4: three public POST create-message routes coexist but only
    the flat ``POST /api/v2/messages`` (``create_message_standalone`` below) is
    called by any client — it is the canonical route. This deeply-nested
    workspace variant has no live callers (frontend, CLI, scripts, or synthetic
    traffic); it already delegates to ``ChatService.create_message`` and is kept
    only for a deprecation window. New clients MUST use ``POST /api/v2/messages``
    with ``thread_id`` in the body. Slated for removal in a follow-up cleanup PR
    once the window closes.

    See ``docs/decisions/api-deprecation-window.md`` for the deletion-eligible
    date and criteria.
    """
    # Validate the path hierarchy (workspace/conversation/thread + soft-delete
    # filters) before delegating; the 403 for non-editors is this route's
    # documented behavior.
    thread = await _get_thread_or_404(
        db, workspace_id, conversation_id, thread_id, current_user
    )

    if not thread.conversation.workspace.can_user_edit(str(current_user.id)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Delegate to the canonical ChatService.create_message. This route used to
    # reimplement it and drifted (same as the v2 standalone route, fixed in
    # #1051): it accepted but silently discarded latency_ms, stopped, and
    # attachment_ids, skipped token accounting, and lacked the org-ownership
    # guard on attachments. The path thread_id is authoritative — this route
    # always wrote to it and ignored any body thread_id.
    from src.services.threads.chat_service import get_chat_service

    request.thread_id = thread_id
    service = get_chat_service(db)
    message = await service.create_message(request, current_user.id)
    if not message:
        raise HTTPException(
            status_code=404, detail="Thread not found or insufficient permissions"
        )

    # Re-query with eager loading to get citations with document info.
    # Deliberately NOT filtered on ``superseded_by_message_id``: this reads back
    # the row just created, by id.
    stmt = (
        select(ChatMessage)
        .options(
            # load_only: the message responses render only title/type/mime of a
            # cited/attached Document — never content_text/content_summary/
            # search_vector (the heavy extracted body). Loading only the 3 read
            # columns keeps content_text off the wire on every paged fetch.
            selectinload(ChatMessage.citations)
            .selectinload(Citation.document)
            .load_only(Document.title, Document.document_type, Document.mime_type),
            selectinload(ChatMessage.attachments)
            .selectinload(MessageAttachment.document)
            .load_only(Document.title, Document.document_type, Document.mime_type),
        )
        .where(ChatMessage.id == message.id)
    )
    result = await db.execute(stmt)
    message = result.scalars().first()
    if message is None:
        raise HTTPException(
            status_code=404, detail="Thread not found or insufficient permissions"
        )

    return _message_to_response(message)


@router.get(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages",
    response_model=ChatMessageListResponse,
)
async def list_messages(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageListResponse:
    """List messages in a thread"""
    offset = (page - 1) * limit
    result = await message_service.list_messages(
        db,
        thread_id,
        current_user.id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
        order="asc",
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages, total, has_more = result

    return ChatMessageListResponse(
        messages=[_message_to_response(m) for m in messages],
        total=total,
        page=page,
        limit=limit,
        has_more=has_more,
    )


@router.patch(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages/{message_id}",
    response_model=ChatMessageResponse,
)
async def update_message_feedback(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    message_id: UUID,
    request: ChatMessageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    """Update message feedback"""
    try:
        message = await message_service.update_message_feedback(
            db,
            message_id,
            request,
            current_user.id,
            thread_id=thread_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    await db.commit()
    return _message_to_response(message)


# ============================================================================
# Standalone (Flat) Message Routes
# These routes allow direct access without full path hierarchy
# ============================================================================


@standalone_router.get(
    "/threads/{thread_id}/messages", response_model=ChatMessageListResponse
)
async def list_messages_standalone(
    thread_id: UUID,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    order: str = Query(
        "asc",
        pattern="^(asc|desc)$",
        description=(
            "asc = chronological page/offset (default). desc = newest-first "
            "cursor window: returns the most recent `limit` messages (older than "
            "`before_id` when given), newest→oldest; the client reverses for "
            "display. `has_more` then means older messages remain."
        ),
    ),
    before_id: Optional[UUID] = Query(
        None,
        description=(
            "desc-order cursor: return only messages strictly OLDER than this "
            "message id. Ignored when order=asc."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageListResponse:
    """List messages in a thread (standalone route)"""
    # `order`'s FastAPI Query pattern (`^(asc|desc)$`) already constrains the
    # wire value; kept as a plain `str` param (not Literal) so the OpenAPI
    # schema still renders as a regex-constrained string rather than an
    # enum, matching the existing generated contract. Re-validate here and
    # assign literal constants (not the `str` variable) so mypy narrows to
    # the Literal["asc", "desc"] `list_messages` expects, with no cast().
    validated_order: Literal["asc", "desc"]
    if order == "asc":
        validated_order = "asc"
    elif order == "desc":
        validated_order = "desc"
    else:
        raise HTTPException(status_code=400, detail=f"Invalid order: {order}")
    offset = (page - 1) * limit
    result = await message_service.list_messages(
        db,
        thread_id,
        current_user.id,
        limit=limit,
        offset=offset,
        before_id=before_id,
        order=validated_order,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages, total, has_more = result

    return ChatMessageListResponse(
        messages=[_message_to_response(m) for m in messages],
        total=total,
        page=page,
        limit=limit,
        has_more=has_more,
    )


@standalone_router.post(
    "/messages", response_model=ChatMessageResponse, status_code=status.HTTP_201_CREATED
)
async def create_message_standalone(
    request: ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    """Create a new message (standalone route - uses thread_id from request body).

    CANONICAL create-message route (audit finding C4). This flat
    ``POST /api/v2/messages`` is the only create-message route any client calls
    (frontend ``workspaceService.createMessage``); the two nested variants
    (``POST /api/v2/threads/{thread_id}/messages`` and
    ``POST /api/v2/workspaces/.../threads/{thread_id}/messages``) are deprecated
    and awaiting removal. Route new clients here.
    """
    # Delegate to the canonical ChatService.create_message. This route used to
    # reimplement it and drifted: it accepted but silently discarded
    # latency_ms, stopped, and attachment_ids (so legacy-mode clients lost the
    # stopped badge / response time on reload), skipped token accounting, and
    # lacked the org-ownership guard on attachments.
    from src.services.threads.chat_service import get_chat_service

    service = get_chat_service(db)
    message = await service.create_message(request, current_user.id)
    if not message:
        raise HTTPException(
            status_code=404, detail="Thread not found or insufficient permissions"
        )

    # Re-query with eager loading to get citations with document info.
    # Deliberately NOT filtered on ``superseded_by_message_id``: this reads back
    # the row just created, by id.
    stmt = (
        select(ChatMessage)
        .options(
            # load_only: the message responses render only title/type/mime of a
            # cited/attached Document — never content_text/content_summary/
            # search_vector (the heavy extracted body). Loading only the 3 read
            # columns keeps content_text off the wire on every paged fetch.
            selectinload(ChatMessage.citations)
            .selectinload(Citation.document)
            .load_only(Document.title, Document.document_type, Document.mime_type),
            selectinload(ChatMessage.attachments)
            .selectinload(MessageAttachment.document)
            .load_only(Document.title, Document.document_type, Document.mime_type),
        )
        .where(ChatMessage.id == message.id)
    )
    result = await db.execute(stmt)
    message = result.scalars().first()
    if message is None:
        raise HTTPException(
            status_code=404, detail="Thread not found or insufficient permissions"
        )

    return _message_to_response(message)


@standalone_router.get("/messages/{message_id}", response_model=ChatMessageResponse)
async def get_message_standalone(
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    """Get message details (standalone route)"""
    message = await workspace_access.get_message(db, message_id, current_user.id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    return _message_to_response(message)


@standalone_router.patch("/messages/{message_id}", response_model=ChatMessageResponse)
async def update_message_standalone(
    message_id: UUID,
    request: ChatMessageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    """Update message feedback (standalone route)"""
    try:
        message = await message_service.update_message_feedback(
            db, message_id, request, current_user.id
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    await db.commit()
    return _message_to_response(message)


@standalone_router.delete(
    "/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_message_standalone(
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a message (standalone route)"""
    try:
        deleted = await message_service.delete_message(
            db, message_id, current_user.id, require_author_or_admin=False
        )
    except PermissionError:
        logger.warning("Workspace access denied", exc_info=True)
        raise HTTPException(status_code=403, detail="Workspace access denied")
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found")

    await db.commit()
