"""
Search API endpoints for hybrid search functionality
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import func, text

from src.api.research.chat import (
    RAG_SYSTEM_PROMPT,
    RetrievedContext,
    build_context_prompt,
)
from src.api.search._tenancy import require_org_id as _require_org_id
from src.core.api_key_auth import APIKeyData, APIKeyUsageLog, get_api_key_data
from src.core.database import get_db_sync
from src.core.dependencies import get_current_user
from src.models.search_analytics import SearchAnalyticsEvent
from src.models.search_schemas import (
    DeterministicTrace,
    SearchAnalytics,
    SearchIndex,
    SearchQuery,
    SearchResponse,
    SearchResult,
    SearchSortOrder,
    SearchSuggestion,
    SearchType,
)
from src.models.user import User
from src.services.infrastructure.azure_openai_service import azure_openai_service
from src.services.search.fulltext_search_service import fulltext_search_service
from src.services.search.hybrid_search_service import (
    HybridSearchUnavailableError,
    hybrid_search_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


def _ensure_deterministic_response_fields(result: Any) -> SearchResponse:
    """Ensure deterministic response fields are present for hybrid endpoints."""
    response = (
        result
        if isinstance(result, SearchResponse)
        else SearchResponse.model_validate(result)
    )

    if response.answer_type is None:
        response.answer_type = "extractive"
    if response.confidence is None:
        response.confidence = 0.0
    trace_id = None
    if response.trace is not None:
        trace_id = response.trace.decision_trace_id
    elif response.decision_trace_id is not None:
        trace_id = response.decision_trace_id
    else:
        trace_id = f"trace-{response.search_id}"

    response.decision_trace_id = trace_id
    if response.trace is None:
        response.trace = DeterministicTrace(decision_trace_id=trace_id)

    if response.coverage is None or (
        response.coverage == 0.0 and len(response.results) > 0
    ):
        response.coverage = _calculate_source_coverage(response)
    response.deterministic_status, response.deterministic_message = (
        _apply_deterministic_gate(response)
    )
    if response.deterministic_status in {
        "INSUFFICIENT_EVIDENCE",
        "CONFLICTING_EVIDENCE",
        "NO_MATCH",
    }:
        response.suggestions = _build_refinement_suggestions(response.query)

    return response


def _calculate_source_coverage(response: SearchResponse) -> float:
    """Mirrors HybridSearchService._calculate_deterministic_coverage: coverage
    is the fraction of results backed by a content-bearing source
    (fulltext/vector), not a requirement that every result also have a
    knowledge-graph co-hit. The two must stay in lockstep — this is only a
    fallback path (see caller) for when the service's own coverage value is
    missing.

    Results whose metadata predates R2-H10 (no "original_sources" key —
    hand-built responses, fixtures, or any producer this endpoint doesn't
    control the shape of) fall back to the old ">=2 sources" semantics via
    "source_count" instead of reading as zero coverage.
    """
    if not response.results:
        return 0.0

    evidenced = 0
    for result in response.results:
        metadata = result.metadata or {}
        if "original_sources" in metadata:
            if any(s in ("fulltext", "vector") for s in metadata["original_sources"]):
                evidenced += 1
        elif metadata.get("source_count", 1) >= 2:
            evidenced += 1

    return round(evidenced / len(response.results), 4)


def _has_conflicting_signals(response: SearchResponse) -> bool:
    positive_tokens = {
        "effective",
        "improves",
        "supported",
        "works",
        "confirmed",
        "benefit",
    }
    negative_tokens = {
        "ineffective",
        "does not",
        "not effective",
        "rejected",
        "fails",
        "harmful",
    }

    top_previews = [
        (result.content_preview or "").lower() for result in response.results[:5]
    ]
    has_positive = any(
        token in preview for preview in top_previews for token in positive_tokens
    )
    has_negative = any(
        token in preview for preview in top_previews for token in negative_tokens
    )
    return has_positive and has_negative


def _apply_deterministic_gate(response: SearchResponse) -> tuple[str, str]:
    if not response.results:
        return "NO_MATCH", "No matching evidence found for this query."

    if (response.confidence or 0.0) < 0.2:
        return (
            "NO_MATCH",
            "Top retrieved evidence is below confidence threshold for deterministic answering.",
        )

    if (response.coverage or 0.0) < 0.5:
        return (
            "INSUFFICIENT_EVIDENCE",
            "Insufficient cross-source evidence to produce a deterministic answer.",
        )

    if _has_conflicting_signals(response):
        return (
            "CONFLICTING_EVIDENCE",
            "Top evidence contains conflicting conclusions.",
        )

    return "SUPPORTED", "Evidence coverage is sufficient and consistent."


def _build_refinement_suggestions(query: str) -> List[str]:
    query = query.strip()
    return [
        f"Narrow the scope of '{query}' to a specific topic or document set.",
        "Add concrete terms (framework, date range, dataset, or method).",
        "Ask for a comparison between two specific concepts to improve precision.",
    ]


@router.post("/", response_model=SearchResponse)
async def search_documents(
    search_request: SearchQuery,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db_sync),
):
    """
    Perform search on documents with multiple search modalities
    """
    organization_id = _require_org_id(current_user)
    try:
        # Route search to appropriate service based on search type.
        # R6-H1: every pipeline below is fully synchronous (PG ts_rank_cd +
        # rerank HTTP); running it inline blocked the event loop for the
        # whole worker whenever one query was slow.
        if search_request.search_type == SearchType.HYBRID:
            # Use hybrid search service
            result = await run_in_threadpool(
                hybrid_search_service.search,
                search_request=search_request,
                user_id=str(current_user.id),
                organization_id=organization_id,
                db=db,
            )
        elif search_request.search_type == SearchType.FULLTEXT:
            # Use full-text search service
            result = await run_in_threadpool(
                fulltext_search_service.search,
                search_request=search_request,
                user_id=str(current_user.id),
                organization_id=organization_id,
                db=db,
            )
        elif search_request.search_type == SearchType.VECTOR:
            # Vector-only search was Qdrant-backed and has been removed with the
            # Qdrant→DO KB migration (no working backend). Semantic retrieval now
            # lives in the agent's DO KB path; use HYBRID for documents here.
            # See docs/database/ADR-qdrant-dense-leg.md.
            raise HTTPException(
                status_code=400,
                detail=(
                    "Vector-only search is no longer supported. Use "
                    "search_type='hybrid' (full-text + knowledge graph)."
                ),
            )
        elif search_request.search_type in (
            SearchType.KNOWLEDGE_GRAPH,
            SearchType.GRAPH,
        ):
            # Use knowledge graph search service
            from src.services.knowledge_graph.knowledge_graph_service import (
                knowledge_graph_service,
            )

            result = await run_in_threadpool(
                knowledge_graph_service.search,
                search_request=search_request,
                user_id=str(current_user.id),
                organization_id=organization_id,
                db=db,
            )
        else:
            # Default to hybrid search
            search_request.search_type = SearchType.HYBRID
            result = await run_in_threadpool(
                hybrid_search_service.search,
                search_request=search_request,
                user_id=str(current_user.id),
                organization_id=organization_id,
                db=db,
            )

        # Log search query in background (for analytics)
        background_tasks.add_task(
            log_search_query,
            str(result.search_id),
            str(current_user.id),
            organization_id,
            search_request.query,
            len(result.results),
            result.search_time_ms,
            search_request.search_type.value,
        )

        return result

    except HTTPException:
        # R2-M18: the VECTOR 400 above is an intentional client error — the
        # blanket except used to re-wrap it as a 500.
        raise
    except HybridSearchUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily overloaded; please retry",
            headers={"Retry-After": "5"},
        )
    except Exception as e:
        logger.error(f"Error performing search: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/hybrid", response_model=SearchResponse)
async def hybrid_search(
    search_request: SearchQuery,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db_sync),
):
    """
    Perform hybrid search combining vector, full-text, and knowledge graph search
    """
    organization_id = _require_org_id(current_user)
    try:
        # Force hybrid search type
        search_request.search_type = SearchType.HYBRID

        # Perform hybrid search (R6-H1: off the event loop)
        result = await run_in_threadpool(
            hybrid_search_service.search,
            search_request=search_request,
            user_id=str(current_user.id),
            organization_id=organization_id,
        )
        result = _ensure_deterministic_response_fields(result)

        # Synthesize LLM answer from retrieved results when requested
        if search_request.synthesize_answer and result.results:
            contexts = []
            for r in result.results[:5]:
                metadata = getattr(r, "metadata", {}) or {}
                content = (
                    metadata.get("full_text")
                    or metadata.get("text")
                    or getattr(r, "content_preview", "")
                    or ""
                )
                contexts.append(
                    RetrievedContext(
                        document_id=str(r.document_id) if r.document_id else None,
                        title=r.title or "Untitled",
                        content=content[:3000],
                        score=float(r.relevance_score or 0),
                        source=metadata.get("source_type", "unknown"),
                    )
                )

            context_prompt = build_context_prompt(contexts)
            messages = [
                {
                    "role": "system",
                    "content": f"{RAG_SYSTEM_PROMPT}\n\n{context_prompt}",
                },
                {"role": "user", "content": search_request.query},
            ]

            try:
                llm_response = await azure_openai_service.chat_completion(
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1024,
                    stream=False,
                )
                result.synthesized_answer = llm_response.get("content", "")
            except Exception as e:
                logger.warning(f"Answer synthesis failed: {e}")

        # Log search query in background
        background_tasks.add_task(
            log_search_query,
            str(result.search_id),
            str(current_user.id),
            organization_id,
            search_request.query,
            len(result.results),
            result.search_time_ms,
            "hybrid",
        )

        return result

    except HybridSearchUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Search is temporarily overloaded; please retry",
            headers={"Retry-After": "5"},
        )
    except Exception as e:
        logger.error(f"Error performing hybrid search: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/suggestions")
async def get_search_suggestions(
    q: str = Query(
        ..., min_length=2, max_length=100, description="Query for suggestions"
    ),
    limit: int = Query(default=5, ge=1, le=20, description="Number of suggestions"),
    current_user: User = Depends(get_current_user),
    # Suggestions are built from a sync `db.execute(text(...))` in the service.
    # Inject the sync session (not the async `get_db`) so that call actually
    # runs — with the async session it returns a coroutine and silently fails.
    db=Depends(get_db_sync),
):
    """
    Get search suggestions for auto-completion
    """
    # 403 for an org-less caller (before any try), never a "None" tenant.
    org_id = _require_org_id(current_user)
    try:
        # `_get_search_suggestions` returns [] unless it is scoped to the
        # caller's org (title suggestions would otherwise leak across tenants).
        # This call site previously dropped organization_id entirely, so the
        # endpoint always returned []. Pass the caller's org (None-safe: org-less
        # users still get [] via the service's fail-closed guard).
        suggestions = fulltext_search_service._get_search_suggestions(
            q,
            db,
            organization_id=org_id,
        )

        # Convert to SearchSuggestion models
        search_suggestions = [
            SearchSuggestion(
                text=suggestion,
                type="completion",
                score=0.8,
                metadata={"source": "document_titles"},
            )
            for suggestion in suggestions[:limit]
        ]

        return {"suggestions": search_suggestions}

    except Exception as e:
        logger.error(f"Error getting search suggestions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def get_search_history(
    limit: int = Query(default=50, ge=1, le=100, description="Number of history items"),
    current_user: User = Depends(get_current_user),
):
    """
    Get search history for the current user (placeholder)
    """
    # Return empty list for now until search history table is implemented
    return []


@router.post("/history")
async def add_search_history(
    query: str,
    result_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Add item to search history (placeholder)
    """
    return {"status": "success", "message": "Search history saved"}


@router.delete("/history")
async def clear_search_history(current_user: User = Depends(get_current_user)):
    """
    Clear search history (placeholder)
    """
    return {"status": "success", "message": "Search history cleared"}


@router.get("/analytics", response_model=SearchAnalytics)
async def get_search_analytics(
    days: int = Query(
        default=30, ge=1, le=365, description="Number of days for analytics"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Get search analytics data
    """
    # 403 for an org-less caller (before any try), never a "None" tenant.
    org_id = _require_org_id(current_user)
    try:
        analytics_data = await run_in_threadpool(
            _load_search_analytics,
            org_id,
            days,
        )
        return SearchAnalytics(**analytics_data)

    except Exception as e:
        logger.error(f"Error getting search analytics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/indexes/rebuild")
async def rebuild_search_indexes(
    current_user: User = Depends(get_current_user), db=Depends(get_db_sync)
):
    """
    Rebuild full-text search vectors for the caller's organization (admin only)

    Index DDL is deployment-owned (Alembic ``p3q4r5s6t7u8`` creates the GIN
    index on ``documents.search_vector``); a tenant admin's request must not
    run ``CREATE INDEX`` against the shared ``documents`` table. This endpoint
    only recomputes ``search_vector`` for the caller's organization.
    """
    # Check if user has admin privileges
    if current_user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    # Admin is a role boundary, not a tenant boundary: refuse an org-less
    # caller rather than rebuilding against ``organization_id = NULL``.
    _require_org_id(current_user)

    try:
        # Update only the caller's organization.
        documents = fulltext_search_service.get_rebuild_documents(
            current_user.organization_id, db
        )

        updated_count = 0
        for document in documents:
            try:
                fulltext_search_service.update_document_search_vector(
                    str(document.id), db
                )
                updated_count += 1
            except Exception as e:
                logger.error(
                    f"Error updating search vector for document {document.id}: {e}"
                )

        return {
            "message": (
                "Search vectors rebuilt for this organization; search indexes "
                "are managed by deployment migrations and were not created"
            ),
            "indexes_created": False,
            "documents_updated": updated_count,
            "total_documents": len(documents),
        }

    except Exception as e:
        logger.error(f"Error rebuilding search indexes: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/indexes", response_model=List[SearchIndex])
def get_search_indexes(
    current_user: User = Depends(get_current_user), db=Depends(get_db_sync)
):
    """
    Get information about search indexes
    """
    # 403 for an org-less caller (before any try), never a "None" tenant.
    org_id = _require_org_id(current_user)
    try:
        # Get index information from PostgreSQL.
        # R6-M2: the previous string held a Python-style '#' comment (a PG
        # syntax error) and was executed without text() — this endpoint had
        # never once returned 200.
        # R6-F6: the count is scoped to the caller's organization — an
        # unscoped count(*) disclosed the platform-wide document population
        # to every authenticated tenant user.
        index_query = text("""
        SELECT
            schemaname || '.' || indexname as name,
            amname as type,
            (SELECT count(*) FROM documents d
             WHERE NOT d.is_deleted
               AND d.organization_id = :org) as document_count,
            round(pg_relation_size(i.indexrelid) / 1024.0 / 1024.0, 2) as size_mb,
            NOW() as last_updated,
            i.indisvalid as is_active,
            '{}'::jsonb as configuration
        FROM pg_indexes ix
        JOIN pg_class c ON c.relname = ix.indexname
        JOIN pg_am am ON am.oid = c.relam
        JOIN pg_index i ON i.indexrelid = c.oid
        WHERE ix.tablename = 'documents'
            AND ix.indexname LIKE '%search%'
        """)

        result = db.execute(index_query, {"org": org_id})
        indexes = []

        for row in result:
            indexes.append(
                SearchIndex(
                    name=row.name,
                    type=row.type,
                    document_count=row.document_count,
                    size_mb=row.size_mb,
                    last_updated=row.last_updated,
                    is_active=row.is_active,
                    configuration=row.configuration,
                )
            )

        return indexes

    except Exception as e:
        logger.error(f"Error getting search indexes: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/documents/{document_id}/reindex")
async def reindex_document(
    document_id: str,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db_sync),
):
    """
    Rebuild search vector for a specific document
    """
    # 403 for an org-less caller (before any try), never a "None" tenant.
    org_id = _require_org_id(current_user)
    try:
        # Verify user has access to the document
        from src.models.document import Document

        document = (
            db.query(Document)
            .filter(
                Document.id == document_id,
                Document.organization_id == org_id,
                Document.is_deleted == False,
            )
            .first()
        )

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Update search vector, then restore the searchable flag + persist.
        # A document whose ingestion-time search-vector build failed is left
        # is_indexed=False (so it isn't falsely advertised as searchable); a
        # successful reindex makes it searchable again, so the flag must flip
        # back, else can_be_searched() stays False and this recovery path is a
        # no-op for the very documents it exists to fix.
        fulltext_search_service.update_document_search_vector(document_id, db)
        document.is_indexed = True
        db.commit()

        return {
            "message": f"Document {document_id} reindexed successfully",
            "document_id": document_id,
            "title": document.title,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reindexing document {document_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/popular")
async def get_popular_searches(
    limit: int = Query(
        default=10, ge=1, le=50, description="Number of popular searches"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Get popular search queries (placeholder for future analytics implementation)
    """
    try:
        # This is a placeholder - would need search query logging table
        # For now, return empty results
        return {"popular_queries": [], "trending_terms": [], "recent_searches": []}

    except Exception as e:
        logger.error(f"Error getting popular searches: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/similar")
async def get_similar_queries(
    q: str = Query(..., min_length=2, description="Query to find similar searches for"),
    limit: int = Query(default=5, ge=1, le=20, description="Number of similar queries"),
    current_user: User = Depends(get_current_user),
):
    """
    Get similar search queries (placeholder)
    """
    return []


@router.get("/related/{result_id}")
async def get_related_searches(
    result_id: str,
    limit: int = Query(
        default=5, ge=1, le=20, description="Number of related searches"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Get related searches for a result (placeholder)
    """
    return []


@router.post("/feedback")
async def submit_search_feedback(
    search_query: str,
    document_id: str,
    rating: int = Query(..., ge=1, le=5, description="Rating 1-5"),
    feedback_text: Optional[str] = Query(None, description="Optional feedback text"),
    current_user: User = Depends(get_current_user),
):
    """
    Submit feedback for search results (placeholder for future implementation)
    """
    try:
        # This is a placeholder - would need feedback logging table
        # For now, just acknowledge receipt
        return {
            "message": "Feedback submitted successfully",
            "query": search_query,
            "document_id": document_id,
            "rating": rating,
            "feedback_text": feedback_text,
            "user_id": str(current_user.id),
        }

    except Exception as e:
        logger.error(f"Error submitting search feedback: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health")
async def search_health_check(
    current_user: User = Depends(get_current_user), db=Depends(get_db_sync)
):
    """
    Health check for all search functionality including hybrid search
    """
    organization_id = _require_org_id(current_user)
    try:
        health_status = {
            "status": "healthy",
            "services": {},
            "overall_search_time_ms": 0,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Test full-text search
        try:
            ft_query = SearchQuery(
                query="test", search_type=SearchType.FULLTEXT, limit=1
            )
            start_time = time.time()
            ft_result = fulltext_search_service.search(
                search_request=ft_query,
                user_id=str(current_user.id),
                organization_id=organization_id,
            )
            ft_time_ms = (time.time() - start_time) * 1000
            health_status["services"]["fulltext"] = {
                "status": "healthy",
                "search_time_ms": ft_time_ms,
                "results_count": len(ft_result.results),
            }
        except Exception as e:
            health_status["services"]["fulltext"] = {
                "status": "unhealthy",
                "error": "full-text search unavailable",
            }
            logger.error(f"Full-text search health check failed: {e}")

        # Test hybrid search
        try:
            hybrid_query = SearchQuery(
                query="test", search_type=SearchType.HYBRID, limit=1
            )
            start_time = time.time()
            hybrid_result = await run_in_threadpool(
                hybrid_search_service.search,
                search_request=hybrid_query,
                user_id=str(current_user.id),
                organization_id=organization_id,
            )
            hybrid_time_ms = (time.time() - start_time) * 1000
            health_status["services"]["hybrid"] = {
                "status": "healthy",
                "search_time_ms": hybrid_time_ms,
                "results_count": len(hybrid_result.results),
            }
            health_status["overall_search_time_ms"] = hybrid_time_ms
        except Exception as e:
            health_status["services"]["hybrid"] = {
                "status": "unhealthy",
                "detail": type(e).__name__,
            }
            logger.error(f"Hybrid search health check failed: {e}")

        # Check if full-text indexes exist
        try:
            index_check_query = """
            SELECT COUNT(*) as index_count
            FROM pg_indexes
            WHERE tablename = 'documents'
                AND indexname LIKE '%search%'
            """
            index_result = db.execute(text(index_check_query))
            index_count = index_result.scalar()
            health_status["indexes"] = {
                "search_indexes_count": index_count,
                "indexes_available": index_count > 0,
            }
        except Exception as e:
            health_status["indexes"] = {
                "status": "unhealthy",
                "error": "search index status unavailable",
            }
            logger.error(f"Search index health check failed: {e}")

        # Check external service availability (knowledge graph)
        # Qdrant probe removed — Qdrant is gone (Qdrant→DO KB migration). It
        # used to call the dead dense leg and always report "healthy", masking
        # the failure. See docs/database/ADR-qdrant-dense-leg.md.
        health_status["external_services"] = {}

        # Test Neo4j (knowledge graph)
        try:
            from src.services.knowledge_graph.knowledge_graph_service import (
                knowledge_graph_service,
            )

            kg_query = SearchQuery(
                query="test", search_type=SearchType.KNOWLEDGE_GRAPH, limit=1
            )
            start_time = time.time()
            kg_result = await run_in_threadpool(
                knowledge_graph_service.search,
                search_request=kg_query,
                user_id=str(current_user.id),
                organization_id=organization_id,
            )
            kg_time_ms = (time.time() - start_time) * 1000
            health_status["external_services"]["neo4j"] = {
                "status": "healthy",
                "search_time_ms": kg_time_ms,
            }
        except Exception as e:
            health_status["external_services"]["neo4j"] = {
                "status": "unhealthy",
                "error": "knowledge graph unavailable",
            }
            logger.error(f"Knowledge graph health check failed: {e}")

        # Determine overall status
        all_healthy = all(
            service.get("status") == "healthy"
            for service in health_status["services"].values()
        )

        health_status["status"] = "healthy" if all_healthy else "degraded"

        return health_status

    except Exception as e:
        logger.error(f"Search health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                # R6-L4: internals stay in logs, not client bodies.
                "error": "search subsystem unavailable",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )


@router.post("/authenticated/hybrid", response_model=SearchResponse)
async def authenticated_hybrid_search(
    search_request: SearchQuery,
    background_tasks: BackgroundTasks,
    request: Request,
    api_key_data: tuple = Depends(get_api_key_data),
    db=Depends(get_db_sync),
):
    """
    API Key authenticated hybrid search endpoint.
    Requires valid API key for secure external access.

    This endpoint replaces the previous unauthenticated '/public/hybrid' endpoint
    to prevent unauthorized access to search functionality.
    """
    api_key, endpoint = api_key_data

    try:
        # Force hybrid search type
        search_request.search_type = SearchType.HYBRID

        # Enforce organization scoping — reject API keys without an organization
        if not api_key.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is not scoped to an organization",
            )

        # Perform hybrid search with API key context (R6-H1: off the loop)
        result = await run_in_threadpool(
            hybrid_search_service.search,
            search_request=search_request,
            user_id=f"api_key:{api_key.id}",
            organization_id=api_key.organization_id,
        )

        # Enhanced logging for API key usage
        await log_authenticated_search_query(
            api_key_id=api_key.id,
            api_key_name=api_key.name,
            query=search_request.query,
            result_count=len(result.results),
            search_time_ms=result.search_time_ms,
            search_type="authenticated_hybrid",
            client_ip=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent", "unknown"),
            background_tasks=background_tasks,
            endpoint=request.url.path,
            method=request.method,
        )

        # Log API access for security audit
        from src.core.api_key_auth import log_api_access

        log_api_access(
            api_key_data,
            request,
            "search",
            {
                "query_length": len(search_request.query),
                "search_type": search_request.search_type.value,
                "results_count": len(result.results),
                "search_time_ms": result.search_time_ms,
            },
        )

        return _ensure_deterministic_response_fields(result)

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error performing authenticated hybrid search: {e}")

        # Log failed search attempt
        from src.core.api_key_auth import log_api_access

        log_api_access(
            api_key_data,
            request,
            "search_failed",
            {"error": str(e), "query_length": len(search_request.query)},
        )

        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/authenticated/health")
async def authenticated_search_health_check(
    request: Request, api_key_data: tuple = Depends(get_api_key_data)
):
    """
    API Key authenticated health check for search services.
    Requires valid API key to prevent information disclosure.
    """
    api_key, endpoint = api_key_data

    try:
        health_status = {
            "status": "healthy",
            "services": {},
            "timestamp": datetime.utcnow().isoformat(),
            "api_key": {
                "name": api_key.name,
                "prefix": api_key.key_prefix,
                "usage_count": api_key.usage_count,
            },
        }

        # Test basic API functionality
        health_status["services"]["api"] = {
            "status": "healthy",
            "message": "Search API is accessible with valid authentication",
        }

        # Test hybrid search service availability (without performing actual search)
        try:
            health_status["services"]["hybrid_search"] = {
                "status": "healthy",
                "message": "Hybrid search service is available",
            }
        except Exception as e:
            health_status["services"]["hybrid_search"] = {
                "status": "unhealthy",
                "error": "hybrid search unavailable",
            }

        # Log API access
        from src.core.api_key_auth import log_api_access

        log_api_access(
            api_key_data,
            request,
            "health_check",
            {"services_checked": len(health_status["services"])},
        )

        return health_status

    except Exception as e:
        logger.error(f"Authenticated search health check failed: {e}")

        # Log failed health check
        from src.core.api_key_auth import log_api_access

        log_api_access(api_key_data, request, "health_check_failed", {"error": str(e)})

        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": "search subsystem unavailable",
                "timestamp": datetime.utcnow().isoformat(),
            },
        )


# Background task functions
def _load_search_analytics(organization_id: str, days: int) -> Dict[str, Any]:
    """Aggregate persisted search events for one organization."""
    from src.core.database import SessionLocal

    db = SessionLocal()
    try:
        filters = (
            SearchAnalyticsEvent.organization_id == organization_id,
            SearchAnalyticsEvent.created_at >= datetime.utcnow() - timedelta(days=days),
            SearchAnalyticsEvent.is_deleted == False,  # noqa: E712
        )
        total, average_time, average_results = (
            db.query(
                func.count(SearchAnalyticsEvent.id),
                func.coalesce(func.avg(SearchAnalyticsEvent.search_time_ms), 0.0),
                func.coalesce(func.avg(SearchAnalyticsEvent.result_count), 0.0),
            )
            .filter(*filters)
            .one()
        )
        common_queries = (
            db.query(SearchAnalyticsEvent.query, func.count(SearchAnalyticsEvent.id))
            .filter(*filters)
            .group_by(SearchAnalyticsEvent.query)
            .order_by(func.count(SearchAnalyticsEvent.id).desc())
            .limit(10)
            .all()
        )
        search_types = (
            db.query(
                SearchAnalyticsEvent.search_type,
                func.count(SearchAnalyticsEvent.id),
            )
            .filter(*filters)
            .group_by(SearchAnalyticsEvent.search_type)
            .all()
        )
        zero_results = (
            db.query(SearchAnalyticsEvent.query)
            .filter(*filters, SearchAnalyticsEvent.result_count == 0)
            .distinct()
            .limit(10)
            .all()
        )
        return {
            "total_searches": int(total),
            "average_search_time_ms": float(average_time),
            "most_common_queries": [
                {"query": query, "count": count} for query, count in common_queries
            ],
            "search_types_distribution": {
                search_type or "unknown": count for search_type, count in search_types
            },
            "zero_result_queries": [query for (query,) in zero_results],
            "average_results_per_search": float(average_results),
        }
    finally:
        db.close()


def log_search_query(
    search_id: str,
    user_id: str,
    organization_id: str,
    query: str,
    result_count: int,
    search_time_ms: float,
    search_type: str = "unknown",
):
    """Persist one search event for analytics."""
    from src.core.database import SessionLocal

    db = SessionLocal()
    try:
        db.add(
            SearchAnalyticsEvent(
                search_id=search_id,
                user_id=user_id,
                organization_id=organization_id,
                query=query,
                result_count=result_count,
                search_time_ms=search_time_ms,
                search_type=search_type,
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error logging search query: {e}")
    finally:
        db.close()


async def persist_api_key_usage_log(
    api_key_id: str,
    endpoint: str,
    method: str,
    client_ip: str,
    user_agent: str,
    response_time_ms: float,
    search_query: str,
    results_count: int,
    response_status: int = 200,
):
    """
    Persist API key usage to database for audit trail (background task)
    """
    try:
        from src.core.database import SessionLocal

        db = SessionLocal()
        try:
            usage_log = APIKeyUsageLog(
                api_key_id=api_key_id,
                endpoint=endpoint,
                method=method,
                client_ip=client_ip,
                user_agent=user_agent[:500] if user_agent else None,  # Limit length
                response_time_ms=int(response_time_ms),
                search_query=(
                    search_query[:1000] if search_query else None
                ),  # Limit length
                results_count=results_count,
                response_status=response_status,
            )

            db.add(usage_log)
            db.commit()

            logger.debug(
                f"API usage logged to database: key_id={api_key_id}, endpoint={endpoint}"
            )

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to persist API usage log to database: {e}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Error in persist_api_key_usage_log background task: {e}")


async def log_authenticated_search_query(
    api_key_id: str,
    api_key_name: str,
    query: str,
    result_count: int,
    search_time_ms: float,
    search_type: str,
    client_ip: str,
    user_agent: str,
    background_tasks: BackgroundTasks,
    endpoint: str = "/search/public",
    method: str = "POST",
):
    """
    Log authenticated search query with enhanced security context
    """
    try:
        # Enhanced logging for API key searches with security context
        logger.info(
            f"API Key Search: key_id={api_key_id}, key_name='{api_key_name}', "
            f"query_hash='{hash(query) % 10000}', query_length={len(query)}, "
            f"results={result_count}, time={search_time_ms:.2f}ms, "
            f"type={search_type}, ip={client_ip}, ua='{user_agent[:100]}'"
        )

        # Store in api_key_usage_log table for audit trail (async background task)
        background_tasks.add_task(
            persist_api_key_usage_log,
            api_key_id=api_key_id,
            endpoint=endpoint,
            method=method,
            client_ip=client_ip,
            user_agent=user_agent,
            response_time_ms=search_time_ms,
            search_query=query,
            results_count=result_count,
            response_status=200,
        )

    except Exception as e:
        logger.error(f"Error logging authenticated search query: {e}")
