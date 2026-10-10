"""Semantic Scholar source connector."""

from typing import Any, Dict, List, Optional

import httpx

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get


class SemanticScholarConnector(SourceConnector):
    """Connector for the Semantic Scholar API."""

    endpoint = "https://api.semanticscholar.org/graph/v1/paper/search"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search Semantic Scholar for papers matching the query."""
        if not 1 <= max_results <= 200:
            raise ValueError("max_results must be between 1 and 200")
        params: Dict[str, Any] = {
            "query": query,
            "limit": min(100, max(1, max_results)),
            "fields": "paperId,title,abstract,authors,url,externalIds,publicationDate,publicationTypes,openAccessPdf",
        }

        headers: Dict[str, str] = {}
        trace = search_trace
        if self.api_key:
            headers["x-api-key"] = self.api_key

        documents: List[SourceDocument] = []

        def to_document(paper: Dict[str, Any]) -> SourceDocument:
            authors = [a["name"] for a in paper.get("authors") or [] if a.get("name")]
            return SourceDocument(
                connector_type="semantic_scholar",
                external_id=paper.get("paperId"),
                title=paper.get("title") or "",
                authors=authors,
                abstract=paper.get("abstract"),
                url=paper.get("url"),
                metadata={
                    "doi": (paper.get("externalIds") or {}).get("DOI"),
                    "pmid": (paper.get("externalIds") or {}).get("PubMed"),
                    "pmcid": (paper.get("externalIds") or {}).get("PubMedCentral"),
                    "arxiv": (paper.get("externalIds") or {}).get("ArXiv"),
                    "publication_date": paper.get("publicationDate"),
                    "publication_type": paper.get("publicationTypes"),
                    "full_text_url": (paper.get("openAccessPdf") or {}).get("url"),
                },
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            while len(documents) < max_results:
                request_params = dict(params)
                if trace is not None:
                    await trace.begin_request(self.endpoint, request_params)
                response = await get(
                    client,
                    self.endpoint,
                    provider="semantic_scholar",
                    params=params,
                    headers=headers,
                    search_trace=trace,
                )
                data = response.json()
                batch = data.get("data") or []
                remaining = max_results - len(documents)
                page_documents = [to_document(paper) for paper in batch[:remaining]]
                documents.extend(page_documents)
                next_offset = data.get("next")
                if trace is not None:
                    await trace.record_page(
                        endpoint=self.endpoint,
                        params=request_params,
                        response=response,
                        documents=page_documents,
                        total_available=data.get("total"),
                        next_cursor=next_offset,
                        has_more=(
                            len(batch) > len(page_documents)
                            or isinstance(next_offset, int)
                        ),
                        provider_count=len(batch),
                    )
                if (
                    not batch
                    or not isinstance(next_offset, int)
                    or next_offset <= params.get("offset", 0)
                ):
                    break
                params = {
                    **params,
                    "offset": next_offset,
                    "limit": min(100, max_results - len(documents)),
                }
        return documents
