"""
Hybrid Search Service that combines full-text and knowledge-graph search results.

The Qdrant vector arm was retired (no vector backend is deployed); ``HYBRID``
now fuses full-text and knowledge-graph sources, and ``SEMANTIC`` maps to
full-text.
"""

import inspect
import logging
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.search_schemas import (
    SearchFilter,
    SearchQuery,
    SearchResponse,
    SearchResult,
    SearchType,
)

from .cohere_rerank_service import cohere_rerank_service
from .fulltext_search_service import fulltext_search_service

if TYPE_CHECKING:
    # Only for the return-type annotation below — the runtime import stays
    # deferred inside search_with_diagnostics() so the diagnostics module is
    # only loaded when that diagnostic path actually runs.
    from src.services.diagnostics.retrieval_diagnostics import RetrievalTrace

logger = logging.getLogger(__name__)


class SearchSourceType(Enum):
    """Source types for search results"""

    FULLTEXT = "fulltext"
    VECTOR = "vector"
    KNOWLEDGE_GRAPH = "knowledge_graph"


@dataclass
class RawSearchResult:
    """Raw search result from a specific source"""

    document_id: str
    source_type: SearchSourceType
    relevance_score: float
    metadata: Dict[str, Any]
    search_result: Optional[SearchResult] = None


@dataclass
class SearchSourceResult:
    """Results from a specific search source"""

    source_type: SearchSourceType
    results: List[RawSearchResult]
    search_time_ms: float
    total_available: int
    success: bool
    error: Optional[str] = None


class HybridSearchService:
    """Service for combining multiple search modalities"""

    def __init__(self, db: Session = None):
        self.db = db
        self.fulltext_weight = 0.4
        self.vector_weight = 0.4
        self.knowledge_graph_weight = 0.2

        # Fusion parameters
        self.min_results_per_source = 3
        self.max_results_per_source = 20
        self.diversity_boost = 0.1
        self.recency_boost_hours = 24

        # Thread pool for parallel search execution
        self.executor = ThreadPoolExecutor(max_workers=3)

    async def search_documents(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Legacy async wrapper kept for older unit tests."""
        vector_results = self._vector_search(
            query=query,
            user_id=user_id,
            organization_id=organization_id,
            limit=limit,
        )
        if inspect.isawaitable(vector_results):
            vector_results = await vector_results

        fulltext_results = self._fulltext_search(
            query=query,
            user_id=user_id,
            organization_id=organization_id,
            limit=limit,
        )
        if inspect.isawaitable(fulltext_results):
            fulltext_results = await fulltext_results

        fused_results = self._fusion_ranking(vector_results, fulltext_results)
        if inspect.isawaitable(fused_results):
            fused_results = await fused_results

        if filters:
            fused_results = self._apply_filters(fused_results, filters)
            if inspect.isawaitable(fused_results):
                fused_results = await fused_results

        return list(fused_results)[:limit]

    def _vector_search(self, **_kwargs) -> List[Dict[str, Any]]:
        """Compatibility hook used by legacy tests."""
        return []

    def _fulltext_search(self, **_kwargs) -> List[Dict[str, Any]]:
        """Compatibility hook used by legacy tests."""
        return []

    def _fusion_ranking(
        self,
        vector_results: List[Dict[str, Any]],
        fulltext_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Compatibility hook used by legacy tests."""
        return [*vector_results, *fulltext_results]

    def _apply_filters(
        self,
        results: List[Dict[str, Any]],
        _filters: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Compatibility hook used by legacy tests."""
        return results

    def search(
        self,
        search_request: SearchQuery,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> SearchResponse:
        """
        Perform hybrid search combining multiple search modalities

        Args:
            search_request: Search query and parameters
            user_id: ID of user performing search
            organization_id: ID of organization for filtering
            db: Database session (optional, will create one if not provided)

        Returns:
            SearchResponse with fused results from all sources
        """
        start_time = time.time()

        try:
            # Route search query to appropriate sources
            search_sources = self._route_search_query(search_request)

            # Execute searches in parallel
            source_results = self._execute_parallel_searches(
                search_request, search_sources, user_id, organization_id, db
            )

            # Fuse and rank results
            fused_results = self._fuse_search_results(source_results, search_request)

            # Apply Cohere reranking if enabled (improves Precision@3/5)
            if cohere_rerank_service.is_enabled and fused_results:
                logger.info(
                    f"Applying Cohere reranking to {len(fused_results)} results"
                )
                reranked = self._apply_cohere_reranking(
                    search_request.query,
                    fused_results,
                    # R2-M15: rerank the full pool (bounded), not limit*2 —
                    # truncating pre-pagination made offset>=20 return empty
                    # pages while total claimed otherwise.
                    min(len(fused_results), 200),
                )
                fused_results = reranked

            # Apply final filtering and pagination
            final_results, total_filtered = self._apply_final_filtering(
                fused_results, search_request, organization_id, db=db
            )

            # Create response
            search_time_ms = (time.time() - start_time) * 1000
            coverage = self._calculate_deterministic_coverage(final_results)
            confidence = self._calculate_deterministic_confidence(final_results)

            return SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=SearchType.HYBRID,
                results=final_results,
                total_results=total_filtered,
                returned_results=len(final_results),
                search_time_ms=search_time_ms,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=total_filtered
                > (search_request.offset + search_request.limit),
                suggestions=self._get_hybrid_suggestions(
                    search_request, source_results
                ),
                answer_type="extractive",
                confidence=confidence,
                coverage=coverage,
                deterministic_status="SUPPORTED" if final_results else "NO_MATCH",
            )

        except Exception as e:
            logger.error(f"Error performing hybrid search: {e}")
            # Fallback to full-text search if hybrid fails
            return self._fallback_to_fulltext(search_request, user_id, organization_id)

    def search_with_diagnostics(
        self,
        search_request: SearchQuery,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        db: Optional[Session] = None,
        weights_override: Optional[Dict[str, float]] = None,
    ) -> Tuple[SearchResponse, "RetrievalTrace"]:
        """
        Perform hybrid search with full diagnostic instrumentation.

        Same pipeline as search() but captures per-source timing, fusion stats,
        reranking deltas, and returns a RetrievalTrace alongside the response.

        Args:
            search_request: Search query and parameters
            user_id: ID of user performing search
            organization_id: ID of organization for filtering
            db: Database session
            weights_override: Optional dict of {fulltext, vector, knowledge_graph} weights
                              for experimentation. Must sum to 1.0.

        Returns:
            Tuple of (SearchResponse, RetrievalTrace)
        """
        from src.services.diagnostics.retrieval_diagnostics import (
            FusionDiagnostics,
            RerankDiagnostics,
            RetrievalTrace,
            SourceDiagnostics,
        )

        start_time = time.time()
        trace = RetrievalTrace(
            query=search_request.query,
            search_type=search_request.search_type or "hybrid",
        )

        effective_weights = {
            "fulltext": self.fulltext_weight,
            "vector": self.vector_weight,
            "knowledge_graph": self.knowledge_graph_weight,
        }
        if weights_override:
            effective_weights = {
                "fulltext": weights_override.get("fulltext", self.fulltext_weight),
                "vector": weights_override.get("vector", self.vector_weight),
                "knowledge_graph": weights_override.get(
                    "knowledge_graph", self.knowledge_graph_weight
                ),
            }

        try:
            # Step 1: Route query
            search_sources = self._route_search_query(search_request)

            # Step 2: Execute parallel searches with per-source diagnostics
            source_results = self._execute_parallel_searches(
                search_request, search_sources, user_id, organization_id, db
            )

            for source_type, source_result in source_results.items():
                scores = [r.relevance_score for r in source_result.results]
                trace.sources.append(
                    SourceDiagnostics(
                        source_type=source_type.value,
                        search_time_ms=source_result.search_time_ms,
                        result_count=len(source_result.results),
                        total_available=source_result.total_available,
                        success=source_result.success,
                        error=source_result.error,
                        top_scores=sorted(scores, reverse=True)[:5],
                        avg_score=round(statistics.mean(scores), 4) if scores else 0.0,
                    )
                )

            # Step 3: Fuse results with diagnostics
            fusion_start = time.time()
            fused_results = self._fuse_search_results(
                source_results,
                search_request,
                source_weights=effective_weights,
            )
            fusion_time = (time.time() - fusion_start) * 1000

            # Count raw input docs and multi-source docs
            raw_input_count = sum(
                len(sr.results) for sr in source_results.values() if sr.success
            )
            # Count docs appearing in multiple sources
            doc_sources: Dict[str, int] = {}
            for sr in source_results.values():
                if sr.success:
                    for r in sr.results:
                        doc_sources[r.document_id] = (
                            doc_sources.get(r.document_id, 0) + 1
                        )
            multi_source_count = sum(1 for c in doc_sources.values() if c > 1)

            fused_scores = [r.relevance_score for r in fused_results]
            score_dist = {}
            if fused_scores:
                score_dist = {
                    "min": round(min(fused_scores), 4),
                    "max": round(max(fused_scores), 4),
                    "mean": round(statistics.mean(fused_scores), 4),
                    "median": round(statistics.median(fused_scores), 4),
                }

            trace.fusion = FusionDiagnostics(
                input_count=raw_input_count,
                output_count=len(fused_results),
                multi_source_count=multi_source_count,
                weights_used=effective_weights,
                score_distribution=score_dist,
                fusion_time_ms=round(fusion_time, 2),
            )

            # Step 4: Rerank with diagnostics
            if cohere_rerank_service.is_enabled and fused_results:
                rerank_start = time.time()
                pre_rerank_scores = {
                    r.document_id: r.relevance_score for r in fused_results
                }

                try:
                    reranked = self._apply_cohere_reranking(
                        search_request.query,
                        fused_results,
                        # R2-M15: full pool, bounded
                        min(len(fused_results), 200),
                    )
                    rerank_time = (time.time() - rerank_start) * 1000

                    score_deltas = []
                    for r in reranked:
                        before = pre_rerank_scores.get(r.document_id, 0.0)
                        after = r.relevance_score
                        score_deltas.append(
                            {
                                "doc_id": r.document_id,
                                "before": round(before, 4),
                                "after": round(after, 4),
                                "delta": round(after - before, 4),
                            }
                        )

                    trace.rerank = RerankDiagnostics(
                        enabled=True,
                        rerank_time_ms=round(rerank_time, 2),
                        input_count=len(fused_results),
                        output_count=len(reranked),
                        score_deltas=score_deltas,
                        fallback_used=False,
                    )
                    fused_results = reranked
                except Exception as e:
                    rerank_time = (time.time() - rerank_start) * 1000
                    trace.rerank = RerankDiagnostics(
                        enabled=True,
                        rerank_time_ms=round(rerank_time, 2),
                        input_count=len(fused_results),
                        output_count=len(fused_results),
                        fallback_used=True,
                        error=str(e),
                    )
            else:
                trace.rerank = RerankDiagnostics(enabled=False)

            # Step 5: Final filtering
            final_results, total_filtered = self._apply_final_filtering(
                fused_results, search_request, organization_id, db=db
            )

            search_time_ms = (time.time() - start_time) * 1000
            trace.total_time_ms = round(search_time_ms, 2)
            trace.final_result_count = len(final_results)

            response = SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=SearchType.HYBRID,
                results=final_results,
                total_results=total_filtered,
                returned_results=len(final_results),
                search_time_ms=search_time_ms,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=total_filtered
                > (search_request.offset + search_request.limit),
                suggestions=self._get_hybrid_suggestions(
                    search_request, source_results
                ),
                answer_type="extractive",
                confidence=self._calculate_deterministic_confidence(final_results),
                coverage=self._calculate_deterministic_coverage(final_results),
                deterministic_status="SUPPORTED" if final_results else "NO_MATCH",
            )

            return response, trace

        except Exception as e:
            logger.error(f"Error in search_with_diagnostics: {e}")
            trace.total_time_ms = round((time.time() - start_time) * 1000, 2)
            fallback = self._fallback_to_fulltext(
                search_request, user_id, organization_id
            )
            return fallback, trace

    def _route_search_query(
        self, search_request: SearchQuery
    ) -> List[SearchSourceType]:
        """
        Determine which search sources to use based on query characteristics

        Args:
            search_request: The search query

        Returns:
            List of search source types to use
        """
        sources = []

        # Vector search was removed (Qdrant retired); SEMANTIC now maps to
        # full-text so a valid enum value never yields an empty result set.
        if search_request.search_type in [
            SearchType.FULLTEXT,
            SearchType.SEMANTIC,
            SearchType.HYBRID,
        ]:
            sources.append(SearchSourceType.FULLTEXT)

        # Include knowledge graph search for entity-based queries
        if search_request.search_type in [SearchType.HYBRID]:
            # Check if query contains entities or relationship indicators
            import re as _re

            if _re.search(
                r"\b(who|what|where|when|how|relationship[s]?|connected)\b",
                search_request.query.lower(),
            ):
                sources.append(SearchSourceType.KNOWLEDGE_GRAPH)

        # Default to all sources for hybrid search
        if search_request.search_type == SearchType.HYBRID and not sources:
            sources = [
                SearchSourceType.FULLTEXT,
                SearchSourceType.KNOWLEDGE_GRAPH,
            ]

        return sources

    def _execute_parallel_searches(
        self,
        search_request: SearchQuery,
        sources: List[SearchSourceType],
        user_id: Optional[str],
        organization_id: Optional[str],
        db: Optional[Session] = None,
    ) -> Dict[SearchSourceType, SearchSourceResult]:
        """
        Execute searches from multiple sources in parallel

        Args:
            search_request: Search query
            sources: List of search source types
            user_id: User ID for access control
            organization_id: Organization ID for filtering

        Returns:
            Dictionary mapping source types to their results
        """
        results = {}

        # Create search tasks for each source
        search_tasks = []
        for source_type in sources:
            if source_type == SearchSourceType.FULLTEXT:
                search_tasks.append(
                    (
                        source_type,
                        self._execute_fulltext_search,
                        search_request,
                        user_id,
                        organization_id,
                        db,
                    )
                )
            elif source_type == SearchSourceType.KNOWLEDGE_GRAPH:
                search_tasks.append(
                    (
                        source_type,
                        self._execute_knowledge_graph_search,
                        search_request,
                        user_id,
                        organization_id,
                        db,
                    )
                )

        # Execute searches in parallel
        future_to_source = {
            self.executor.submit(search_func, *args): source_type
            for source_type, search_func, *args in search_tasks
        }

        # Collect results
        for future in future_to_source:
            source_type = future_to_source[future]
            try:
                results[source_type] = future.result(
                    timeout=10
                )  # 10 second timeout per source
            except Exception as e:
                logger.error(f"Error in {source_type.value} search: {e}")
                results[source_type] = SearchSourceResult(
                    source_type=source_type,
                    results=[],
                    search_time_ms=0,
                    total_available=0,
                    success=False,
                    error=str(e),
                )

        return results

    def _execute_fulltext_search(
        self,
        search_request: SearchQuery,
        user_id: str,
        organization_id: str,
        db: Session = None,
    ) -> SearchSourceResult:
        """Execute full-text search"""
        start_time = time.time()

        try:
            # Create full-text search query
            ft_search_request = SearchQuery(
                query=search_request.query,
                search_type=SearchType.FULLTEXT,
                limit=self.max_results_per_source,
                offset=0,
                filters=search_request.filters,
                include_snippets=search_request.include_snippets,
            )

            # Execute search
            ft_result = fulltext_search_service.search(
                search_request=ft_search_request,
                user_id=user_id,
                organization_id=organization_id,
                db=db,
            )

            # Convert to raw results
            raw_results = []
            for result in ft_result.results:
                raw_results.append(
                    RawSearchResult(
                        document_id=result.document_id,
                        source_type=SearchSourceType.FULLTEXT,
                        relevance_score=result.relevance_score,
                        metadata={
                            "original_score": result.relevance_score,
                            "source": "fulltext",
                            "snippets": result.snippets,
                        },
                        search_result=result,
                    )
                )

            search_time_ms = (time.time() - start_time) * 1000

            return SearchSourceResult(
                source_type=SearchSourceType.FULLTEXT,
                results=raw_results,
                search_time_ms=search_time_ms,
                total_available=ft_result.total_results,
                success=True,
            )

        except Exception as e:
            logger.error(f"Full-text search failed: {e}")
            return SearchSourceResult(
                source_type=SearchSourceType.FULLTEXT,
                results=[],
                search_time_ms=0,
                total_available=0,
                success=False,
                error=str(e),
            )

    def _execute_knowledge_graph_search(
        self,
        search_request: SearchQuery,
        user_id: str,
        organization_id: str,
        db: Session = None,
    ) -> SearchSourceResult:
        """Execute knowledge graph search"""
        start_time = time.time()

        # GOO-399: fail closed like the full-text arm (GOO-351). With no
        # organization search_entities gets no scope at all and its query
        # spans every tenant's entities.
        if not organization_id:
            logger.warning(
                "Knowledge graph search rejected: missing organization scope"
            )
            return SearchSourceResult(
                source_type=SearchSourceType.KNOWLEDGE_GRAPH,
                results=[],
                search_time_ms=0,
                total_available=0,
                success=True,
            )

        try:
            # Import and use knowledge graph service. Must import the
            # *submodule* attribute, not the package: `from
            # src.services.knowledge_graph import knowledge_graph_service`
            # binds the `knowledge_graph_service` *module* object (the
            # package's __init__.py never re-exports the singleton), which
            # has no `.search_entities` — every call raised AttributeError,
            # silently caught below, so the KG arm has been contributing
            # nothing regardless of any fusion/normalization fix.
            from src.services.knowledge_graph.knowledge_graph_service import (
                knowledge_graph_service,
            )

            # Execute search using the available interface. organization_id MUST
            # be threaded through — without it search_entities runs unscoped over
            # every tenant's entities and leaks other orgs' entity names/ids/
            # context into this user's ranked search results.
            kg_result = knowledge_graph_service.search_entities(
                query=search_request.query,
                limit=self.max_results_per_source,
                organization_id=organization_id,
            )

            # Convert to raw results. Entities live in a different UUID space
            # than Document rows (Neo4j entity id != Document.id) — fusing on
            # the entity id can never merge with the fulltext arm, and the
            # entity id 404s client-side as an "open document" target. Only
            # entities carrying a real source_document_id (the Document they
            # were extracted from) are usable as document-level evidence;
            # entities without one are skipped rather than fabricating a
            # document_id from the entity's own uuid.
            raw_results = []
            for result in kg_result:
                document_id = getattr(result, "source_document_id", None)
                if not document_id:
                    continue

                entity_id = getattr(result, "id", None)
                confidence_score = getattr(result, "confidence_score", None)
                if confidence_score is None:
                    confidence_score = 0.8
                relevance_score = max(0.0, min(confidence_score, 1.0))

                # Create SearchResult from knowledge graph result
                search_result = SearchResult(
                    document_id=str(document_id),
                    title=getattr(result, "name", "Entity"),
                    document_type=DocumentType.TEXT,
                    content_preview=getattr(result, "context", "") or "",
                    snippets=[],
                    relevance_score=relevance_score,
                    file_size_bytes=0,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                    processing_status=ProcessingStatus.COMPLETED,
                    tags=[],
                    is_public=False,
                    uploaded_by_user_id=user_id or "",
                    organization_id=organization_id,
                    metadata={
                        "entity_id": entity_id,
                        "entity_name": getattr(result, "name", None),
                    },
                )

                raw_results.append(
                    RawSearchResult(
                        document_id=search_result.document_id,
                        source_type=SearchSourceType.KNOWLEDGE_GRAPH,
                        relevance_score=search_result.relevance_score,
                        metadata={
                            "original_score": search_result.relevance_score,
                            "source": "knowledge_graph",
                            "entities": [],
                            "entity_id": entity_id,
                        },
                        search_result=search_result,
                    )
                )

            search_time_ms = (time.time() - start_time) * 1000

            return SearchSourceResult(
                source_type=SearchSourceType.KNOWLEDGE_GRAPH,
                results=raw_results,
                search_time_ms=search_time_ms,
                total_available=len(raw_results),  # KG service doesn't provide total
                success=True,
            )

        except Exception as e:
            logger.error(f"Knowledge graph search failed: {e}")
            return SearchSourceResult(
                source_type=SearchSourceType.KNOWLEDGE_GRAPH,
                results=[],
                search_time_ms=0,
                total_available=0,
                success=False,
                error=str(e),
            )

    def _fuse_search_results(
        self,
        source_results: Dict[SearchSourceType, SearchSourceResult],
        search_request: SearchQuery,
        source_weights: Optional[Dict[str, float]] = None,
    ) -> List[RawSearchResult]:
        """
        Fuse results from multiple search sources using weighted scoring

        Args:
            source_results: Results from each search source
            search_request: Original search request

        Returns:
            Fused and ranked list of raw results
        """
        # Collect all results with their document IDs
        all_results = {}

        # Process content-bearing sources before KNOWLEDGE_GRAPH (regardless
        # of source_results dict/thread-completion order — see the sort key
        # below), so a KG hit can only ever corroborate a document another
        # source already surfaced. KG's document_id comes from
        # EntityResponse.source_document_id, which
        # KnowledgeGraphService.create_entity sets only in its ON CREATE
        # branch and never updates on ON MATCH — entities are MERGEd across
        # documents by (name, type, org), so a shared entity's
        # source_document_id permanently identifies whichever document
        # created the node first, which can be stale or even deleted (see
        # test_delete_document_graph_shared_node_semantics_documented). That
        # property is safe as a corroboration boost for a document another
        # source already verified; it is not safe as the sole source of a
        # returned result — it can misboost or return the wrong/dead
        # document, or 404 as an "open document" target. So a KG-only
        # document_id (no other source already matched it) is dropped here
        # rather than turned into a standalone result.
        ordered_source_types = sorted(
            source_results.keys(),
            key=lambda st: 1 if st == SearchSourceType.KNOWLEDGE_GRAPH else 0,
        )
        for source_type in ordered_source_types:
            source_result = source_results[source_type]
            if not source_result.success:
                continue

            for result in source_result.results:
                document_id = result.document_id

                if document_id not in all_results:
                    if source_type == SearchSourceType.KNOWLEDGE_GRAPH:
                        continue
                    all_results[document_id] = {
                        "document_id": document_id,
                        "sources": {},
                        "search_result": result.search_result,
                        "boost_factors": {},
                    }
                elif result.search_result and source_type == SearchSourceType.FULLTEXT:
                    # Prefer fulltext search_result (has proper document titles from DB)
                    all_results[document_id]["search_result"] = result.search_result

                # Add source-specific score
                weight = self._get_source_weight(
                    source_type, source_weights=source_weights
                )
                normalized_score = self._normalize_score(
                    result.relevance_score, source_type
                )
                all_results[document_id]["sources"][source_type] = {
                    "score": result.relevance_score,
                    "normalized_score": normalized_score,
                    "weight": weight,
                    "metadata": result.metadata,
                }

        # Calculate fused scores for each document
        fused_results = []
        for document_id, doc_data in all_results.items():
            fused_score = 0.0
            source_count = len(doc_data["sources"])

            # Base score from weighted combination
            for source_type, source_data in doc_data["sources"].items():
                fused_score += source_data["normalized_score"] * source_data["weight"]

            # Apply diversity boost for documents from multiple sources
            if source_count > 1:
                diversity_boost = self.diversity_boost * (source_count - 1)
                fused_score += diversity_boost
                doc_data["boost_factors"]["diversity"] = diversity_boost

            # Apply recency boost
            if doc_data["search_result"]:
                recency_boost = self._calculate_recency_boost(doc_data["search_result"])
                if recency_boost > 0:
                    fused_score += recency_boost
                    doc_data["boost_factors"]["recency"] = recency_boost

            # Create fused result
            fused_result = RawSearchResult(
                document_id=document_id,
                source_type=SearchSourceType.FULLTEXT,  # Default for hybrid
                relevance_score=fused_score,
                metadata={
                    "fused_score": fused_score,
                    "sources": doc_data["sources"],
                    "source_count": source_count,
                    "boost_factors": doc_data["boost_factors"],
                    "original_sources": [s.value for s in doc_data["sources"].keys()],
                },
                search_result=doc_data["search_result"],
            )

            fused_results.append(fused_result)

        # Sort deterministically:
        # 1) fused score desc
        # 2) source quality tier asc (better source first)
        # 3) updated_at timestamp desc (newer first)
        # 4) document_id lexicographic asc
        fused_results.sort(key=self._deterministic_fusion_sort_key)

        return fused_results

    def _deterministic_fusion_sort_key(
        self, result: RawSearchResult
    ) -> Tuple[float, int, float, str]:
        """Create deterministic sort key for fused results."""
        source_quality_tier = self._get_source_quality_tier(result)
        updated_at_ts = self._extract_updated_at_timestamp(result)
        return (
            -result.relevance_score,
            source_quality_tier,
            -updated_at_ts,
            result.document_id,
        )

    def _get_source_quality_tier(self, result: RawSearchResult) -> int:
        """Return best (lowest) source quality tier for the result."""
        source_tier_map = {
            SearchSourceType.FULLTEXT.value: 0,
            SearchSourceType.VECTOR.value: 1,
            SearchSourceType.KNOWLEDGE_GRAPH.value: 2,
        }
        original_sources = result.metadata.get("original_sources", [])
        if not original_sources:
            return source_tier_map.get(result.source_type.value, 99)

        tiers = [source_tier_map.get(source, 99) for source in original_sources]
        return min(tiers) if tiers else 99

    def _extract_updated_at_timestamp(self, result: RawSearchResult) -> float:
        """Extract comparable timestamp for deterministic tie-breaks."""
        if not result.search_result:
            return 0.0

        updated_at = result.search_result.updated_at or result.search_result.created_at
        if not updated_at:
            return 0.0

        if updated_at.tzinfo is None:
            from datetime import timezone

            updated_at = updated_at.replace(tzinfo=timezone.utc)

        return updated_at.timestamp()

    def _get_source_weight(
        self,
        source_type: SearchSourceType,
        source_weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """Get weight for a specific search source"""
        if source_weights:
            return source_weights.get(source_type.value, 0.33)

        default_weights = {
            SearchSourceType.FULLTEXT: self.fulltext_weight,
            SearchSourceType.VECTOR: self.vector_weight,
            SearchSourceType.KNOWLEDGE_GRAPH: self.knowledge_graph_weight,
        }
        return default_weights.get(source_type, 0.33)

    def _normalize_score(self, score: float, source_type: SearchSourceType) -> float:
        """Normalize a raw per-source score onto a common 0-1 scale for fusion.

        Scale contract per source (see fulltext_search_service.py and
        _execute_knowledge_graph_search below for where these are produced):
          - FULLTEXT: fulltext_search_service's SQL selects
            ``ts_rank_cd(...) * 10 AS relevance_score``. Raw ts_rank_cd is
            naturally bounded ~0-1 for typical matches, so undo that app-level
            x10 scaling here before clamping — dividing by 50 (the old,
            wrong assumption) crushed real scores to ~0.001-0.02 and starved
            the fusion weight of any signal.
          - VECTOR: retired (no vector backend deployed); kept 0-1 for any
            legacy/test caller that still feeds it a score.
          - KNOWLEDGE_GRAPH: entity match confidence is already produced on a
            0-1 scale (see _execute_knowledge_graph_search) — do not
            re-divide it.
        """
        if source_type == SearchSourceType.FULLTEXT:
            return max(0.0, min(score / 10.0, 1.0))
        elif source_type == SearchSourceType.VECTOR:
            return max(0.0, min(score, 1.0))
        elif source_type == SearchSourceType.KNOWLEDGE_GRAPH:
            return max(0.0, min(score, 1.0))
        else:
            return max(0.0, min(score, 1.0))

    def _calculate_recency_boost(self, search_result: SearchResult) -> float:
        """Calculate recency boost for a search result"""
        if not search_result.created_at:
            return 0.0

        import datetime
        from datetime import timezone

        now = datetime.datetime.now(timezone.utc)

        # Ensure created_at is aware
        created_at = search_result.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_hours = (now - created_at).total_seconds() / 3600

        if age_hours < self.recency_boost_hours:
            # Linear decay from full boost to 0 over the period
            boost = (1.0 - age_hours / self.recency_boost_hours) * 0.1
            return boost

        return 0.0

    def _apply_cohere_reranking(
        self, query: str, fused_results: List[RawSearchResult], top_n: int
    ) -> List[RawSearchResult]:
        """
        Apply Cohere reranking to improve precision of results.

        Args:
            query: The search query
            fused_results: List of fused search results
            top_n: Number of results to return

        Returns:
            Reranked list of results
        """
        try:
            # Convert to format expected by reranking service
            documents = []
            for result in fused_results:
                content = ""
                if result.search_result:
                    metadata = result.search_result.metadata or {}
                    content = (
                        metadata.get("full_text")
                        or metadata.get("text")
                        or result.search_result.content_preview
                        or result.search_result.title
                        or ""
                    )

                documents.append(
                    {
                        "id": result.document_id,
                        "document_id": result.document_id,
                        "content": content,
                        "content_snippet": content[:500],
                        "relevance_score": result.relevance_score,
                    }
                )

            # Use sync version of reranking for sync context
            rerank_outcome = cohere_rerank_service.rerank_sync_with_outcome(
                query, documents, top_n
            )
            rerank_results = rerank_outcome.results

            # Build reordered results
            result_map = {r.document_id: r for r in fused_results}
            reranked = []

            for rr in rerank_results:
                if rr.document_id in result_map:
                    original = result_map[rr.document_id]
                    # Update score with Cohere's relevance score
                    original.relevance_score = rr.relevance_score
                    original.metadata["cohere_score"] = rr.relevance_score
                    original.metadata["original_score"] = rr.original_score
                    reranked.append(original)

            fallback_info = rerank_outcome.failure
            if fallback_info:
                for result in reranked:
                    result.metadata["cohere_fallback_used"] = True
                    result.metadata["cohere_fallback_reason"] = fallback_info.get(
                        "reason"
                    )
                    if "status_code" in fallback_info:
                        result.metadata["cohere_status_code"] = fallback_info.get(
                            "status_code"
                        )
            else:
                for result in reranked:
                    result.metadata["cohere_fallback_used"] = False

            logger.info(
                f"Cohere reranking complete: {len(fused_results)} -> {len(reranked)} results"
            )
            return reranked

        except Exception as e:
            logger.error(f"Cohere reranking failed, using original order: {e}")
            return fused_results[:top_n]

    def _apply_final_filtering(
        self,
        fused_results: List[RawSearchResult],
        search_request: SearchQuery,
        organization_id: Optional[str],
        db: Optional[Session] = None,
    ) -> Tuple[List[SearchResult], int]:
        """Apply final filtering and pagination to fused results.

        Returns ``(page_results, total_after_filter)`` — the second value is the
        full post-filter candidate count (pre-pagination), so callers can
        compute ``has_more`` correctly instead of guessing from the sliced page.
        """
        selected_document_ids = (
            set(search_request.filters.document_ids or [])
            if search_request.filters
            else set()
        )
        selected_tags = (
            set(search_request.filters.tags or []) if search_request.filters else set()
        )

        filtered_fused_results: List[RawSearchResult] = []
        for result in fused_results:
            if self._is_noisy_result(result):
                continue

            if (
                selected_document_ids
                and result.document_id not in selected_document_ids
            ):
                continue

            if selected_tags and result.search_result:
                result_tags = set(result.search_result.tags or [])
                if not (result_tags & selected_tags):
                    continue

            filtered_fused_results.append(result)

        # Apply offset and limit
        start_idx = search_request.offset
        end_idx = start_idx + search_request.limit

        paginated_results = filtered_fused_results[start_idx:end_idx]

        # Convert to SearchResult objects
        final_results = []
        for result in paginated_results:
            if result.search_result:
                # Update the search result with the fused score
                result.search_result.relevance_score = result.relevance_score
                result.search_result.metadata.update(result.metadata)
                final_results.append(result.search_result)

        # Enrich titles for results that still have placeholder titles
        if db and final_results:
            self._enrich_titles_from_db(
                final_results, db, organization_id=organization_id
            )

        return final_results, len(filtered_fused_results)

    def _enrich_titles_from_db(
        self,
        results: List[SearchResult],
        db: Session,
        organization_id: Optional[str] = None,
    ) -> None:
        """Look up actual document titles from DB for results with placeholder titles."""
        try:
            ids_needing_titles = [
                r.document_id
                for r in results
                if r.title in ("Untitled", "Untitled Document", "") or len(r.title) > 80
            ]
            if not ids_needing_titles:
                return

            from sqlalchemy import text

            # Tenant-scoped + soft-delete aware: this is a bare-id lookup and
            # would otherwise leak deleted documents' titles (R6-L2).
            org_filter = ""
            params: dict = {"ids": ids_needing_titles}
            if organization_id:
                org_filter = " AND organization_id = :organization_id"
                params["organization_id"] = organization_id
            rows = db.execute(
                text(
                    "SELECT id::text, title FROM documents "
                    "WHERE id = ANY(:ids) AND NOT is_deleted" + org_filter
                ),
                params,
            ).fetchall()
            title_map = {row[0]: row[1] for row in rows if row[1]}

            for result in results:
                if result.document_id in title_map:
                    result.title = title_map[result.document_id]
        except Exception as e:
            logger.warning(f"Failed to enrich titles from DB: {e}")

    def _is_noisy_result(self, result: RawSearchResult) -> bool:
        """Filter out known noisy synthetic verification strings from retrieval results."""
        content = ""
        if result.search_result:
            content = (
                f"{result.search_result.title} {result.search_result.content_preview}"
            ).lower()

        if not content:
            return False

        noisy_markers = [
            "verified 0/l000 tampered whisper field isolation parse validation",
            "all security properties verified through",
            "replay prevention nonce + timestamp",
        ]
        return any(marker in content for marker in noisy_markers)

    # Sources that carry real document content and can stand as "evidence" on
    # their own. KNOWLEDGE_GRAPH results only carry entity metadata (name,
    # extracted context) with no full document text, so a KG-only hit is
    # enrichment, not evidence — it must not count toward coverage, and it
    # must not be *required* for coverage either. Requiring a KG co-hit is
    # what made coverage 0.0 on every populated response before: the KG arm
    # only activates for entity-indicator queries (see _route_search_query),
    # so most hybrid results are fulltext-only by design, not by bug.
    _EVIDENCE_SOURCE_VALUES = {
        SearchSourceType.FULLTEXT.value,
        SearchSourceType.VECTOR.value,
    }

    def _calculate_deterministic_coverage(self, results: List[SearchResult]) -> float:
        """Calculate deterministic evidence coverage from fused metadata.

        Coverage = fraction of returned results backed by at least one
        content-bearing source (fulltext/vector). KG hits still boost ranking
        via the diversity boost in _fuse_search_results, but don't gate
        coverage — see _EVIDENCE_SOURCE_VALUES above.

        Results without an "original_sources" key (metadata from before
        R2-H10, or any caller that built a SearchResult by hand) fall back to
        the old ">=2 sources" reading via "source_count" instead of reading
        as zero coverage — keep in lockstep with
        api.search.search._calculate_source_coverage.
        """
        if not results:
            return 0.0

        evidenced_results = 0
        for result in results:
            metadata = result.metadata or {}
            if "original_sources" in metadata:
                if any(
                    s in self._EVIDENCE_SOURCE_VALUES
                    for s in metadata["original_sources"]
                ):
                    evidenced_results += 1
            elif metadata.get("source_count", 1) >= 2:
                evidenced_results += 1

        return round(evidenced_results / len(results), 4)

    def _calculate_deterministic_confidence(self, results: List[SearchResult]) -> float:
        """Calculate deterministic confidence from final relevance scores."""
        if not results:
            return 0.0

        scores = [max(0.0, min(1.0, result.relevance_score)) for result in results]
        return round(sum(scores) / len(scores), 4)

    def _get_hybrid_suggestions(
        self,
        search_request: SearchQuery,
        source_results: Dict[SearchSourceType, SearchSourceResult],
    ) -> List[str]:
        """Derive query suggestions from this search's own top titles.

        SearchSourceResult has never carried a ``suggestions`` field, so the
        old double-hasattr collected nothing and hybrid suggestions were
        permanently empty (R2-L25). Titles are normalized the same way the
        fulltext suggestion query normalizes them.

        GOO-399: graph hits are skipped. Their "title" is an entity name, not
        a document title, and nothing re-checks them against PostgreSQL;
        fusion only lets them corroborate a document another source returned,
        so they must not reach the response through suggestions either.
        """
        import re as _re

        suggestions: List[str] = []
        for source_result in source_results.values():
            if (
                not source_result.success
                or source_result.source_type == SearchSourceType.KNOWLEDGE_GRAPH
            ):
                continue
            for result in source_result.results[:5]:
                title = ""
                if result.search_result:
                    title = result.search_result.title or ""
                title = title or getattr(result, "title", "") or ""
                cleaned = (
                    _re.sub(r"\s+", " ", _re.sub(r"[^a-zA-Z0-9\s]", " ", title))
                    .strip()
                    .lower()
                )
                if cleaned and cleaned not in suggestions:
                    suggestions.append(cleaned)
                if len(suggestions) >= 5:
                    break
            if len(suggestions) >= 5:
                break
        return suggestions[:5]

    def _fallback_to_fulltext(
        self,
        search_request: SearchQuery,
        user_id: Optional[str],
        organization_id: Optional[str],
    ) -> SearchResponse:
        """Fallback to full-text search if hybrid search fails"""
        logger.warning("Hybrid search failed, falling back to full-text search")

        try:
            ft_search_request = SearchQuery(
                query=search_request.query,
                search_type=SearchType.FULLTEXT,
                limit=search_request.limit,
                offset=search_request.offset,
                filters=search_request.filters,
                include_snippets=search_request.include_snippets,
            )

            return fulltext_search_service.search(
                search_request=ft_search_request,
                user_id=user_id,
                organization_id=organization_id,
            )
        except Exception as e:
            logger.error(f"Fallback full-text search also failed: {e}")
            return SearchResponse(
                query=search_request.query,
                search_id=str(uuid.uuid4()),
                search_type=SearchType.FULLTEXT,
                results=[],
                total_results=0,
                returned_results=0,
                search_time_ms=0,
                limit=search_request.limit,
                offset=search_request.offset,
                has_more=False,
                suggestions=[],
            )


# Global hybrid search service instance
hybrid_search_service = HybridSearchService()
