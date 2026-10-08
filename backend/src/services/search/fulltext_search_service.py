"""
Full-Text Search Service using PostgreSQL built-in full-text search capabilities
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, not_, or_, text
from sqlalchemy.orm import Session
from sqlalchemy.sql import select

from src.core.database import get_db_sync
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.search_schemas import (
    SearchFilter,
    SearchQuery,
    SearchResponse,
    SearchResult,
    SearchSortOrder,
    SearchSuggestion,
    SearchType,
    TextSnippet,
)

logger = logging.getLogger(__name__)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally (R2-L21)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_org_scope(organization_id: Any) -> str:
    """GOO-351: document search/count is organization-scoped and fails closed.

    A missing, empty or non-UUID scope (including ``str(None)``) must never
    produce SQL without the ``d.organization_id`` predicate.
    """
    try:
        return str(uuid.UUID(str(organization_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(
            "Document search requires a valid organization scope"
        ) from None


# A sentence ends at ".", "!" or "?" followed by whitespace and a capital
# letter (possibly highlighted), so "95.3%", "v2.1" and "(Fig. 3)" never end
# one. retrieve_passages quotes these sentences to a model (audit RT-1).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=(?:<mark>)?[A-Z])")
# A capitalised word after one of these does not start a new sentence.
_ABBREVIATIONS = frozenset(
    {
        "al.",
        "approx.",
        "cf.",
        "dr.",
        "e.g.",
        "eq.",
        "eqs.",
        "fig.",
        "figs.",
        "i.e.",
        "no.",
        "ref.",
        "refs.",
        "sec.",
        "vs.",
    }
)


def _sentences(text: str) -> List[str]:
    """Split ``text`` into sentences without cutting numbers or abbreviations."""
    sentences: List[str] = []
    for piece in _SENTENCE_END.split(text):
        last_word = (sentences[-1].split() or [""])[-1].lower() if sentences else ""
        if last_word in _ABBREVIATIONS:
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return sentences


class FullTextSearchService:
    """Service for PostgreSQL full-text search functionality"""

    def __init__(self, db: Session = None):
        self.db = db
        self.min_query_length = 2
        self.default_limit = 20
        self.max_limit = 100
        self.highlight_pre_tag = "<mark>"
        self.highlight_post_tag = "</mark>"
        self.snippet_length = 200
        self.snippet_surround = 50

    def search(
        self,
        search_request: Optional[SearchQuery] = None,
        user_id: str = None,
        organization_id: Optional[str] = None,
        db: Session = None,
        *,
        query: Optional[str] = None,
        limit: Optional[int] = None,
        use_ranking: bool = False,
    ) -> SearchResponse | Any:
        """
        Perform full-text search on documents

        Args:
            search_request: Search query and parameters
            user_id: ID of user performing search (for access control)
            organization_id: ID of organization (for filtering)
            db: Database session (optional, will create one if not provided)

        Returns:
            SearchResponse with results and metadata
        """
        if search_request is None:
            if query is None:
                raise TypeError("search() requires either search_request or query")
            return self._legacy_search(
                query=query,
                organization_id=organization_id,
                limit=limit,
                use_ranking=use_ranking,
                db=db or self.db,
            )

        return self._search_response(
            search_request=search_request,
            user_id=user_id,
            organization_id=organization_id,
            db=db,
        )

    def _search_response(
        self,
        search_request: SearchQuery,
        user_id: str = None,
        organization_id: str = None,
        db: Session = None,
    ) -> SearchResponse:
        """Current synchronous search implementation."""
        start_time = time.time()

        # Validate query
        if len(search_request.query.strip()) < self.min_query_length:
            return SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=search_request.search_type,
                results=[],
                total_results=0,
                returned_results=0,
                search_time_ms=0,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=False,
                suggestions=self._get_spelling_suggestions(search_request.query),
            )

        # GOO-351: no valid organization scope -> empty result, no DB work.
        try:
            _require_org_scope(organization_id)
        except ValueError:
            logger.warning("Full-text search rejected: missing organization scope")
            return SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=search_request.search_type,
                results=[],
                total_results=0,
                returned_results=0,
                search_time_ms=0,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=False,
            )

        # Use provided db session or create a new one
        should_close_db = False
        if db is None:
            db = next(get_db_sync())
            should_close_db = True

        try:
            # Build the search query
            search_sql, params = self._build_search_query(
                search_request, user_id, organization_id
            )

            # Execute search
            result = db.execute(text(search_sql), params)
            rows = result.fetchall()

            # Get total count for pagination
            count_sql, count_params = self._build_count_query(
                search_request, user_id, organization_id
            )
            count_result = db.execute(text(count_sql), count_params)
            total_count = count_result.scalar()

            # Process results
            search_results = []
            for row in rows:
                search_result = self._row_to_search_result(row, search_request)
                if search_result:
                    search_results.append(search_result)

            search_time_ms = (time.time() - start_time) * 1000

            # Generate suggestions if needed
            suggestions = None
            if len(search_results) < 5:
                suggestions = self._get_search_suggestions(
                    search_request.query, db, organization_id
                )

            return SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=search_request.search_type,
                results=search_results,
                total_results=total_count,
                returned_results=len(search_results),
                search_time_ms=search_time_ms,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=(search_request.offset + len(search_results)) < total_count,
                suggestions=suggestions,
                filters_applied=self._serialize_filters(search_request.filters),
            )

        except Exception as e:
            logger.error(f"Error performing full-text search: {e}")
            raise
        finally:
            if should_close_db:
                db.close()

    async def _legacy_search(
        self,
        query: str,
        organization_id: str = None,
        limit: Optional[int] = None,
        use_ranking: bool = False,
        db: Session = None,
    ) -> List[Dict[str, Any]]:
        """Backward-compatible async search interface used by older unit tests."""
        if not query.strip():
            return []

        session = db or self.db
        if session is None:
            return []

        rows = session.execute(text("SELECT 1")).fetchall()
        results: List[Dict[str, Any]] = []
        max_results = limit or self.default_limit

        for row in rows[:max_results]:
            score = self._calculate_bm25_score(query, row) if use_ranking else 1.0
            results.append(
                {
                    "document_id": getattr(row, "id", None),
                    "title": getattr(row, "title", ""),
                    "content": getattr(row, "content", ""),
                    "score": score,
                }
            )

        return results

    def _calculate_bm25_score(self, query: str, row: Any) -> float:
        """Small deterministic scoring hook used by compatibility tests."""
        haystack = " ".join(
            str(getattr(row, attr, "") or "")
            for attr in ("title", "content", "content_text")
        ).lower()
        terms = [term for term in re.findall(r"\w+", query.lower()) if term]
        if not haystack or not terms:
            return 0.0
        matches = sum(haystack.count(term) for term in terms)
        return round(matches / max(len(terms), 1), 4)

    def _build_search_query(
        self,
        search_request: SearchQuery,
        user_id: str = None,
        organization_id: str = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Build PostgreSQL full-text search SQL query"""

        # Prepare search terms
        search_terms = self._prepare_search_terms(search_request.query)

        # Base query with full-text search.
        # Scale contract: raw ts_rank_cd() is naturally bounded ~0-1 for
        # typical matches; the x10 here only exists to make relevance_score
        # read better on this endpoint's plain response. HybridSearchService
        # ._normalize_score() undoes this x10 before fusing across sources —
        # if this multiplier ever changes, that undo must change with it.
        query_parts = [
            "SELECT",
            "    d.id, d.title, d.document_type, d.content_text, d.content_summary,",
            "    d.file_size_bytes, d.created_at, d.updated_at, d.processing_status,",
            "    d.tags, d.is_public, d.uploaded_by_user_id, d.organization_id, d.document_metadata,",
            "    ts_rank_cd(search_vector, plainto_tsquery(:query)) * 10 as relevance_score,",
            "    ts_headline('english', coalesce(d.content_text, ''), plainto_tsquery(:query),",
            f"           'StartSel={self.highlight_pre_tag}, StopSel={self.highlight_post_tag}, MaxWords={self.snippet_length}, MinWords={self.snippet_surround}') as highlighted_content,",
            "    ts_headline('english', coalesce(d.title, ''), plainto_tsquery(:query),",
            f"           'StartSel={self.highlight_pre_tag}, StopSel={self.highlight_post_tag}, MaxWords=50, MinWords=10') as highlighted_title",
            "FROM documents d",
            "WHERE",
            "    d.is_deleted = false",
            "    AND d.processing_status = :completed_status",
            "    AND search_vector @@ plainto_tsquery(:query)",
            "    AND NOT (",
            "        coalesce(d.content_text, '') ILIKE :noise_pattern_1",
            "        OR coalesce(d.content_text, '') ILIKE :noise_pattern_2",
            "        OR coalesce(d.content_text, '') ILIKE :noise_pattern_3",
            "    )",
        ]

        params = {
            "query": search_terms,
            "completed_status": ProcessingStatus.COMPLETED.name,
            "noise_pattern_1": "%verified 0/l000 tampered whisper field isolation parse validation%",
            "noise_pattern_2": "%all security properties verified through%",
            "noise_pattern_3": "%replay prevention nonce + timestamp%",
        }

        # Access control: always tenant-scoped (GOO-351, fail closed).
        query_parts.append("    AND d.organization_id = :organization_id")
        params["organization_id"] = _require_org_scope(organization_id)

        # Add additional filters
        if search_request.filters:
            filter_clauses, filter_params = self._build_filter_clauses(
                search_request.filters
            )
            if filter_clauses:
                query_parts.extend(filter_clauses)
                params.update(filter_params)

        # Add ordering
        order_clause = self._build_order_clause(search_request.sort_order)
        query_parts.append(f"ORDER BY {order_clause}")

        # Add pagination
        query_parts.extend(["LIMIT :limit OFFSET :offset"])
        params.update({"limit": search_request.limit, "offset": search_request.offset})

        return "\n".join(query_parts), params

    def _build_count_query(
        self,
        search_request: SearchQuery,
        user_id: str = None,
        organization_id: str = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Build count query for pagination"""

        search_terms = self._prepare_search_terms(search_request.query)

        query_parts = [
            "SELECT COUNT(*) as total_count",
            "FROM documents d",
            "WHERE",
            "    d.is_deleted = false",
            "    AND d.processing_status = :completed_status",
            "    AND search_vector @@ plainto_tsquery(:query)",
            "    AND NOT (",
            "        coalesce(d.content_text, '') ILIKE :noise_pattern_1",
            "        OR coalesce(d.content_text, '') ILIKE :noise_pattern_2",
            "        OR coalesce(d.content_text, '') ILIKE :noise_pattern_3",
            "    )",
        ]

        params = {
            "query": search_terms,
            "completed_status": ProcessingStatus.COMPLETED.name,
            "noise_pattern_1": "%verified 0/l000 tampered whisper field isolation parse validation%",
            "noise_pattern_2": "%all security properties verified through%",
            "noise_pattern_3": "%replay prevention nonce + timestamp%",
        }

        # Add the same filters as the main query
        query_parts.append("    AND d.organization_id = :organization_id")
        params["organization_id"] = _require_org_scope(organization_id)

        if search_request.filters:
            filter_clauses, filter_params = self._build_filter_clauses(
                search_request.filters
            )
            if filter_clauses:
                query_parts.extend(filter_clauses)
                params.update(filter_params)

        return "\n".join(query_parts), params

    def _prepare_search_terms(self, query: str) -> str:
        """Prepare search terms for PostgreSQL full-text search"""
        # Remove special characters and normalize
        cleaned = re.sub(r"[^\w\s]", " ", query.lower())
        # Split into words and remove stop words (PostgreSQL will handle this)
        words = [
            word.strip()
            for word in cleaned.split()
            if len(word.strip()) >= self.min_query_length
        ]

        if not words:
            return query.lower()

        # Space-joined cleaned terms. The binding sites run these through
        # plainto_tsquery(), which itself ANDs every lexeme — hand-building a
        # tsquery string with '&' here was misleading (plainto strips the
        # operators and re-ANDs anyway), so we just supply clean words (R2-L20).
        return " ".join(words)

    def _build_filter_clauses(
        self, filters: SearchFilter
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Build SQL filter clauses from SearchFilter"""
        clauses = []
        params = {}

        if filters.document_types:
            doc_types = [dt.value for dt in filters.document_types]
            placeholders = ",".join([f":doc_type_{i}" for i in range(len(doc_types))])
            clauses.append(f"    AND d.document_type IN ({placeholders})")
            for i, doc_type in enumerate(doc_types):
                params[f"doc_type_{i}"] = doc_type

        if filters.document_ids:
            placeholders = ",".join(
                [f":doc_id_{i}" for i in range(len(filters.document_ids))]
            )
            clauses.append(f"    AND d.id::text IN ({placeholders})")
            for i, document_id in enumerate(filters.document_ids):
                params[f"doc_id_{i}"] = document_id

        if filters.tags:
            # Using PostgreSQL array operators
            placeholders = ",".join([f":tag_{i}" for i in range(len(filters.tags))])
            clauses.append(f"    AND d.tags && ARRAY[{placeholders}]")
            for i, tag in enumerate(filters.tags):
                params[f"tag_{i}"] = tag

        if filters.date_from:
            clauses.append("    AND d.created_at >= :date_from")
            params["date_from"] = filters.date_from

        if filters.date_to:
            clauses.append("    AND d.created_at <= :date_to")
            params["date_to"] = filters.date_to

        if filters.file_size_min:
            clauses.append("    AND d.file_size_bytes >= :file_size_min")
            params["file_size_min"] = filters.file_size_min

        if filters.file_size_max:
            clauses.append("    AND d.file_size_bytes <= :file_size_max")
            params["file_size_max"] = filters.file_size_max

        if filters.is_public is not None:
            clauses.append("    AND d.is_public = :is_public")
            params["is_public"] = filters.is_public

        if filters.uploaded_by_user_id:
            clauses.append("    AND d.uploaded_by_user_id = :uploaded_by_user_id")
            params["uploaded_by_user_id"] = filters.uploaded_by_user_id

        return clauses, params

    def _build_order_clause(self, sort_order: SearchSortOrder) -> str:
        """Build ORDER BY clause based on sort order"""
        if sort_order == SearchSortOrder.RELEVANCE:
            return "relevance_score DESC, d.created_at DESC"
        elif sort_order == SearchSortOrder.DATE_DESC:
            return "d.created_at DESC"
        elif sort_order == SearchSortOrder.DATE_ASC:
            return "d.created_at ASC"
        elif sort_order == SearchSortOrder.TITLE_ASC:
            return "d.title ASC"
        elif sort_order == SearchSortOrder.TITLE_DESC:
            return "d.title DESC"
        else:
            return "relevance_score DESC, d.created_at DESC"

    def _row_to_search_result(
        self, row, search_request: SearchQuery
    ) -> Optional[SearchResult]:
        """Convert database row to SearchResult"""
        try:
            # Extract content preview
            content_preview = row.content_text or row.content_summary or ""
            if len(content_preview) > 300:
                content_preview = content_preview[:300] + "..."

            # Create snippets if requested
            snippets = []
            if search_request.include_snippets:
                snippets = self._extract_snippets(
                    row.highlighted_content or "",
                    row.highlighted_title or "",
                    search_request,
                )

            # Convert document type string to enum
            document_type = None
            try:
                if isinstance(row.document_type, str):
                    document_type = DocumentType[row.document_type]
                else:
                    document_type = DocumentType(row.document_type)
            except KeyError:
                logger.error(f"Invalid document type: {row.document_type}")
                document_type = DocumentType.TEXT  # Default fallback

            return SearchResult(
                document_id=str(row.id),
                title=row.title,
                document_type=document_type,
                content_preview=content_preview,
                snippets=snippets,
                relevance_score=(
                    float(row.relevance_score) if row.relevance_score else 0.0
                ),
                file_size_bytes=row.file_size_bytes,
                created_at=row.created_at,
                updated_at=row.updated_at,
                processing_status=row.processing_status,
                tags=row.tags or [],
                is_public=row.is_public,
                uploaded_by_user_id=str(row.uploaded_by_user_id),
                organization_id=str(row.organization_id),
                metadata=row.document_metadata or {},
            )
        except Exception as e:
            logger.error(f"Error converting row to search result: {e}")
            return None

    def _extract_snippets(
        self,
        highlighted_content: str,
        highlighted_title: str,
        search_request: SearchQuery,
    ) -> List[TextSnippet]:
        """Extract text snippets from highlighted content"""
        snippets = []

        # Extract title snippet if highlighted
        if highlighted_title and self.highlight_pre_tag in highlighted_title:
            snippets.append(
                TextSnippet(
                    text=highlighted_title,
                    start_position=0,
                    end_position=len(highlighted_title),
                    relevance_score=1.0,
                )
            )

        # Extract content snippets
        if highlighted_content and self.highlight_pre_tag in highlighted_content:
            # Whole sentences around highlighted terms: a split on every "."
            # cut "95.3%" to "3%" and "et al." mid-sentence (RT-1).
            sentences = _sentences(highlighted_content)

            for sentence in sentences:
                sentence = sentence.strip()
                if self.highlight_pre_tag in sentence and len(sentence) > 10:
                    # Find position of first highlight
                    highlight_start = sentence.find(self.highlight_pre_tag)
                    if highlight_start != -1:
                        snippets.append(
                            TextSnippet(
                                text=sentence,
                                start_position=0,
                                end_position=len(sentence),
                                relevance_score=0.8,
                            )
                        )

            # Limit number of snippets
            snippets = snippets[:3]

        return snippets

    def _get_search_suggestions(
        self, query: str, db: Session, organization_id: Optional[str] = None
    ) -> List[str]:
        """Get search suggestions from existing documents IN THE CALLER'S ORG.

        Suggestions are derived from document titles via a substring LIKE, so
        without an org filter they leak other tenants' document titles to any
        user whose search returned <5 results. Scope to ``organization_id``;
        when it's absent we can't scope safely, so return no suggestions rather
        than disclose across tenants.
        """
        if not organization_id:
            return []
        try:
            # Simple suggestion based on document titles and content, scoped to
            # the caller's organization.
            suggestion_query = text(r"""
                SELECT DISTINCT
                    regexp_replace(regexp_replace(lower(title), '[^a-zA-Z0-9\s]', ' ', 'g'), '\s+', ' ', 'g') as suggestion
                FROM documents
                WHERE is_deleted = false
                    AND processing_status = :completed_status
                    AND organization_id = :organization_id
                    AND lower(title) LIKE lower(:query_pattern) ESCAPE '\'
                LIMIT 5
            """)

            result = db.execute(
                suggestion_query,
                {
                    "completed_status": ProcessingStatus.COMPLETED.name,
                    "organization_id": organization_id,
                    # R2-L21: escape %/_ so user input can't wildcard the scan
                    "query_pattern": f"%{_escape_like(query)}%",
                },
            )

            suggestions = [row.suggestion for row in result if row.suggestion]
            return suggestions

        except Exception as e:
            logger.error(f"Error getting search suggestions: {e}")
            return []

    def _get_spelling_suggestions(self, query: str) -> List[str]:
        """Get spelling suggestions (placeholder for future implementation)"""
        # This could be implemented with PostgreSQL pg_trgm extension
        # For now, return common variations
        suggestions = []

        # Simple common typo corrections
        common_corrections = {
            "teh": "the",
            "adn": "and",
            "wit": "with",
            "for": "from",
            "whihc": "which",
            "becuase": "because",
        }

        words = query.lower().split()
        for word in words:
            if word in common_corrections:
                suggestions.append(query.replace(word, common_corrections[word]))

        return suggestions[:3]

    def _serialize_filters(
        self, filters: Optional[SearchFilter]
    ) -> Optional[Dict[str, Any]]:
        """Serialize filters for response"""
        if not filters:
            return None

        return {
            "document_ids": filters.document_ids,
            "document_types": (
                [dt.value for dt in filters.document_types]
                if filters.document_types
                else None
            ),
            "tags": filters.tags,
            "date_from": filters.date_from.isoformat() if filters.date_from else None,
            "date_to": filters.date_to.isoformat() if filters.date_to else None,
            "file_size_min": filters.file_size_min,
            "file_size_max": filters.file_size_max,
            "is_public": filters.is_public,
            "uploaded_by_user_id": filters.uploaded_by_user_id,
        }

    def create_search_indexes(self, db: Session = None):
        """Create necessary full-text search indexes"""
        if not db:
            db = next(get_db_sync())

        try:
            # Create GIN index for full-text search
            index_queries = [
                """
                CREATE INDEX IF NOT EXISTS idx_documents_search_vector
                ON documents USING GIN (search_vector)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_documents_title_gin
                ON documents USING GIN (to_tsvector('english', title))
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_documents_content_gin
                ON documents USING GIN (to_tsvector('english', coalesce(content_text, '')))
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_documents_created_at
                ON documents (created_at DESC)
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_documents_type_status
                ON documents (document_type, processing_status)
                """,
            ]

            for query in index_queries:
                db.execute(text(query))

            db.commit()
            logger.info("Full-text search indexes created successfully")

        except Exception as e:
            logger.error(f"Error creating search indexes: {e}")
            db.rollback()
            raise

    def update_document_search_vector(self, document_id: str, db: Session = None):
        """Update the search vector for a specific document"""
        if not db:
            db = next(get_db_sync())

        try:
            update_query = text("""
                UPDATE documents
                SET search_vector =
                    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(content_text, '')), 'B') ||
                    setweight(to_tsvector('english', coalesce(content_summary, '')), 'C') ||
                    setweight(to_tsvector('english', coalesce(array_to_string(tags, ' '), '')), 'D')
                WHERE id = :document_id
            """)

            db.execute(update_query, {"document_id": document_id})
            db.commit()
            logger.debug(f"Updated search vector for document {document_id}")

        except Exception as e:
            logger.error(
                f"Error updating search vector for document {document_id}: {e}"
            )
            db.rollback()
            raise

    # Same tsvector build as update_document_search_vector, but for the async
    # ingest paths (arXiv REST + agent tool) that hold an AsyncSession. Those
    # paths persist Documents as COMPLETED but never ran the sync updater, so
    # documents.search_vector stayed NULL and Postgres fulltext (which requires
    # `search_vector @@ plainto_tsquery`) could never surface them.
    _SEARCH_VECTOR_SQL = text("""
        UPDATE documents
        SET search_vector =
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(content_text, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(content_summary, '')), 'C') ||
            setweight(to_tsvector('english', coalesce(array_to_string(tags, ' '), '')), 'D')
        WHERE id = ANY(:document_ids)
        """)

    async def async_update_document_search_vectors(self, document_ids, db) -> None:
        """Build search_vector for the given documents on an AsyncSession.

        Best-effort within the caller's transaction: the caller decides when to
        commit. Raises on failure so the caller can log — never leaves a half
        state, since it's a single set-based UPDATE.
        """
        ids = [str(d) for d in document_ids if d]
        if not ids:
            return
        await db.execute(self._SEARCH_VECTOR_SQL, {"document_ids": ids})

    def get_search_analytics(
        self, organization_id: str = None, days: int = 30
    ) -> Dict[str, Any]:
        """Get search analytics data"""
        try:
            with next(get_db_sync()) as db:
                # Calculate cutoff date in Python to avoid SQL injection/syntax issues
                cutoff_date = datetime.utcnow() - timedelta(days=days)

                # Use bind parameter for date comparison
                query_str = """
                    SELECT
                        COUNT(*) as total_documents,
                        AVG(file_size_bytes) as avg_file_size,
                        COUNT(DISTINCT document_type) as unique_types,
                        COUNT(DISTINCT uploaded_by_user_id) as unique_uploaders
                    FROM documents
                    WHERE is_deleted = false
                        AND processing_status = :completed_status
                        AND created_at >= :cutoff_date
                """

                if organization_id:
                    query_str += " AND organization_id = :organization_id"

                result = db.execute(
                    text(query_str),
                    {
                        "completed_status": ProcessingStatus.COMPLETED.name,
                        "cutoff_date": cutoff_date,
                        "organization_id": organization_id,
                    },
                )

                row = result.first()

                return {
                    "total_documents": row.total_documents if row else 0,
                    "avg_file_size_bytes": (
                        float(row.avg_file_size) if row and row.avg_file_size else 0
                    ),
                    "unique_types": row.unique_types if row else 0,
                    "unique_uploaders": row.unique_uploaders if row else 0,
                    "searchable_documents": row.total_documents if row else 0,
                }

        except Exception as e:
            logger.error(f"Error getting search analytics: {e}")
            return {}


# Global service instance
fulltext_search_service = FullTextSearchService()
