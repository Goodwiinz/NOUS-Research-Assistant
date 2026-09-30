"""RAG Store source connector."""

from typing import Any, Callable, Coroutine, Dict, List, Optional

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)


class RagStoreConnector(SourceConnector):
    """Connector that searches the local RAG store via a provided async function."""

    endpoint = "rag_store.search"

    def __init__(
        self,
        search_fn: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
    ) -> None:
        self.search_fn = search_fn

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search the local RAG store."""
        trace = search_trace
        if trace is not None:
            await trace.begin_request(
                self.endpoint, {"query": query, "max_results": max_results}
            )
        result = await self.search_fn(query, max_results)
        documents: List[SourceDocument] = []
        results = result.get("results") or []

        for item in results:
            documents.append(
                SourceDocument(
                    connector_type="rag_store",
                    external_id=item.get("id"),
                    title=item.get("title", ""),
                    full_text=item.get("content"),
                    metadata=item.get("metadata", {}),
                )
            )

        if trace is not None:
            total_available = result.get("total") or result.get("total_count")
            await trace.record_page(
                endpoint=self.endpoint,
                params={"query": query, "max_results": max_results},
                documents=documents,
                total_available=total_available,
                has_more=result.get("has_more"),
                provider_count=len(results),
            )
        return documents
