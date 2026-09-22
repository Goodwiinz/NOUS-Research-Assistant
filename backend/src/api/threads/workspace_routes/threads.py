"""
Thread endpoints, nested and standalone (Task 4.2 split of the former
monolithic ``backend/src/api/threads/workspaces.py``; Task 4.3 moved the
persistence logic into ``src/services/threads/thread_service.py``; PR 3 Task
3.2 moved the single request commit UP to these handlers — the service flushes
(the old ``create_thread(commit=...)`` flag is gone), each mutating handler ends
with one ``await db.commit()``). Transport + transaction-boundary only.

``standalone_list_router`` (``list_threads_standalone``) is kept as a separate
router object from ``standalone_router`` (thread CRUD) purely to preserve the
exact route registration order the 4.1 contract test
(``backend/tests/unit/api/test_workspace_route_contract.py``) pins: in the
original file, ``list_threads_standalone`` was defined *after* the standalone
message routes, not alongside the other standalone thread routes.
``workspace_routes/__init__.py`` composes it into ``standalone_router`` at
that same later position.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.thread import ThreadStatus
from src.models.user import User
from src.schemas.chat import (
    ThreadCreate,
    ThreadDetailResponse,
    ThreadListResponse,
    ThreadResponse,
    ThreadUpdate,
)
from src.services.threads import thread_service, workspace_access

from .dependencies import _get_thread_or_404
from .presenters import _thread_to_detail_response, _thread_to_response

router = APIRouter(prefix="/api/v2/workspaces", tags=["workspaces"])

# Standalone router for flat API paths (used by frontend) — thread CRUD.
standalone_router = APIRouter(prefix="/api/v2", tags=["workspaces-flat"])

# See module docstring: list_threads_standalone registers later in the
# original file's order (after the standalone message routes), so it is
# composed into the final standalone_router at that position separately.
standalone_list_router = APIRouter(prefix="/api/v2", tags=["workspaces-flat"])

# ============================================================================
# Thread Endpoints
# ============================================================================


@router.post(
    "/{workspace_id}/conversations/{conversation_id}/threads",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_thread(
    workspace_id: UUID,
    conversation_id: UUID,
    request: ThreadCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    """Create a new thread in a conversation"""
    request.conversation_id = conversation_id
    try:
        thread = await thread_service.create_thread(
            db, request, current_user.id, workspace_id=workspace_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not thread:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()
    return _thread_to_response(thread)


@router.get(
    "/{workspace_id}/threads",
    response_model=ThreadListResponse,
)
async def list_workspace_threads(
    workspace_id: UUID,
    status_filter: Optional[str] = Query(
        None, description="Filter by status: active, resolved, archived"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadListResponse:
    """List threads across every live conversation in a workspace."""
    status_enum = None
    if status_filter:
        try:
            status_enum = ThreadStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid status: {status_filter}"
            )

    offset = (page - 1) * limit
    result = await thread_service.list_workspace_threads(
        db,
        workspace_id,
        current_user.id,
        status_filter=status_enum,
        limit=limit,
        offset=offset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    threads, total, previews = result
    return ThreadListResponse(
        threads=[
            _thread_to_response(thread, last_message_preview=previews.get(thread.id))
            for thread in threads
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=(offset + len(threads)) < total,
    )


@router.get(
    "/{workspace_id}/conversations/{conversation_id}/threads",
    response_model=ThreadListResponse,
)
async def list_threads(
    workspace_id: UUID,
    conversation_id: UUID,
    status_filter: Optional[str] = Query(
        None, description="Filter by status: active, resolved, archived"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadListResponse:
    """List threads in a conversation"""
    status_enum = None
    if status_filter:
        try:
            status_enum = ThreadStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid status: {status_filter}"
            )

    offset = (page - 1) * limit
    result = await thread_service.list_threads(
        db,
        conversation_id,
        current_user.id,
        workspace_id=workspace_id,
        status_filter=status_enum,
        limit=limit,
        offset=offset,
        with_preview=True,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    threads, total, previews = result

    return ThreadListResponse(
        threads=[
            _thread_to_response(t, last_message_preview=previews.get(t.id))
            for t in threads
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=(offset + len(threads)) < total,
    )


@router.get(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
    response_model=ThreadDetailResponse,
)
async def get_thread(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    include_messages: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadDetailResponse:
    """Get thread details with messages"""
    thread = await _get_thread_or_404(
        db, workspace_id, conversation_id, thread_id, current_user
    )

    return _thread_to_detail_response(thread, include_messages)


@router.patch(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
    response_model=ThreadResponse,
)
async def update_thread(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    request: ThreadUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    """Update thread details"""
    try:
        thread = await thread_service.update_thread(
            db,
            thread_id,
            request,
            current_user.id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    await db.commit()
    return _thread_to_response(thread)


@router.delete(
    "/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_thread(
    workspace_id: UUID,
    conversation_id: UUID,
    thread_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a thread"""
    try:
        deleted = await thread_service.delete_thread(
            db,
            thread_id,
            current_user.id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            stamp_deleted_at=True,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")

    await db.commit()


# ============================================================================
# Standalone (Flat) Thread Routes
# These routes allow direct access without full path hierarchy
# ============================================================================


@standalone_router.post(
    "/threads", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED
)
async def create_thread_standalone(
    request: ThreadCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    """Create a new thread (standalone route - uses conversation_id from request body)"""
    try:
        thread = await thread_service.create_thread(db, request, current_user.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not thread:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.commit()
    return _thread_to_response(thread)


@standalone_router.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
async def get_thread_standalone(
    thread_id: UUID,
    include_messages: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadDetailResponse:
    """Get thread details with messages (standalone route)"""
    thread = await workspace_access.get_thread(
        db, thread_id, current_user.id, include_messages=include_messages
    )
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    return _thread_to_detail_response(thread, include_messages)


@standalone_router.patch("/threads/{thread_id}", response_model=ThreadResponse)
async def update_thread_standalone(
    thread_id: UUID,
    request: ThreadUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    """Update thread details (standalone route)"""
    try:
        thread = await thread_service.update_thread(
            db, thread_id, request, current_user.id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    await db.commit()
    return _thread_to_response(thread)


@standalone_router.delete(
    "/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_thread_standalone(
    thread_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-delete a thread (standalone route)"""
    try:
        deleted = await thread_service.delete_thread(
            db, thread_id, current_user.id, stamp_deleted_at=True
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")

    await db.commit()


@standalone_list_router.get(
    "/conversations/{conversation_id}/threads", response_model=ThreadListResponse
)
async def list_threads_standalone(
    conversation_id: UUID,
    status_filter: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ThreadListResponse:
    """List threads in a conversation (standalone route)"""
    status_enum = None
    if status_filter:
        try:
            status_enum = ThreadStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid status: {status_filter}"
            )

    offset = (page - 1) * limit
    result = await thread_service.list_threads(
        db,
        conversation_id,
        current_user.id,
        status_filter=status_enum,
        limit=limit,
        offset=offset,
        with_preview=True,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    threads, total, previews = result

    return ThreadListResponse(
        threads=[
            _thread_to_response(t, last_message_preview=previews.get(t.id))
            for t in threads
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=(offset + len(threads)) < total,
    )
