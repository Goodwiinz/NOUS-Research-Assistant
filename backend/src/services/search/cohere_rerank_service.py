"""
Cohere Reranking Service

Uses Azure AI Cohere rerank API to improve precision@K scores by reranking
search results based on query-document relevance.
"""

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.core.circuit_breaker import ServiceUnavailableError, get_circuit_breaker
from src.core.config import settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2


def _normalize_rerank_endpoint(endpoint: Optional[str]) -> Optional[str]:
    """Accept Azure SDK bases while preserving full/custom operation URLs."""
    if not endpoint:
        return endpoint
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return endpoint
    azure_suffixes = (
        ".services.ai.azure.com",
        ".cognitiveservices.azure.com",
        ".openai.azure.com",
    )
    if (
        parsed.scheme in {"https", "http"}
        and parsed.hostname
        and parsed.hostname.endswith(azure_suffixes)
        and parsed.path.rstrip("/") == "/openai/v1"
    ):
        return urlunsplit(
            parsed._replace(path="/providers/cohere/v2/rerank", fragment="")
        )
    return endpoint


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        return min(30.0, max(0.0, float(response.headers.get("Retry-After", ""))))
    except ValueError:
        return 0.5 * (2**attempt)


@dataclass
class RerankResult:
    """Result from reranking operation"""

    document_id: str
    index: int
    relevance_score: float
    original_score: float


@dataclass(frozen=True)
class RerankOutcome:
    """Per-invocation rerank results and fallback provenance.

    ``last_failure`` remains available as a context-local legacy diagnostic,
    but callers that make correctness decisions consume this explicit value so
    results and provenance cannot drift apart.
    """

    results: List[RerankResult]
    failure: Optional[Dict[str, Any]] = None

    @property
    def succeeded(self) -> bool:
        return self.failure is None


class CohereRerankService:
    """
    Service for reranking search results using Azure AI Cohere rerank API.

    This improves Precision@K by using a cross-encoder model to score
    query-document pairs more accurately than initial retrieval.
    """

    def __init__(self):
        self.endpoint = _normalize_rerank_endpoint(settings.COHERE_RERANK_ENDPOINT)
        self.api_key = settings.COHERE_RERANK_API_KEY
        self.model = settings.COHERE_RERANK_MODEL
        self.default_top_n = settings.COHERE_RERANK_TOP_N
        self._enabled = bool(self.endpoint and self.api_key)
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None
        self._last_failure: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
            f"cohere_last_failure_{id(self)}", default=None
        )

        if self._enabled:
            logger.info(f"Cohere reranking enabled with model: {self.model}")
        else:
            logger.warning("Cohere reranking disabled - missing endpoint or API key")

    @property
    def is_enabled(self) -> bool:
        """Check if reranking is enabled"""
        return self._enabled

    @property
    def last_failure(self) -> Optional[Dict[str, Any]]:
        """Invocation-local diagnostic failure for legacy callers.

        A ``ContextVar`` keeps concurrent asyncio tasks (and worker threads)
        from overwriting each other's provenance on the shared service.
        Correctness-sensitive callers still consume ``RerankOutcome``.
        """
        return self._last_failure.get()

    @last_failure.setter
    def last_failure(self, value: Optional[Dict[str, Any]]) -> None:
        self._last_failure.set(value)

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=30.0)
        return self._async_client

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=30.0)
        return self._sync_client

    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """
        Rerank documents based on query relevance using Cohere API.

        Args:
            query: The search query
            documents: List of documents with 'content' and 'id' fields
            top_n: Number of top results to return (default: settings.COHERE_RERANK_TOP_N)
            return_documents: Whether to include document text in response

        Returns:
            List of RerankResult sorted by relevance score (highest first)
        """
        self.last_failure = None

        if not self._enabled:
            logger.debug("Reranking disabled, returning original order")
            self.last_failure = {"reason": "service_disabled"}
            return self._fallback_rerank(documents, top_n)

        if not documents:
            return []

        # Check circuit breaker before making Cohere API call
        breaker = get_circuit_breaker("cohere")
        if breaker and not breaker.can_execute():
            logger.warning("Cohere circuit breaker is open - using fallback reranking")
            self.last_failure = {"reason": "circuit_open"}
            return self._fallback_rerank(documents, top_n)

        top_n = top_n or self.default_top_n
        top_n = min(top_n, len(documents))

        start_time = time.time()

        try:
            # Prepare documents for Cohere API
            doc_texts = []
            doc_ids = []
            original_scores = []

            for doc in documents:
                # Extract text content - try multiple fields
                content = (
                    doc.get("content")
                    or doc.get("content_snippet")
                    or doc.get("text")
                    or doc.get("title", "")
                )
                doc_texts.append(content[:4096])  # Cohere has token limits
                doc_ids.append(str(doc.get("id") or doc.get("document_id", "")))
                original_scores.append(
                    doc.get("relevance_score", doc.get("score", 0.0))
                )

            # Reuse the connection pool and honor Cohere's 429 retry window.
            for attempt in range(_MAX_ATTEMPTS):
                response = await self._get_async_client().post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": doc_texts,
                        "top_n": top_n,
                        "return_documents": return_documents,
                    },
                )
                if response.status_code != 429 or attempt == _MAX_ATTEMPTS - 1:
                    response.raise_for_status()
                    break
                await asyncio.sleep(_retry_delay(response, attempt))
            result = response.json()

            elapsed_ms = (time.time() - start_time) * 1000
            logger.info(
                f"Cohere rerank completed in {elapsed_ms:.0f}ms for {len(documents)} docs"
            )

            # Parse results
            rerank_results = []
            for item in result.get("results", []):
                idx = item["index"]
                rerank_results.append(
                    RerankResult(
                        document_id=doc_ids[idx],
                        index=idx,
                        relevance_score=item["relevance_score"],
                        original_score=original_scores[idx],
                    )
                )

            # Sort by relevance score (highest first)
            rerank_results.sort(key=lambda x: x.relevance_score, reverse=True)

            # Record circuit breaker success
            if breaker:
                breaker.record_success()
            self.last_failure = None

            return rerank_results

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else None
            failure_reason = (
                "endpoint_not_found" if status_code == 404 else "upstream_http_error"
            )
            self.last_failure = {
                "reason": failure_reason,
                "status_code": status_code,
                "response_text": (e.response.text if e.response else "")[:500],
            }
            logger.error(
                f"Cohere API error: {e.response.status_code} - {e.response.text}"
            )
            # Record circuit breaker failure
            if breaker:
                breaker.record_failure(e)
            return self._fallback_rerank(documents, top_n)

        except Exception as e:
            logger.error(f"Cohere rerank failed: {e}")
            self.last_failure = {"reason": "unexpected_error", "error": str(e)}
            # Record circuit breaker failure
            if breaker:
                breaker.record_failure(e)
            return self._fallback_rerank(documents, top_n)

    async def rerank_with_outcome(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> RerankOutcome:
        """Return results and provenance as one invocation-local value.

        There is intentionally no await between ``rerank`` returning and the
        snapshot of ``last_failure``. Asyncio therefore cannot resume another
        caller of the singleton service in between those operations.
        """
        results = await self.rerank(
            query,
            documents,
            top_n=top_n,
            return_documents=return_documents,
        )
        failure = dict(self.last_failure) if self.last_failure is not None else None
        return RerankOutcome(results=results, failure=failure)

    def _fallback_rerank(
        self, documents: List[Dict[str, Any]], top_n: Optional[int] = None
    ) -> List[RerankResult]:
        """Fallback when API is unavailable - return original order"""
        top_n = top_n or self.default_top_n
        top_n = min(top_n, len(documents))

        results = []
        for i, doc in enumerate(documents[:top_n]):
            score = doc.get("relevance_score", doc.get("score", 1.0 - (i * 0.1)))
            results.append(
                RerankResult(
                    document_id=str(doc.get("id") or doc.get("document_id", "")),
                    index=i,
                    relevance_score=score,
                    original_score=score,
                )
            )

        return results

    def rerank_sync(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> List[RerankResult]:
        """
        Synchronous version of rerank for use in non-async contexts.

        Args:
            query: The search query
            documents: List of documents with 'content' and 'id' fields
            top_n: Number of top results to return
            return_documents: Whether to include document text in response

        Returns:
            List of RerankResult sorted by relevance score (highest first)
        """
        self.last_failure = None

        if not self._enabled:
            logger.debug("Reranking disabled, returning original order")
            self.last_failure = {"reason": "service_disabled"}
            return self._fallback_rerank(documents, top_n)

        if not documents:
            return []

        # Check circuit breaker before making Cohere API call
        breaker = get_circuit_breaker("cohere")
        if breaker and not breaker.can_execute():
            logger.warning("Cohere circuit breaker is open - using fallback reranking")
            self.last_failure = {"reason": "circuit_open"}
            return self._fallback_rerank(documents, top_n)

        top_n = top_n or self.default_top_n
        top_n = min(top_n, len(documents))

        start_time = time.time()

        try:
            # Prepare documents for Cohere API
            doc_texts = []
            doc_ids = []
            original_scores = []

            for doc in documents:
                content = (
                    doc.get("content")
                    or doc.get("content_snippet")
                    or doc.get("text")
                    or doc.get("title", "")
                )
                doc_texts.append(content[:4096])
                doc_ids.append(str(doc.get("id") or doc.get("document_id", "")))
                original_scores.append(
                    doc.get("relevance_score", doc.get("score", 0.0))
                )

            # Reuse the connection pool and honor Cohere's 429 retry window.
            for attempt in range(_MAX_ATTEMPTS):
                response = self._get_sync_client().post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": doc_texts,
                        "top_n": top_n,
                        "return_documents": return_documents,
                    },
                )
                if response.status_code != 429 or attempt == _MAX_ATTEMPTS - 1:
                    response.raise_for_status()
                    break
                time.sleep(_retry_delay(response, attempt))
            result = response.json()

            elapsed_ms = (time.time() - start_time) * 1000
            logger.info(
                f"Cohere rerank (sync) completed in {elapsed_ms:.0f}ms for {len(documents)} docs"
            )

            # Parse results
            rerank_results = []
            for item in result.get("results", []):
                idx = item["index"]
                rerank_results.append(
                    RerankResult(
                        document_id=doc_ids[idx],
                        index=idx,
                        relevance_score=item["relevance_score"],
                        original_score=original_scores[idx],
                    )
                )

            rerank_results.sort(key=lambda x: x.relevance_score, reverse=True)

            # Record circuit breaker success
            if breaker:
                breaker.record_success()
            self.last_failure = None

            return rerank_results

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response else None
            failure_reason = (
                "endpoint_not_found" if status_code == 404 else "upstream_http_error"
            )
            self.last_failure = {
                "reason": failure_reason,
                "status_code": status_code,
                "response_text": (e.response.text if e.response else "")[:500],
            }
            logger.error(
                f"Cohere API error: {e.response.status_code} - {e.response.text}"
            )
            # Record circuit breaker failure
            if breaker:
                breaker.record_failure(e)
            return self._fallback_rerank(documents, top_n)

        except Exception as e:
            logger.error(f"Cohere rerank (sync) failed: {e}")
            self.last_failure = {"reason": "unexpected_error", "error": str(e)}
            # Record circuit breaker failure
            if breaker:
                breaker.record_failure(e)
            return self._fallback_rerank(documents, top_n)

    def rerank_sync_with_outcome(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: Optional[int] = None,
        return_documents: bool = False,
    ) -> RerankOutcome:
        """Synchronous counterpart of :meth:`rerank_with_outcome`."""
        results = self.rerank_sync(
            query,
            documents,
            top_n=top_n,
            return_documents=return_documents,
        )
        failure = dict(self.last_failure) if self.last_failure is not None else None
        return RerankOutcome(results=results, failure=failure)

    async def rerank_search_results(
        self,
        query: str,
        results: List[Any],
        top_n: Optional[int] = None,
        content_field: str = "content_snippet",
    ) -> List[Any]:
        """
        Convenience method to rerank SearchResult objects directly.

        Args:
            query: The search query
            results: List of SearchResult objects (or dicts with similar structure)
            top_n: Number of results to return
            content_field: Field name containing the text content

        Returns:
            Reranked list of results with updated scores
        """
        if not results:
            return []

        # Convert SearchResult objects to dicts for reranking
        documents = []
        for r in results:
            if hasattr(r, "dict"):
                doc = r.dict() if callable(r.dict) else dict(r)
            elif hasattr(r, "__dict__"):
                doc = vars(r)
            else:
                doc = dict(r)

            # Ensure we have content
            doc["content"] = (
                doc.get(content_field)
                or doc.get("content_snippet")
                or doc.get("content")
                or doc.get("title", "")
            )
            documents.append(doc)

        # Perform reranking
        rerank_results = await self.rerank(query, documents, top_n)

        # Create index map for fast lookup
        rerank_map = {r.index: r for r in rerank_results}

        # Build reordered results with updated scores
        reranked = []
        for rr in rerank_results:
            original_result = results[rr.index]

            # Update score if the result has this attribute
            if hasattr(original_result, "relevance_score"):
                original_result.relevance_score = rr.relevance_score
            elif isinstance(original_result, dict):
                original_result["relevance_score"] = rr.relevance_score

            reranked.append(original_result)

        return reranked


# Global service instance
cohere_rerank_service = CohereRerankService()
