"""
Threads API endpoints for Terminal Observatory chat system.

Provides REST endpoints for thread and message management.
"""

import logging
import threading
import time
from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.middleware.rate_limiting import get_rate_limiter
from src.models.thread import ThreadStatus
from src.models.user import User
from src.schemas.chat import (  # Thread schemas; Bulk thread schemas; Message schemas
    BulkThreadRequest,
    BulkThreadResponse,
    BulkThreadResult,
    ChatMessageCreate,
    ChatMessageListResponse,
    ChatMessageResponse,
    ChatMessageUpdate,
    CitationResponse,
    MessageAttachmentResponse,
    ThreadCreate,
    ThreadDetailResponse,
    ThreadListResponse,
    ThreadResponse,
    ThreadUpdate,
)
from src.services.agent._pii_redact import redact_tool_executions
from src.services.threads.chat_service import ChatService, get_chat_service
from src.services.threads.thread_event_service import thread_event_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["Threads"])

# Initialize rate limiter (uses Redis if available, falls back to in-memory)
_rate_limiter = None
_rate_limiter_lock = threading.Lock()


def get_bulk_rate_limiter():
    """Get or create the rate limiter instance (thread-safe)."""
    global _rate_limiter
    # First check (without lock for performance)
    if _rate_limiter is None:
        with _rate_limiter_lock:
            # Double-check inside lock to prevent race conditions
            if _rate_limiter is None:
                try:
                    import redis

                    redis_client = redis.Redis.from_url(
                        settings.REDIS_URL or "redis://localhost:6379/0",
                        decode_responses=True,
                    )
                    redis_client.ping()
                    _rate_limiter = get_rate_limiter(redis_client)
                    logger.info("Bulk thread rate limiter initialized with Redis")
                except Exception as e:
                    logger.warning(
                        f"Redis unavailable for rate limiting, using in-memory: {e}"
                    )
                    _rate_limiter = get_rate_limiter(None)
    return _rate_limiter


async def check_bulk_rate_limit(
    request: Request,
    current_user: User = Depends(get_current_user),
    limit: int = 10,
    window: int = 60,
    operation: str = "bulk_operation",
):
    """
    Rate limiting dependency for bulk operations.

    Args:
        request: FastAPI request object
        current_user: Authenticated user
        limit: Maximum operations allowed in window
        window: Time window in seconds
        operation: Operation name for logging

    Raises:
        HTTPException: 429 if rate limit exceeded
    """
    rate_limiter = get_bulk_rate_limiter()
    rate_key = f"threads:bulk:{current_user.id}:{operation}"

    allowed, info = rate_limiter.is_allowed(rate_key, limit, window)

    if not allowed:
        retry_after = info.get("retry_after", window)
        logger.warning(
            f"Rate limit exceeded for user {current_user.id} on {operation}: "
            f"{info['current_requests']}/{info['limit']} requests"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {limit} bulk {operation} operations per {window} seconds.",
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(
                    int(info.get("reset_time", time.time() + window))
                ),
                "Retry-After": str(retry_after),
            },
        )

    return True


# Rate limit dependencies for specific operations
async def check_bulk_resolve_rate_limit(
    request: Request, current_user: User = Depends(get_current_user)
):
    """Rate limit: 10 bulk resolve operations per minute."""
    return await check_bulk_rate_limit(
        request, current_user, limit=10, window=60, operation="resolve"
    )


async def check_bulk_archive_rate_limit(
    request: Request, current_user: User = Depends(get_current_user)
):
    """Rate limit: 10 bulk archive operations per minute."""
    return await check_bulk_rate_limit(
        request, current_user, limit=10, window=60, operation="archive"
    )


async def check_bulk_summarize_rate_limit(
    request: Request, current_user: User = Depends(get_current_user)
):
    """Rate limit: 5 bulk summarize operations per minute (heavy AI ops)."""
    return await check_bulk_rate_limit(
        request, current_user, limit=5, window=60, operation="summarize"
    )


async def check_bulk_delete_rate_limit(
    request: Request, current_user: User = Depends(get_current_user)
):
    """Rate limit: 5 bulk delete operations per minute (stricter for destructive ops)."""
    return await check_bulk_rate_limit(
        request, current_user, limit=5, window=60, operation="delete"
    )


# =============================================================================
# Thread Endpoints
# =============================================================================


@router.post("", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED)
async def create_thread(
    data: ThreadCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new thread in a conversation.

    A thread is a specific line of inquiry within a conversation
    (e.g., "Revenue Forecast Investigation").
    """
    service = get_chat_service(db)
    thread = await service.create_thread(data, current_user.id)

    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found or insufficient permissions",
        )

    # Auto-link to project if project_id provided (Phase 2: Project-Chat Integration)
    if data.project_id:
        from sqlalchemy import and_

        from src.models import Collection, ProjectThreadLinkType, Workspace
        from src.services.research.project_thread_service import (
            attach_thread_to_project,
        )

        # Verify project exists and user has access
        project_query = (
            select(Collection)
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                and_(
                    Collection.id == data.project_id,
                    Workspace.owner_id == current_user.id,
                    Collection.is_deleted.is_(False),
                )
            )
        )
        project_result = await db.execute(project_query)
        project = project_result.scalar_one_or_none()

        if project:
            await attach_thread_to_project(
                db,
                thread,
                data.project_id,
                link_type=ProjectThreadLinkType.FROM_CHAT.value,
                linked_by_id=current_user.id,
                context_note="Auto-linked when creating thread with project_id",
            )
            logger.info(
                "thread_auto_linked_to_project",
                thread_id=str(thread.id),
                project_id=str(data.project_id),
            )
        else:
            logger.warning(
                "project_not_found_for_auto_link",
                project_id=str(data.project_id),
                thread_id=str(thread.id),
            )

    await db.commit()
    await db.refresh(thread)

    # Broadcast thread creation event via WebSocket
    try:
        await thread_event_service.broadcast_thread_created(
            thread_id=str(thread.id),
            conversation_id=str(thread.conversation_id),
            user_id=str(current_user.id),
            organization_id=str(current_user.organization_id),
            title=thread.title,
        )
    except Exception as e:
        logger.error(f"Failed to broadcast thread_created event: {e}")

    return ThreadResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title,
        summary=thread.summary,
        status=thread.status,
        last_message_at=thread.last_message_at,
        message_count=thread.message_count,
        token_count=thread.token_count,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        source_project_id=thread.source_project_id,
    )


@router.get("", response_model=ThreadListResponse)
async def list_threads(
    conversation_id: UUID = Query(
        ..., description="Conversation ID to list threads from"
    ),
    status_filter: Optional[ThreadStatus] = Query(
        None, description="Filter by thread status"
    ),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List threads in a conversation.
    """
    service = get_chat_service(db)
    threads, total = await service.list_threads(
        conversation_id=conversation_id,
        user_id=current_user.id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )

    page = (offset // limit) + 1 if limit > 0 else 1
    has_more = (offset + len(threads)) < total

    return ThreadListResponse(
        threads=[
            ThreadResponse(
                id=t.id,
                conversation_id=t.conversation_id,
                title=t.title or t.generate_title(),
                summary=t.summary,
                status=t.status,
                last_message_at=t.last_message_at,
                message_count=t.message_count,
                token_count=t.token_count,
                created_by_id=t.created_by_id,
                created_at=t.created_at,
                updated_at=t.updated_at,
                source_project_id=t.source_project_id,
            )
            for t in threads
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=has_more,
    )


# =============================================================================
# Bulk Thread Operations
# IMPORTANT: These routes MUST be defined before /{thread_id} routes to avoid
# FastAPI matching "bulk" as a thread_id parameter.
# =============================================================================


@router.post("/bulk/resolve", response_model=BulkThreadResponse)
async def bulk_resolve_threads(
    request: BulkThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: bool = Depends(check_bulk_resolve_rate_limit),
):
    """
    Bulk resolve multiple threads.

    Rate limited to 10 operations per minute per user.
    """
    service = get_chat_service(db)
    results = await service.bulk_update_threads(
        request.thread_ids, ThreadUpdate(status=ThreadStatus.RESOLVED), current_user.id
    )

    return await _build_bulk_response(
        results=results,
        action="resolved",
        user_id=current_user.id,
        organization_id=str(current_user.organization_id),
    )


@router.post("/bulk/archive", response_model=BulkThreadResponse)
async def bulk_archive_threads(
    request: BulkThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: bool = Depends(check_bulk_archive_rate_limit),
):
    """
    Bulk archive multiple threads.

    Rate limited to 10 operations per minute per user.
    """
    service = get_chat_service(db)
    results = await service.bulk_update_threads(
        request.thread_ids, ThreadUpdate(status=ThreadStatus.ARCHIVED), current_user.id
    )

    return await _build_bulk_response(
        results=results,
        action="archived",
        user_id=current_user.id,
        organization_id=str(current_user.organization_id),
    )


@router.post("/bulk/summarize", response_model=BulkThreadResponse)
async def bulk_summarize_threads(
    request: BulkThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: bool = Depends(check_bulk_summarize_rate_limit),
):
    """
    Bulk trigger AI summarization for multiple threads.

    Rate limited to 5 operations per minute per user (heavy AI operations).
    """
    service = get_chat_service(db)
    results = await service.bulk_summarize_threads(request.thread_ids, current_user.id)

    return await _build_bulk_response(
        results=results,
        action="summarized",
        user_id=current_user.id,
        organization_id=str(current_user.organization_id),
        include_threads=False,  # Thread data would be stale since summarization is async
    )


@router.delete("/bulk", response_model=BulkThreadResponse)
async def bulk_delete_threads(
    request: BulkThreadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _rate_limit: bool = Depends(check_bulk_delete_rate_limit),
):
    """
    Bulk delete multiple threads (soft delete).

    Rate limited to 5 operations per minute per user (stricter for destructive operations).
    """
    service = get_chat_service(db)
    results = await service.bulk_delete_threads(request.thread_ids, current_user.id)

    return await _build_bulk_response(
        results=results,
        action="deleted",
        user_id=current_user.id,
        organization_id=str(current_user.organization_id),
        include_threads=False,
    )


# =============================================================================
# Single Thread Operations
# =============================================================================


@router.get("/{thread_id}", response_model=ThreadDetailResponse)
async def get_thread(
    thread_id: UUID,
    include_messages: bool = Query(True, description="Include messages in response"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get thread details by ID, optionally including messages.
    """
    service = get_chat_service(db)
    thread = await service.get_thread(
        thread_id, current_user.id, include_messages=include_messages
    )

    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or access denied",
        )

    messages = []
    if include_messages and thread.messages:
        messages = [
            _format_message_response(m)
            for m in thread.messages
            # ``superseded_by_message_id`` is set: an edit-and-resend replaced
            # this turn. The relationship load has no row filter, so both
            # copies of this loop (see workspace_routes/presenters.py) filter
            # in Python.
            if not m.is_deleted and m.superseded_by_message_id is None
        ]

    return ThreadDetailResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title or thread.generate_title(),
        summary=thread.summary,
        status=thread.status,
        last_message_at=thread.last_message_at,
        message_count=thread.message_count,
        token_count=thread.token_count,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        source_project_id=thread.source_project_id,
        messages=messages,
    )


@router.patch("/{thread_id}", response_model=ThreadResponse)
async def update_thread(
    thread_id: UUID,
    data: ThreadUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update thread properties.
    """
    service = get_chat_service(db)
    thread = await service.update_thread(thread_id, data, current_user.id)

    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or insufficient permissions",
        )

    # Broadcast thread update event via WebSocket
    changes = data.model_dump(exclude_unset=True)
    try:
        await thread_event_service.broadcast_thread_updated(
            thread_id=str(thread.id),
            conversation_id=str(thread.conversation_id),
            user_id=str(current_user.id),
            organization_id=str(current_user.organization_id),
            changes=changes,
        )
    except Exception as e:
        logger.error(f"Failed to broadcast thread_updated event: {e}")

    return ThreadResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title,
        summary=thread.summary,
        status=thread.status,
        last_message_at=thread.last_message_at,
        message_count=thread.message_count,
        token_count=thread.token_count,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a thread (soft delete).
    """
    service = get_chat_service(db)

    # Get thread info before deletion for broadcasting
    thread = await service.get_thread(thread_id, current_user.id)
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or insufficient permissions",
        )

    conversation_id = str(thread.conversation_id)
    success = await service.delete_thread(thread_id, current_user.id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or insufficient permissions",
        )

    # Broadcast thread deletion event via WebSocket
    await thread_event_service.broadcast_thread_deleted(
        thread_id=str(thread_id),
        conversation_id=conversation_id,
        user_id=str(current_user.id),
        organization_id=str(current_user.organization_id),
    )


@router.post("/{thread_id}/resolve", response_model=ThreadResponse)
async def resolve_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark a thread as resolved.
    """
    return await update_thread(
        thread_id, ThreadUpdate(status=ThreadStatus.RESOLVED), current_user, db
    )


@router.post("/{thread_id}/reopen", response_model=ThreadResponse)
async def reopen_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Reopen a resolved thread.
    """
    return await update_thread(
        thread_id, ThreadUpdate(status=ThreadStatus.ACTIVE), current_user, db
    )


@router.post("/{thread_id}/archive", response_model=ThreadResponse)
async def archive_thread(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Archive a thread.
    """
    return await update_thread(
        thread_id, ThreadUpdate(status=ThreadStatus.ARCHIVED), current_user, db
    )


def _run_thread_summary_sync(thread_id: UUID) -> Optional[str]:
    """Drive the (sync-session) summarization service from a worker thread.

    Mirrors ``summarize_thread_task``: a fresh sync ``SessionLocal`` plus
    ``asyncio.run`` for the service's async LLM calls. Module-level (not a
    closure) so it can be unit-tested directly. Always closes its session.
    """
    import asyncio

    from src.core.database import SessionLocal
    from src.services.threads.thread_summarization_service import (
        get_thread_summarization_service,
    )

    sync_db = SessionLocal()
    try:
        service = get_thread_summarization_service(sync_db)
        return asyncio.run(service.generate_summary(thread_id, force=True))
    finally:
        sync_db.close()


@router.post("/{thread_id}/summarize", response_model=ThreadResponse)
async def regenerate_thread_summary(
    thread_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Regenerate AI summary for a thread.

    Forces regeneration regardless of rate limits.
    """
    chat_service = get_chat_service(db)
    thread = await chat_service.get_thread(thread_id, current_user.id)

    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found"
        )

    # The summarization service uses the SYNC SQLAlchemy API (db.query / commit)
    # and is shared with the Celery task, which drives it from a sync
    # SessionLocal + asyncio.run. The HTTP path must do the same: handing it the
    # request's AsyncSession raises AttributeError ("'AsyncSession' has no
    # attribute 'query'") and 500s every call. Run it in a worker thread with its
    # own sync session + event loop so the blocking DB I/O stays off the request
    # loop, mirroring the Celery driver exactly.
    import asyncio

    try:
        summary = await asyncio.to_thread(_run_thread_summary_sync, thread_id)

        if summary:
            logger.info(f"Regenerated summary for thread {thread_id}")
        else:
            logger.warning(f"Failed to generate summary for thread {thread_id}")

    except Exception as e:
        logger.error(f"Summary regeneration failed for thread {thread_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Summary generation failed",
        )

    # The sync service committed via its own session; refresh the async-session
    # copy so the response reflects the freshly written summary.
    await db.refresh(thread)

    return ThreadResponse(
        id=thread.id,
        conversation_id=thread.conversation_id,
        title=thread.title,
        summary=thread.summary,
        status=thread.status,
        last_message_at=thread.last_message_at,
        message_count=thread.message_count,
        token_count=thread.token_count,
        created_by_id=thread.created_by_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.get("/{thread_id}/context")
async def get_thread_context(
    thread_id: UUID,
    max_messages: int = Query(20, ge=1, le=100),
    max_tokens: int = Query(4000, ge=100, le=16000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get thread messages formatted for LLM context.

    Returns messages in the format expected by LLM APIs, limited by count or tokens.
    """
    service = get_chat_service(db)
    context = await service.get_thread_context(
        thread_id=thread_id,
        user_id=current_user.id,
        max_messages=max_messages,
        max_tokens=max_tokens,
    )

    if context is None:
        # R5-M10: service distinguishes missing/no-access (None) from a real,
        # possibly empty, context.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or access denied",
        )

    metadata = context.get("metadata", {})
    return {
        "thread_id": str(thread_id),
        "messages": context.get("messages", []),
        "message_count": metadata.get(
            "message_count", len(context.get("messages", []))
        ),
        "metadata": metadata,
    }


# =============================================================================
# Message Endpoints
# =============================================================================


@router.post(
    "/{thread_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def create_message(
    thread_id: UUID,
    data: ChatMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    [DEPRECATED] Create a new message in a thread.

    Audit finding C4: three public POST create-message routes coexist but only
    the flat ``POST /api/v2/messages`` (``create_message_standalone`` in
    ``api/threads/workspaces.py``) is called by any client — it is the
    canonical route. This nested variant has no live callers (frontend, CLI,
    scripts, or synthetic traffic) and is kept only for a deprecation window.
    New clients MUST use ``POST /api/v2/messages`` with ``thread_id`` in the
    body. Slated for removal in a follow-up cleanup PR once the window closes.

    See ``docs/decisions/api-deprecation-window.md`` for the deletion-eligible
    date and criteria.
    """
    # Ensure thread_id matches
    if data.thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Thread ID in path must match thread_id in body",
        )

    service = get_chat_service(db)
    message = await service.create_message(data, current_user.id)

    if not message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread not found or insufficient permissions",
        )

    # Broadcast message creation event via WebSocket
    try:
        thread = await service.get_thread(thread_id, current_user.id)
        if thread:
            await thread_event_service.broadcast_message_created(
                message_id=str(message.id),
                thread_id=str(thread_id),
                conversation_id=str(thread.conversation_id),
                user_id=str(current_user.id),
                organization_id=str(current_user.organization_id),
                role=(
                    message.role.value
                    if hasattr(message.role, "value")
                    else str(message.role)
                ),
                content_preview=message.content[:100] if message.content else None,
                has_citations=bool(message.citations),
            )
    except Exception as e:
        logger.error(f"Failed to broadcast message_created event: {e}")

    return _format_message_response(message)


@router.get("/{thread_id}/messages", response_model=ChatMessageListResponse)
async def list_messages(
    thread_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    before_id: Optional[UUID] = Query(
        None, description="Get messages before this message ID"
    ),
    since: Optional[datetime] = Query(
        None,
        description="Return only messages with created_at > since (ISO 8601)",
    ),
    order: Optional[Literal["asc", "desc"]] = Query(
        None,
        description="Sort order: asc (oldest first) or desc (newest first)",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List messages in a thread.
    """
    service = get_chat_service(db)
    messages, total = await service.list_messages(
        thread_id=thread_id,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        before_id=before_id,
        since=since,
        order=order or "asc",
    )

    page = (offset // limit) + 1 if limit > 0 else 1
    has_more = (offset + len(messages)) < total

    return ChatMessageListResponse(
        messages=[_format_message_response(m) for m in messages],
        total=total,
        page=page,
        limit=limit,
        has_more=has_more,
    )


@router.get("/{thread_id}/messages/{message_id}", response_model=ChatMessageResponse)
async def get_message(
    thread_id: UUID,
    message_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get a specific message by ID.
    """
    service = get_chat_service(db)
    message = await service.get_message(message_id, current_user.id)

    if not message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found or access denied",
        )

    # Verify thread matches
    if message.thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found in this thread",
        )

    return _format_message_response(message)


@router.patch("/{thread_id}/messages/{message_id}", response_model=ChatMessageResponse)
async def update_message_feedback(
    thread_id: UUID,
    message_id: UUID,
    data: ChatMessageUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update message feedback (rating and text).

    Note: Message content cannot be edited, only feedback can be updated.
    """
    service = get_chat_service(db)
    message = await service.update_message_feedback(message_id, data, current_user.id)

    if not message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found or access denied",
        )

    # Verify thread matches
    if message.thread_id != thread_id:
        # R5-M11: roll back the flushed feedback write before answering 404.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found in this thread",
        )

    await db.commit()
    return _format_message_response(message)


@router.delete(
    "/{thread_id}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_message(
    thread_id: UUID,
    message_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a message (soft delete).

    Only the message author or workspace admin can delete messages.
    """
    service = get_chat_service(db)

    # First verify the message belongs to this thread
    message = await service.get_message(message_id, current_user.id)
    if message and message.thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found in this thread",
        )

    success = await service.delete_message(message_id, current_user.id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found or insufficient permissions",
        )


# =============================================================================
# Helper Functions
# =============================================================================


async def _build_bulk_response(
    results: List[tuple],
    action: str,
    user_id: UUID,
    organization_id: str,
    include_threads: bool = True,
) -> BulkThreadResponse:
    """
    Build bulk response and broadcast events.

    Args:
        results: List of tuples (thread_id, success, error, [thread])
        action: Action name for event broadcasting ('resolved', 'archived', 'deleted')
        user_id: ID of the user performing the action
        organization_id: Owning organization — required so the bulk broadcast is
            tenant-scoped (a missed value would reopen the cross-tenant leak).
        include_threads: Whether to include thread objects in the response results
    """
    bulk_results = []
    succeeded_ids = []

    for result in results:
        thread_id = result[0]
        success = result[1]
        error = result[2]
        # Thread object might not be present if include_threads is False (e.g. delete)
        thread = result[3] if len(result) > 3 else None

        bulk_results.append(
            BulkThreadResult(
                thread_id=thread_id,
                success=success,
                error=error,
                thread=(
                    ThreadResponse.model_validate(thread)
                    if thread and include_threads
                    else None
                ),
            )
        )
        if success:
            succeeded_ids.append(str(thread_id))

    if succeeded_ids:
        await thread_event_service.broadcast_threads_bulk_updated(
            thread_ids=succeeded_ids,
            action=action,
            user_id=str(user_id),
            organization_id=str(organization_id),
        )

    return BulkThreadResponse(
        total=len(results),
        succeeded=len(succeeded_ids),
        failed=len(results) - len(succeeded_ids),
        results=bulk_results,
    )


def _format_message_response(message) -> ChatMessageResponse:
    """Format a ChatMessage model to ChatMessageResponse schema"""
    citations = []
    if message.citations:
        citations = [
            CitationResponse(
                id=c.id,
                document_id=c.document_id,
                external_reference_id=c.external_reference_id,  # For arXiv IDs, etc.
                source_position=c.source_position,
                chunk_index=c.chunk_index,
                chunk_id=c.chunk_id,
                snippet=c.snippet,
                snippet_preview=c.snippet_preview,
                page_number=c.page_number,
                score=c.score,
                rerank_score=c.rerank_score,
                # Use stored title/type for external refs, or get from document relationship
                document_title=c.document_title
                or (
                    c.document.title if hasattr(c, "document") and c.document else None
                ),
                document_type=c.document_type
                or (
                    c.document.document_type.value
                    if hasattr(c, "document")
                    and c.document
                    and c.document.document_type
                    else None
                ),
            )
            for c in message.citations
        ]

    attachments = []
    if message.attachments:
        attachments = [
            MessageAttachmentResponse(
                id=a.id,
                document_id=a.document_id,
                display_name=a.display_name,
                thumbnail_url=a.thumbnail_url,
                document_title=(
                    a.document.title if hasattr(a, "document") and a.document else None
                ),
                document_type=(
                    a.document.document_type.value
                    if hasattr(a, "document")
                    and a.document
                    and a.document.document_type
                    else None
                ),
                mime_type=(
                    a.document.mime_type
                    if hasattr(a, "document") and a.document
                    else None
                ),
            )
            for a in message.attachments
        ]

    return ChatMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        client_message_id=message.client_message_id,
        user_id=message.user_id,
        role=message.role,
        content=message.content,
        token_count=message.token_count,
        latency_ms=message.latency_ms,
        ttft_ms=message.ttft_ms,
        stopped=message.stopped,
        # Serve-time redaction of persisted raw tool args (see execute.py
        # get_thread_messages — same finding, same funnel discipline).
        tool_executions=redact_tool_executions(message.tool_executions),
        plan=message.plan,
        plan_reasoning=message.plan_reasoning,
        reasoning_summary=message.reasoning_summary,
        token_usage=message.token_usage,
        progress_steps=message.progress_steps,
        model_name=message.model_name,
        model_version=message.model_version,
        tool_name=message.tool_name,
        tool_call_id=message.tool_call_id,
        feedback_rating=message.feedback_rating,
        feedback_text=message.feedback_text,
        created_at=message.created_at,
        updated_at=message.updated_at,
        citations=citations,
        attachments=attachments,
    )
