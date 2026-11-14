"""
Thread and Message Full-Text Search Service

Provides PostgreSQL full-text search capabilities for threads and chat messages
with support for filtering, highlighting, and relevance ranking.
"""

import logging
import re
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, or_, text
from sqlalchemy.orm import Session

from src.core.database import get_db_sync
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.thread import Thread, ThreadStatus

logger = logging.getLogger(__name__)


# Workspace access predicate shared by every search query builder. The search
# builders previously accepted ``user_id`` but never applied it, so any
# authenticated user could full-text search every workspace's threads and
# messages across all tenants. This mirrors
# ``ChatService._user_can_access_workspace`` (owner OR public OR active member)
# as a SQL predicate. Bound as ``:access_user_id`` — a distinct name from the
# ``:user_id`` message-author search filter so the two never collide.
_WORKSPACE_ACCESS_PREDICATE = (
    "    AND (\n"
    "        w.owner_id = :access_user_id\n"
    "        OR w.is_public = true\n"
    "        OR EXISTS (\n"
    "            SELECT 1 FROM workspace_members wm\n"
    "            WHERE wm.workspace_id = w.id\n"
    "              AND wm.user_id = :access_user_id\n"
    "              AND wm.is_deleted = false\n"
    "        )\n"
    "    )"
)


# ============================================================================
# Search Schemas
# ============================================================================


# Legacy shadow agent threads (pre server-canonical cutover) duplicate the
# workspace copy of every /chat turn, so FTS returned two hits per match.
# They are excluded from search; the rows stay readable via the agent API.
_EXCLUDE_LEGACY_AGENT_THREADS = (
    "    AND NOT (COALESCE(t.rag_document_scope, '{}'::jsonb) "
    '@> \'{"source": "agent"}\'::jsonb)'
)


# Edit-and-resend tombstones. Search is a display surface: a message the user
# replaced must not be findable by its old text, and the count twin must carry
# the identical predicate or `total` over-reports and `has_more` goes phantom.
_EXCLUDE_SUPERSEDED_MESSAGES = "    AND m.superseded_by_message_id IS NULL"


class ThreadSearchSortOrder(str, Enum):
    """Sort order options for thread search"""

    RELEVANCE = "relevance"
    DATE_DESC = "date_desc"
    DATE_ASC = "date_asc"
    MESSAGE_COUNT = "message_count"
    LAST_ACTIVITY = "last_activity"


class MessageSearchSortOrder(str, Enum):
    """Sort order options for message search"""

    RELEVANCE = "relevance"
    DATE_DESC = "date_desc"
    DATE_ASC = "date_asc"


class ThreadSearchFilter(BaseModel):
    """Filters for thread search"""

    conversation_id: Optional[UUID] = None
    workspace_id: Optional[UUID] = None
    status: Optional[List[ThreadStatus]] = None
    created_by_id: Optional[UUID] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    min_message_count: Optional[int] = None


class MessageSearchFilter(BaseModel):
    """Filters for message search"""

    thread_id: Optional[UUID] = None
    conversation_id: Optional[UUID] = None
    workspace_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    roles: Optional[List[MessageRole]] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    has_citations: Optional[bool] = None


class ThreadSearchRequest(BaseModel):
    """Thread search request"""

    query: str = Field(..., min_length=1)
    filters: Optional[ThreadSearchFilter] = None
    sort_order: ThreadSearchSortOrder = ThreadSearchSortOrder.RELEVANCE
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    include_snippets: bool = True
    include_messages: bool = False


class MessageSearchRequest(BaseModel):
    """Message search request"""

    query: str = Field(..., min_length=1)
    filters: Optional[MessageSearchFilter] = None
    sort_order: MessageSearchSortOrder = MessageSearchSortOrder.RELEVANCE
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    include_context: bool = True


class ThreadSearchResult(BaseModel):
    """Individual thread search result"""

    thread_id: UUID
    title: Optional[str] = None
    summary: Optional[str] = None
    status: str
    conversation_id: UUID
    relevance_score: float
    message_count: int
    last_message_at: datetime
    created_at: datetime
    highlighted_title: Optional[str] = None
    highlighted_summary: Optional[str] = None
    matching_message_count: Optional[int] = None

    class Config:
        from_attributes = True


class MessageSearchResult(BaseModel):
    """Individual message search result"""

    message_id: UUID
    thread_id: UUID
    content: str
    role: str
    user_id: Optional[UUID] = None
    relevance_score: float
    created_at: datetime
    highlighted_content: Optional[str] = None
    thread_title: Optional[str] = None
    conversation_id: Optional[UUID] = None
    citation_count: int = 0

    class Config:
        from_attributes = True


class ThreadSearchResponse(BaseModel):
    """Thread search response"""

    query: str
    search_id: str
    results: List[ThreadSearchResult]
    total_results: int
    returned_results: int
    search_time_ms: float
    limit: int
    offset: int
    has_more: bool
    filters_applied: Optional[Dict[str, Any]] = None


class MessageSearchResponse(BaseModel):
    """Message search response"""

    query: str
    search_id: str
    results: List[MessageSearchResult]
    total_results: int
    returned_results: int
    search_time_ms: float
    limit: int
    offset: int
    has_more: bool
    filters_applied: Optional[Dict[str, Any]] = None


class CombinedSearchResult(BaseModel):
    """Combined search result (thread or message)"""

    result_type: str  # "thread" or "message"
    id: UUID
    relevance_score: float
    title: Optional[str] = None
    content: Optional[str] = None
    snippet: str
    thread_id: Optional[UUID] = None
    conversation_id: Optional[UUID] = None
    created_at: datetime


class CombinedSearchResponse(BaseModel):
    """Combined search response"""

    query: str
    search_id: str
    results: List[CombinedSearchResult]
    total_results: int
    search_time_ms: float
    has_more: bool


# ============================================================================
# Search Service
# ============================================================================


class ThreadMessageSearchService:
    """Service for full-text search across threads and messages"""

    def __init__(self):
        self.min_query_length = 2
        self.default_limit = 20
        self.max_limit = 100
        self.highlight_pre_tag = "<mark>"
        self.highlight_post_tag = "</mark>"
        self.snippet_length = 150
        self.snippet_surround = 30

    def search_threads(
        self, request: ThreadSearchRequest, user_id: UUID, db: Session = None
    ) -> ThreadSearchResponse:
        """
        Search threads by title and summary with full-text search.

        Args:
            request: Search request with query and filters
            user_id: User performing the search (for access control)
            db: Database session

        Returns:
            ThreadSearchResponse with results and metadata
        """
        start_time = time.time()
        search_id = str(uuid.uuid4())

        # Validate query
        if len(request.query.strip()) < self.min_query_length:
            return ThreadSearchResponse(
                query=request.query,
                search_id=search_id,
                results=[],
                total_results=0,
                returned_results=0,
                search_time_ms=0,
                limit=request.limit,
                offset=request.offset,
                has_more=False,
            )

        should_close_db = False
        if db is None:
            db = next(get_db_sync())
            should_close_db = True

        try:
            search_terms = self._prepare_search_terms(request.query)

            # Build the search query
            query_sql, params = self._build_thread_search_query(
                search_terms, request, user_id
            )

            # Execute search
            result = db.execute(text(query_sql), params)
            rows = result.fetchall()

            # Get total count
            count_sql, count_params = self._build_thread_count_query(
                search_terms, request, user_id
            )
            count_result = db.execute(text(count_sql), count_params)
            total_count = count_result.scalar() or 0

            # Process results
            search_results = []
            for row in rows:
                search_result = ThreadSearchResult(
                    thread_id=row.id,
                    title=row.title,
                    summary=row.summary,
                    status=(
                        row.status if isinstance(row.status, str) else row.status.value
                    ),
                    conversation_id=row.conversation_id,
                    relevance_score=(
                        float(row.relevance_score) if row.relevance_score else 0.0
                    ),
                    message_count=row.message_count or 0,
                    last_message_at=row.last_message_at,
                    created_at=row.created_at,
                    highlighted_title=(
                        row.highlighted_title
                        if hasattr(row, "highlighted_title")
                        else None
                    ),
                    highlighted_summary=(
                        row.highlighted_summary
                        if hasattr(row, "highlighted_summary")
                        else None
                    ),
                    matching_message_count=(
                        row.matching_message_count
                        if hasattr(row, "matching_message_count")
                        else None
                    ),
                )
                search_results.append(search_result)

            search_time_ms = (time.time() - start_time) * 1000

            return ThreadSearchResponse(
                query=request.query,
                search_id=search_id,
                results=search_results,
                total_results=total_count,
                returned_results=len(search_results),
                search_time_ms=search_time_ms,
                limit=request.limit,
                offset=request.offset,
                has_more=(request.offset + len(search_results)) < total_count,
                filters_applied=self._serialize_thread_filters(request.filters),
            )

        except Exception as e:
            logger.error(f"Error performing thread search: {e}")
            raise
        finally:
            if should_close_db:
                db.close()

    def search_messages(
        self, request: MessageSearchRequest, user_id: UUID, db: Session = None
    ) -> MessageSearchResponse:
        """
        Search messages by content with full-text search.

        Args:
            request: Search request with query and filters
            user_id: User performing the search (for access control)
            db: Database session

        Returns:
            MessageSearchResponse with results and metadata
        """
        start_time = time.time()
        search_id = str(uuid.uuid4())

        # Validate query
        if len(request.query.strip()) < self.min_query_length:
            return MessageSearchResponse(
                query=request.query,
                search_id=search_id,
                results=[],
                total_results=0,
                returned_results=0,
                search_time_ms=0,
                limit=request.limit,
                offset=request.offset,
                has_more=False,
            )

        should_close_db = False
        if db is None:
            db = next(get_db_sync())
            should_close_db = True

        try:
            search_terms = self._prepare_search_terms(request.query)

            # Build the search query
            query_sql, params = self._build_message_search_query(
                search_terms, request, user_id
            )

            # Execute search
            result = db.execute(text(query_sql), params)
            rows = result.fetchall()

            # Get total count
            count_sql, count_params = self._build_message_count_query(
                search_terms, request, user_id
            )
            count_result = db.execute(text(count_sql), count_params)
            total_count = count_result.scalar() or 0

            # Process results
            search_results = []
            for row in rows:
                search_result = MessageSearchResult(
                    message_id=row.id,
                    thread_id=row.thread_id,
                    content=(
                        row.content[:500] + "..."
                        if len(row.content) > 500
                        else row.content
                    ),
                    role=row.role if isinstance(row.role, str) else row.role.value,
                    user_id=row.user_id,
                    relevance_score=(
                        float(row.relevance_score) if row.relevance_score else 0.0
                    ),
                    created_at=row.created_at,
                    highlighted_content=(
                        row.highlighted_content
                        if hasattr(row, "highlighted_content")
                        else None
                    ),
                    thread_title=(
                        row.thread_title if hasattr(row, "thread_title") else None
                    ),
                    conversation_id=(
                        row.conversation_id if hasattr(row, "conversation_id") else None
                    ),
                    citation_count=(
                        row.citation_count if hasattr(row, "citation_count") else 0
                    ),
                )
                search_results.append(search_result)

            search_time_ms = (time.time() - start_time) * 1000

            return MessageSearchResponse(
                query=request.query,
                search_id=search_id,
                results=search_results,
                total_results=total_count,
                returned_results=len(search_results),
                search_time_ms=search_time_ms,
                limit=request.limit,
                offset=request.offset,
                has_more=(request.offset + len(search_results)) < total_count,
                filters_applied=self._serialize_message_filters(request.filters),
            )

        except Exception as e:
            logger.error(f"Error performing message search: {e}")
            raise
        finally:
            if should_close_db:
                db.close()

    def combined_search(
        self,
        query: str,
        user_id: UUID,
        workspace_id: Optional[UUID] = None,
        conversation_id: Optional[UUID] = None,
        limit: int = 20,
        db: Session = None,
    ) -> CombinedSearchResponse:
        """
        Search across both threads and messages, returning combined results.

        Args:
            query: Search query
            user_id: User performing search
            workspace_id: Optional workspace filter
            conversation_id: Optional conversation filter
            limit: Max results to return
            db: Database session

        Returns:
            CombinedSearchResponse with unified results
        """
        start_time = time.time()
        search_id = str(uuid.uuid4())

        if len(query.strip()) < self.min_query_length:
            return CombinedSearchResponse(
                query=query,
                search_id=search_id,
                results=[],
                total_results=0,
                search_time_ms=0,
                has_more=False,
            )

        should_close_db = False
        if db is None:
            db = next(get_db_sync())
            should_close_db = True

        try:
            search_terms = self._prepare_search_terms(query)

            # Combined query using UNION
            combined_sql = """
                WITH thread_matches AS (
                    SELECT 
                        'thread' as result_type,
                        t.id,
                        ts_rank_cd(t.search_vector, plainto_tsquery(:query)) * 1.5 as relevance_score,
                        t.title,
                        NULL as content,
                        COALESCE(t.summary, t.title, 'Thread') as snippet,
                        t.id as thread_id,
                        t.conversation_id,
                        t.created_at
                    FROM threads t
                    JOIN conversations c ON t.conversation_id = c.id
                    JOIN workspaces w ON c.workspace_id = w.id
                    WHERE t.search_vector @@ plainto_tsquery(:query)
                        AND t.is_deleted = false
                        AND c.is_deleted = false
                        AND w.is_deleted = false
                        {thread_filters}
                ),
                message_matches AS (
                    SELECT 
                        'message' as result_type,
                        m.id,
                        ts_rank_cd(m.search_vector, plainto_tsquery(:query)) as relevance_score,
                        t.title,
                        m.content,
                        SUBSTRING(m.content, 1, 200) as snippet,
                        m.thread_id,
                        t.conversation_id,
                        m.created_at
                    FROM chat_messages m
                    JOIN threads t ON m.thread_id = t.id
                    JOIN conversations c ON t.conversation_id = c.id
                    JOIN workspaces w ON c.workspace_id = w.id
                    WHERE m.search_vector @@ plainto_tsquery(:query)
                        AND t.is_deleted = false
                        AND c.is_deleted = false
                        AND w.is_deleted = false
                        AND m.superseded_by_message_id IS NULL
                        {message_filters}
                )
                SELECT * FROM (
                    SELECT * FROM thread_matches
                    UNION ALL
                    SELECT * FROM message_matches
                ) combined
                ORDER BY relevance_score DESC
                LIMIT :limit
            """

            # Build filters. Seed both CTE filter strings with the workspace
            # access predicate so the combined search is tenant-scoped like the
            # thread/message builders (both CTEs alias the workspace as ``w``).
            # Both CTEs also drop legacy shadow agent threads, like the
            # thread/message builders; without it every /chat turn matched twice.
            thread_filters = _EXCLUDE_LEGACY_AGENT_THREADS + _WORKSPACE_ACCESS_PREDICATE
            message_filters = _EXCLUDE_LEGACY_AGENT_THREADS + _WORKSPACE_ACCESS_PREDICATE
            params = {
                "query": search_terms,
                "limit": limit,
                "access_user_id": str(user_id),
            }

            if workspace_id:
                thread_filters += " AND w.id = :workspace_id"
                message_filters += " AND w.id = :workspace_id"
                params["workspace_id"] = str(workspace_id)

            if conversation_id:
                thread_filters += " AND t.conversation_id = :conversation_id"
                message_filters += " AND t.conversation_id = :conversation_id"
                params["conversation_id"] = str(conversation_id)

            # Format SQL with filters
            combined_sql = combined_sql.format(
                thread_filters=thread_filters, message_filters=message_filters
            )

            # Execute query
            result = db.execute(text(combined_sql), params)
            rows = result.fetchall()

            # Process results
            search_results = []
            for row in rows:
                search_results.append(
                    CombinedSearchResult(
                        result_type=row.result_type,
                        id=row.id,
                        relevance_score=(
                            float(row.relevance_score) if row.relevance_score else 0.0
                        ),
                        title=row.title,
                        content=row.content,
                        snippet=row.snippet or "",
                        thread_id=row.thread_id,
                        conversation_id=row.conversation_id,
                        created_at=row.created_at,
                    )
                )

            search_time_ms = (time.time() - start_time) * 1000

            return CombinedSearchResponse(
                query=query,
                search_id=search_id,
                results=search_results,
                total_results=len(search_results),
                search_time_ms=search_time_ms,
                has_more=len(search_results) >= limit,
            )

        except Exception as e:
            logger.error(f"Error performing combined search: {e}")
            raise
        finally:
            if should_close_db:
                db.close()

    def _prepare_search_terms(self, query: str) -> str:
        """Prepare search terms for PostgreSQL full-text search"""
        # Remove special characters and normalize
        cleaned = re.sub(r"[^\w\s]", " ", query.lower())
        # Split into words and filter short ones
        words = [
            word.strip()
            for word in cleaned.split()
            if len(word.strip()) >= self.min_query_length
        ]

        if not words:
            return query.lower()

        # Join with & for AND semantics
        return " & ".join(words)

    @staticmethod
    def _apply_thread_filters(
        query_parts: List[str], params: Dict[str, Any], filters: Any
    ) -> None:
        """Append thread-search filter predicates + params.

        Shared by BOTH the search and count builders so the count can never
        drift from the result set. (The count query previously applied only
        conversation_id/workspace_id/status, omitting created_by_id/date_from/
        date_to/min_message_count → over-reported total + phantom has_more.)
        Predicates use the same table aliases (t/c) present in both queries.
        """
        if filters.conversation_id:
            query_parts.append("    AND t.conversation_id = :conversation_id")
            params["conversation_id"] = str(filters.conversation_id)
        if filters.workspace_id:
            query_parts.append("    AND c.workspace_id = :workspace_id")
            params["workspace_id"] = str(filters.workspace_id)
        if filters.status:
            status_values = [s.value for s in filters.status]
            placeholders = ",".join([f":status_{i}" for i in range(len(status_values))])
            query_parts.append(f"    AND t.status IN ({placeholders})")
            for i, status in enumerate(status_values):
                params[f"status_{i}"] = status
        if filters.created_by_id:
            query_parts.append("    AND t.created_by_id = :created_by_id")
            params["created_by_id"] = str(filters.created_by_id)
        if filters.date_from:
            query_parts.append("    AND t.created_at >= :date_from")
            params["date_from"] = filters.date_from
        if filters.date_to:
            query_parts.append("    AND t.created_at <= :date_to")
            params["date_to"] = filters.date_to
        if filters.min_message_count:
            query_parts.append("    AND t.message_count >= :min_message_count")
            params["min_message_count"] = filters.min_message_count

    @staticmethod
    def _apply_message_filters(
        query_parts: List[str], params: Dict[str, Any], filters: Any
    ) -> None:
        """Append message-search filter predicates + params.

        Shared by BOTH the search and count builders. (The count query
        previously applied only thread_id/conversation_id/workspace_id, omitting
        user_id/roles/date_from/date_to/has_citations → over-reported total +
        phantom has_more.) Predicates use the same aliases (m/t/c) in both.
        """
        if filters.thread_id:
            query_parts.append("    AND m.thread_id = :thread_id")
            params["thread_id"] = str(filters.thread_id)
        if filters.conversation_id:
            query_parts.append("    AND t.conversation_id = :conversation_id")
            params["conversation_id"] = str(filters.conversation_id)
        if filters.workspace_id:
            query_parts.append("    AND c.workspace_id = :workspace_id")
            params["workspace_id"] = str(filters.workspace_id)
        if filters.user_id:
            query_parts.append("    AND m.user_id = :user_id")
            params["user_id"] = str(filters.user_id)
        if filters.roles:
            role_values = [r.value for r in filters.roles]
            placeholders = ",".join([f":role_{i}" for i in range(len(role_values))])
            query_parts.append(f"    AND m.role IN ({placeholders})")
            for i, role in enumerate(role_values):
                params[f"role_{i}"] = role
        if filters.date_from:
            query_parts.append("    AND m.created_at >= :date_from")
            params["date_from"] = filters.date_from
        if filters.date_to:
            query_parts.append("    AND m.created_at <= :date_to")
            params["date_to"] = filters.date_to
        if filters.has_citations is not None:
            if filters.has_citations:
                query_parts.append(
                    "    AND EXISTS (SELECT 1 FROM citations cit "
                    "WHERE cit.message_id = m.id)"
                )
            else:
                query_parts.append(
                    "    AND NOT EXISTS (SELECT 1 FROM citations cit "
                    "WHERE cit.message_id = m.id)"
                )

    def _build_thread_search_query(
        self, search_terms: str, request: ThreadSearchRequest, user_id: UUID
    ) -> Tuple[str, Dict[str, Any]]:
        """Build PostgreSQL thread search query"""

        query_parts = [
            "SELECT",
            "    t.id, t.title, t.summary, t.status, t.conversation_id,",
            "    t.message_count, t.last_message_at, t.created_at,",
            "    ts_rank_cd(t.search_vector, plainto_tsquery(:query)) * 10 as relevance_score,",
            f"    ts_headline('english', COALESCE(t.title, ''), plainto_tsquery(:query), 'StartSel={self.highlight_pre_tag}, StopSel={self.highlight_post_tag}, MaxWords=50, MinWords=10') as highlighted_title,",
            f"    ts_headline('english', COALESCE(t.summary, ''), plainto_tsquery(:query), 'StartSel={self.highlight_pre_tag}, StopSel={self.highlight_post_tag}, MaxWords={self.snippet_length}, MinWords={self.snippet_surround}') as highlighted_summary,",
            "    (SELECT COUNT(*) FROM chat_messages cm WHERE cm.thread_id = t.id AND cm.search_vector @@ plainto_tsquery(:query) AND cm.superseded_by_message_id IS NULL) as matching_message_count",
            "FROM threads t",
            "JOIN conversations c ON t.conversation_id = c.id",
            "JOIN workspaces w ON c.workspace_id = w.id",
            "WHERE",
            "    t.is_deleted = false",
            _EXCLUDE_LEGACY_AGENT_THREADS,
            "    AND c.is_deleted = false",
            "    AND w.is_deleted = false",
            "    AND (",
            "        t.search_vector @@ plainto_tsquery(:query)",
            "        OR EXISTS (SELECT 1 FROM chat_messages cm WHERE cm.thread_id = t.id AND cm.search_vector @@ plainto_tsquery(:query) AND cm.superseded_by_message_id IS NULL)",
            "    )",
            _WORKSPACE_ACCESS_PREDICATE,
        ]

        params = {"query": search_terms, "access_user_id": str(user_id)}

        # Add filters (shared with the count builder via _apply_thread_filters)
        if request.filters:
            self._apply_thread_filters(query_parts, params, request.filters)

        # Add ordering
        order_clause = self._build_thread_order_clause(request.sort_order)
        query_parts.append(f"ORDER BY {order_clause}")

        # Add pagination
        query_parts.append("LIMIT :limit OFFSET :offset")
        params["limit"] = request.limit
        params["offset"] = request.offset

        return "\n".join(query_parts), params

    def _build_thread_count_query(
        self, search_terms: str, request: ThreadSearchRequest, user_id: UUID
    ) -> Tuple[str, Dict[str, Any]]:
        """Build count query for thread search"""

        query_parts = [
            "SELECT COUNT(*) as total",
            "FROM threads t",
            "JOIN conversations c ON t.conversation_id = c.id",
            "JOIN workspaces w ON c.workspace_id = w.id",
            "WHERE",
            "    t.is_deleted = false",
            _EXCLUDE_LEGACY_AGENT_THREADS,
            "    AND c.is_deleted = false",
            "    AND w.is_deleted = false",
            "    AND (",
            "        t.search_vector @@ plainto_tsquery(:query)",
            "        OR EXISTS (SELECT 1 FROM chat_messages cm WHERE cm.thread_id = t.id AND cm.search_vector @@ plainto_tsquery(:query) AND cm.superseded_by_message_id IS NULL)",
            "    )",
            _WORKSPACE_ACCESS_PREDICATE,
        ]

        params = {"query": search_terms, "access_user_id": str(user_id)}

        # Same filters as the result query — applied via the shared helper so
        # the count can't drift (previously omitted created_by_id/date_from/
        # date_to/min_message_count → over-counted total + phantom has_more).
        if request.filters:
            self._apply_thread_filters(query_parts, params, request.filters)

        return "\n".join(query_parts), params

    def _build_message_search_query(
        self, search_terms: str, request: MessageSearchRequest, user_id: UUID
    ) -> Tuple[str, Dict[str, Any]]:
        """Build PostgreSQL message search query"""

        query_parts = [
            "SELECT",
            "    m.id, m.thread_id, m.content, m.role, m.user_id, m.created_at,",
            "    ts_rank_cd(m.search_vector, plainto_tsquery(:query)) * 10 as relevance_score,",
            f"    ts_headline('english', COALESCE(m.content, ''), plainto_tsquery(:query), 'StartSel={self.highlight_pre_tag}, StopSel={self.highlight_post_tag}, MaxWords={self.snippet_length}, MinWords={self.snippet_surround}') as highlighted_content,",
            "    t.title as thread_title,",
            "    t.conversation_id,",
            "    (SELECT COUNT(*) FROM citations cit WHERE cit.message_id = m.id) as citation_count",
            "FROM chat_messages m",
            "JOIN threads t ON m.thread_id = t.id",
            "JOIN conversations c ON t.conversation_id = c.id",
            "JOIN workspaces w ON c.workspace_id = w.id",
            "WHERE",
            "    m.search_vector @@ plainto_tsquery(:query)",
            "    AND t.is_deleted = false",
            _EXCLUDE_LEGACY_AGENT_THREADS,
            "    AND c.is_deleted = false",
            "    AND w.is_deleted = false",
            # Same-filters rule: page and count must move in lockstep, and an
            # edited-away message must not be findable by its old text.
            _EXCLUDE_SUPERSEDED_MESSAGES,
            _WORKSPACE_ACCESS_PREDICATE,
        ]

        params = {"query": search_terms, "access_user_id": str(user_id)}

        # Add filters (shared with the count builder via _apply_message_filters)
        if request.filters:
            self._apply_message_filters(query_parts, params, request.filters)

        # Add ordering
        order_clause = self._build_message_order_clause(request.sort_order)
        query_parts.append(f"ORDER BY {order_clause}")

        # Add pagination
        query_parts.append("LIMIT :limit OFFSET :offset")
        params["limit"] = request.limit
        params["offset"] = request.offset

        return "\n".join(query_parts), params

    def _build_message_count_query(
        self, search_terms: str, request: MessageSearchRequest, user_id: UUID
    ) -> Tuple[str, Dict[str, Any]]:
        """Build count query for message search"""

        query_parts = [
            "SELECT COUNT(*) as total",
            "FROM chat_messages m",
            "JOIN threads t ON m.thread_id = t.id",
            "JOIN conversations c ON t.conversation_id = c.id",
            "JOIN workspaces w ON c.workspace_id = w.id",
            "WHERE",
            "    m.search_vector @@ plainto_tsquery(:query)",
            "    AND t.is_deleted = false",
            _EXCLUDE_LEGACY_AGENT_THREADS,
            "    AND c.is_deleted = false",
            "    AND w.is_deleted = false",
            # Same-filters rule: page and count must move in lockstep, and an
            # edited-away message must not be findable by its old text.
            _EXCLUDE_SUPERSEDED_MESSAGES,
            _WORKSPACE_ACCESS_PREDICATE,
        ]

        params = {"query": search_terms, "access_user_id": str(user_id)}

        # Same filters as the result query — applied via the shared helper so
        # the count can't drift (previously omitted user_id/roles/date_from/
        # date_to/has_citations → over-counted total + phantom has_more).
        if request.filters:
            self._apply_message_filters(query_parts, params, request.filters)

        return "\n".join(query_parts), params

    def _build_thread_order_clause(self, sort_order: ThreadSearchSortOrder) -> str:
        """Build ORDER BY clause for thread search"""
        if sort_order == ThreadSearchSortOrder.RELEVANCE:
            return "relevance_score DESC, t.last_message_at DESC"
        elif sort_order == ThreadSearchSortOrder.DATE_DESC:
            return "t.created_at DESC"
        elif sort_order == ThreadSearchSortOrder.DATE_ASC:
            return "t.created_at ASC"
        elif sort_order == ThreadSearchSortOrder.MESSAGE_COUNT:
            return "t.message_count DESC, relevance_score DESC"
        elif sort_order == ThreadSearchSortOrder.LAST_ACTIVITY:
            return "t.last_message_at DESC"
        return "relevance_score DESC, t.last_message_at DESC"

    def _build_message_order_clause(self, sort_order: MessageSearchSortOrder) -> str:
        """Build ORDER BY clause for message search"""
        if sort_order == MessageSearchSortOrder.RELEVANCE:
            return "relevance_score DESC, m.created_at DESC"
        elif sort_order == MessageSearchSortOrder.DATE_DESC:
            return "m.created_at DESC"
        elif sort_order == MessageSearchSortOrder.DATE_ASC:
            return "m.created_at ASC"
        return "relevance_score DESC, m.created_at DESC"

    def _serialize_thread_filters(
        self, filters: Optional[ThreadSearchFilter]
    ) -> Optional[Dict[str, Any]]:
        """Serialize thread filters for response"""
        if not filters:
            return None

        return {
            "conversation_id": (
                str(filters.conversation_id) if filters.conversation_id else None
            ),
            "workspace_id": str(filters.workspace_id) if filters.workspace_id else None,
            "status": [s.value for s in filters.status] if filters.status else None,
            "created_by_id": (
                str(filters.created_by_id) if filters.created_by_id else None
            ),
            "date_from": filters.date_from.isoformat() if filters.date_from else None,
            "date_to": filters.date_to.isoformat() if filters.date_to else None,
            "min_message_count": filters.min_message_count,
        }

    def _serialize_message_filters(
        self, filters: Optional[MessageSearchFilter]
    ) -> Optional[Dict[str, Any]]:
        """Serialize message filters for response"""
        if not filters:
            return None

        return {
            "thread_id": str(filters.thread_id) if filters.thread_id else None,
            "conversation_id": (
                str(filters.conversation_id) if filters.conversation_id else None
            ),
            "workspace_id": str(filters.workspace_id) if filters.workspace_id else None,
            "user_id": str(filters.user_id) if filters.user_id else None,
            "roles": [r.value for r in filters.roles] if filters.roles else None,
            "date_from": filters.date_from.isoformat() if filters.date_from else None,
            "date_to": filters.date_to.isoformat() if filters.date_to else None,
            "has_citations": filters.has_citations,
        }


# Global service instance
thread_message_search_service = ThreadMessageSearchService()
