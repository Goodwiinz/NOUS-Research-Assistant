"""
Thread and Message Search API endpoints.

Provides full-text search functionality for threads and chat messages
with filtering, highlighting, and relevance ranking.
"""

import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    status,
)
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from src.core.database import get_db_sync
from src.core.dependencies import get_current_user
from src.models.chat_message import MessageRole
from src.models.thread import ThreadStatus
from src.models.user import User
from src.services.threads.thread_message_search_service import (
    CombinedSearchResponse,
    MessageSearchFilter,
    MessageSearchRequest,
    MessageSearchResponse,
    MessageSearchSortOrder,
    ThreadSearchFilter,
    ThreadSearchRequest,
    ThreadSearchResponse,
    ThreadSearchSortOrder,
    thread_message_search_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Thread & Message Search"])


# =============================================================================
# Thread Search Endpoints
# =============================================================================


@router.post("/threads", response_model=ThreadSearchResponse)
def search_threads(
    request: ThreadSearchRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Search threads using PostgreSQL full-text search.

    Searches thread titles and summaries, and optionally includes threads
    that have matching messages. Returns relevance-ranked results with
    highlighted snippets.

    **Features:**
    - Full-text search on thread titles and summaries
    - Filter by conversation, workspace, status, date range
    - Sort by relevance, date, message count, or last activity
    - Highlighted matching terms in results
    - Shows count of matching messages per thread
    """
    try:
        result = thread_message_search_service.search_threads(
            request=request, user_id=current_user.id, db=db
        )
        return result
    except Exception:
        logger.error("Error in thread search", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


@router.get("/threads", response_model=ThreadSearchResponse)
def search_threads_get(
    query: str = Query(..., min_length=1, description="Search query"),
    conversation_id: Optional[UUID] = Query(
        None, description="Filter by conversation ID"
    ),
    workspace_id: Optional[UUID] = Query(None, description="Filter by workspace ID"),
    status_filter: Optional[List[str]] = Query(
        None, description="Filter by thread status (active, resolved, archived)"
    ),
    date_from: Optional[datetime] = Query(
        None, description="Filter threads created after this date"
    ),
    date_to: Optional[datetime] = Query(
        None, description="Filter threads created before this date"
    ),
    min_message_count: Optional[int] = Query(
        None, ge=0, description="Minimum message count"
    ),
    sort_order: str = Query(
        "relevance",
        description="Sort order: relevance, date_desc, date_asc, message_count, last_activity",
    ),
    limit: int = Query(20, ge=1, le=100, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Search threads using GET request with query parameters.

    A more RESTful alternative to POST for simple searches.
    """
    try:
        parsed_status = (
            [ThreadStatus(s) for s in status_filter] if status_filter else None
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Invalid status_filter value(s). Valid values: "
                f"{[e.value for e in ThreadStatus]}"
            ),
        )

    try:
        # Build filter object
        filters = (
            ThreadSearchFilter(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                status=parsed_status,
                date_from=date_from,
                date_to=date_to,
                min_message_count=min_message_count,
            )
            if any(
                [
                    conversation_id,
                    workspace_id,
                    status_filter,
                    date_from,
                    date_to,
                    min_message_count,
                ]
            )
            else None
        )

        # Map sort order string to enum
        sort_mapping = {
            "relevance": ThreadSearchSortOrder.RELEVANCE,
            "date_desc": ThreadSearchSortOrder.DATE_DESC,
            "date_asc": ThreadSearchSortOrder.DATE_ASC,
            "message_count": ThreadSearchSortOrder.MESSAGE_COUNT,
            "last_activity": ThreadSearchSortOrder.LAST_ACTIVITY,
        }
        sort_enum = sort_mapping.get(sort_order, ThreadSearchSortOrder.RELEVANCE)

        request = ThreadSearchRequest(
            query=query,
            filters=filters,
            sort_order=sort_enum,
            limit=limit,
            offset=offset,
        )

        result = thread_message_search_service.search_threads(
            request=request, user_id=current_user.id, db=db
        )
        return result
    except Exception:
        logger.error("Error in thread search", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


# =============================================================================
# Message Search Endpoints
# =============================================================================


@router.post("/messages", response_model=MessageSearchResponse)
def search_messages(
    request: MessageSearchRequest = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Search messages using PostgreSQL full-text search.

    Searches message content across all accessible threads.
    Returns relevance-ranked results with highlighted snippets
    and thread context.

    **Features:**
    - Full-text search on message content
    - Filter by thread, conversation, workspace, user, role
    - Filter by date range and citation presence
    - Sort by relevance or date
    - Highlighted matching terms in results
    - Thread context (title, conversation) in results
    """
    try:
        result = thread_message_search_service.search_messages(
            request=request, user_id=current_user.id, db=db
        )
        return result
    except Exception:
        logger.error("Error in message search", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


@router.get("/messages", response_model=MessageSearchResponse)
def search_messages_get(
    query: str = Query(..., min_length=1, description="Search query"),
    thread_id: Optional[UUID] = Query(None, description="Filter by thread ID"),
    conversation_id: Optional[UUID] = Query(
        None, description="Filter by conversation ID"
    ),
    workspace_id: Optional[UUID] = Query(None, description="Filter by workspace ID"),
    user_id: Optional[UUID] = Query(
        None, description="Filter by user ID (message author)"
    ),
    roles: Optional[List[str]] = Query(
        None, description="Filter by message role (user, assistant, system, tool)"
    ),
    date_from: Optional[datetime] = Query(
        None, description="Filter messages created after this date"
    ),
    date_to: Optional[datetime] = Query(
        None, description="Filter messages created before this date"
    ),
    has_citations: Optional[bool] = Query(
        None, description="Filter by citation presence"
    ),
    sort_order: str = Query(
        "relevance", description="Sort order: relevance, date_desc, date_asc"
    ),
    limit: int = Query(20, ge=1, le=100, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Results offset for pagination"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Search messages using GET request with query parameters.

    A more RESTful alternative to POST for simple searches.
    """
    try:
        parsed_roles = [MessageRole(r) for r in roles] if roles else None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Invalid roles value(s). Valid values: "
                f"{[e.value for e in MessageRole]}"
            ),
        )

    try:
        # Build filter object
        filters = (
            MessageSearchFilter(
                thread_id=thread_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                user_id=user_id,
                roles=parsed_roles,
                date_from=date_from,
                date_to=date_to,
                has_citations=has_citations,
            )
            if any(
                [
                    thread_id,
                    conversation_id,
                    workspace_id,
                    user_id,
                    roles,
                    date_from,
                    date_to,
                    has_citations is not None,
                ]
            )
            else None
        )

        # Map sort order string to enum
        sort_mapping = {
            "relevance": MessageSearchSortOrder.RELEVANCE,
            "date_desc": MessageSearchSortOrder.DATE_DESC,
            "date_asc": MessageSearchSortOrder.DATE_ASC,
        }
        sort_enum = sort_mapping.get(sort_order, MessageSearchSortOrder.RELEVANCE)

        request = MessageSearchRequest(
            query=query,
            filters=filters,
            sort_order=sort_enum,
            limit=limit,
            offset=offset,
        )

        result = thread_message_search_service.search_messages(
            request=request, user_id=current_user.id, db=db
        )
        return result
    except Exception:
        logger.error("Error in message search", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


# =============================================================================
# Combined Search Endpoint
# =============================================================================


@router.get("/combined", response_model=CombinedSearchResponse)
def combined_search(
    query: str = Query(..., min_length=1, description="Search query"),
    workspace_id: Optional[UUID] = Query(None, description="Filter by workspace ID"),
    conversation_id: Optional[UUID] = Query(
        None, description="Filter by conversation ID"
    ),
    limit: int = Query(20, ge=1, le=50, description="Max results to return"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Search across both threads and messages in a single query.

    Returns a combined list of results ranked by relevance.
    Thread results are given slightly higher weight than messages.

    **Use cases:**
    - Quick search across all content
    - Workspace-wide or conversation-wide search
    - Finding related threads and messages together
    """
    try:
        result = thread_message_search_service.combined_search(
            query=query,
            user_id=current_user.id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            limit=limit,
            db=db,
        )
        return result
    except Exception:
        logger.error("Error in combined search", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


# =============================================================================
# Search Suggestions Endpoint
# =============================================================================


@router.get("/suggestions")
def get_search_suggestions(
    query: str = Query(..., min_length=1, description="Partial search query"),
    workspace_id: Optional[UUID] = Query(
        None, description="Limit suggestions to workspace"
    ),
    limit: int = Query(5, ge=1, le=10, description="Max suggestions to return"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_sync),
):
    """
    Get search suggestions based on thread titles and recent searches.

    Provides autocomplete functionality for the search input.
    """
    try:
        # Simple suggestion based on thread titles
        from sqlalchemy import text

        def _escape_like(value: str) -> str:
            """Escape SQL LIKE special characters."""
            return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        # Scope to workspaces the caller can access (owner / public / member).
        # The previous ``organization_id = :org OR organization_id IS NULL``
        # clause leaked every NULL-org (personal) workspace's thread titles to
        # all users, and within an org showed all members' titles regardless of
        # membership.
        suggestion_sql = """
            SELECT t.title
            FROM threads t
            JOIN conversations c ON t.conversation_id = c.id
            JOIN workspaces w ON c.workspace_id = w.id
            WHERE t.is_deleted = false
                AND c.is_deleted = false
                AND w.is_deleted = false
                AND t.title IS NOT NULL
                AND LOWER(t.title) LIKE LOWER(:query_pattern)
                AND (
                    w.owner_id = :access_user_id
                    OR w.is_public = true
                    OR EXISTS (
                        SELECT 1 FROM workspace_members wm
                        WHERE wm.workspace_id = w.id
                          AND wm.user_id = :access_user_id
                          AND wm.is_deleted = false
                    )
                )
        """

        params = {
            "query_pattern": f"%{_escape_like(query)}%",
            "access_user_id": str(current_user.id),
        }

        if workspace_id:
            suggestion_sql += " AND w.id = :workspace_id"
            params["workspace_id"] = str(workspace_id)

        # GOO-399: one row per title, most recent first. PostgreSQL rejects
        # SELECT DISTINCT with an ORDER BY column outside the select list, so
        # the previous form answered 500 on every call.
        suggestion_sql += (
            " GROUP BY t.title ORDER BY MAX(t.last_message_at) DESC LIMIT :limit"
        )
        params["limit"] = limit

        result = db.execute(text(suggestion_sql), params)
        suggestions = [row.title for row in result if row.title]

        return {"query": query, "suggestions": suggestions}
    except (ProgrammingError, OperationalError) as e:
        # Don't mask a structurally broken query (bad column/join, param
        # mismatch on the access predicate) as "no suggestions" — that would
        # hide a broken authz query behind an empty 200 forever. Surface it
        # like the sibling search endpoints do.
        logger.error(f"Search suggestions query failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Suggestions failed",
        )
    except Exception as e:
        # Genuinely unexpected (non-SQL) errors: fail soft to empty suggestions.
        logger.error(f"Error getting search suggestions: {e}")
        return {"query": query, "suggestions": []}


# =============================================================================
# Search Health Check
# =============================================================================


@router.get("/health")
def search_health_check(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
):
    """
    Check health of the thread/message search system.

    Verifies that full-text search indexes exist and are functional.

    R5-M13: this endpoint carried no auth dependency, so any unauthenticated
    caller could enumerate the raw GIN index DDL (``pg_indexes.indexdef``) for
    the threads/chat_messages tables plus FTS liveness — schema disclosure.
    Now gated on ``get_current_user`` (same convention as the WS status
    endpoints fixed in #1493), and the response reports index *names* and a
    boolean per index rather than echoing the DDL text.
    """
    try:
        from sqlalchemy import text

        # Check if GIN indexes exist. Only the index name is surfaced below —
        # not indexdef (the raw DDL) — since the auth gate above narrows who
        # can call this, not what a caller who legitimately can should see.
        # pg_indexes renders the access method in lower case ("USING gin"),
        # so the match must be case-insensitive (Q-P3: `LIKE '%GIN%'` never
        # matched and this endpoint reported index_count 0 on a healthy DB).
        # An INVALID index (an interrupted CONCURRENTLY build) serves no
        # query, so it must not count as healthy either.
        index_check_sql = """
            SELECT x.indexname, x.indexdef
            FROM pg_indexes x
            JOIN pg_class c
                ON c.relname = x.indexname
                AND c.relnamespace = x.schemaname::regnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE x.tablename IN ('threads', 'chat_messages')
                AND i.indisvalid
                AND x.indexdef ILIKE '%USING gin%'
        """

        result = db.execute(text(index_check_sql))
        indexes = [{"name": row.indexname, "gin": True} for row in result]

        # Try a simple search to verify functionality
        test_search_sql = """
            SELECT COUNT(*) FROM threads
            WHERE search_vector @@ plainto_tsquery('test')
        """

        try:
            db.execute(text(test_search_sql))
            search_functional = True
        except Exception:
            search_functional = False

        return {
            "status": "healthy" if search_functional else "degraded",
            "search_functional": search_functional,
            "gin_indexes": indexes,
            "index_count": len(indexes),
        }
    except Exception as e:
        logger.error(f"Search health check failed: {e}")
        return {"status": "unhealthy", "error": "thread search unavailable"}
