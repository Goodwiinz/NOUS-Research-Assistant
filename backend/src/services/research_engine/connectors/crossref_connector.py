"""Crossref source connector."""

import re
from typing import Any, Dict, List, Optional

import httpx

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get

JATS_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
MAX_RESULTS_LIMIT = 200


class CrossrefConnector(SourceConnector):
    """Connector for the Crossref REST API."""

    endpoint = "https://api.crossref.org/works"

    def __init__(self, mailto: Optional[str] = None) -> None:
        self.mailto = mailto

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search Crossref for works matching the query."""
        trace = search_trace
        max_results = min(max_results, MAX_RESULTS_LIMIT)

        params: Dict[str, Any] = {"query": query, "rows": max_results}
        headers: Dict[str, str] = {}
        if self.mailto:
            headers["User-Agent"] = f"RAGSystem/2.1 (mailto:{self.mailto})"

        async with httpx.AsyncClient(timeout=60.0) as client:
            if trace is not None:
                await trace.begin_request(self.endpoint, params)
            response = await get(
                client,
                self.endpoint,
                provider="crossref",
                params=params,
                headers=headers,
                search_trace=trace,
            )
            response.raise_for_status()
            data = response.json()

        items = data.get("message", {}).get("items", [])
        total_available = data.get("message", {}).get("total-results")
        documents: List[SourceDocument] = []

        for item in items:
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""

            authors = []
            for author in item.get("author", []):
                given = author.get("given", "")
                family = author.get("family", "")
                name = f"{given} {family}".strip()
                if name:
                    authors.append(name)

            abstract = item.get("abstract", "")
            if abstract:
                abstract = JATS_TAG_RE.sub("", abstract).strip()

            documents.append(
                SourceDocument(
                    connector_type="crossref",
                    external_id=item.get("DOI"),
                    title=title,
                    authors=authors,
                    abstract=abstract or None,
                    url=item.get("URL"),
                    metadata={
                        "doi": item.get("DOI"),
                        "journal": (item.get("container-title") or [None])[0],
                        "citation_count": item.get("is-referenced-by-count"),
                        "published": item.get("published-print", {}).get("date-parts"),
                    },
                )
            )
        if trace is not None:
            await trace.record_page(
                endpoint=self.endpoint,
                params=params,
                response=response,
                documents=documents,
                total_available=total_available,
                has_more=(
                    total_available > len(items)
                    if isinstance(total_available, int)
                    else None
                ),
                provider_count=len(items),
            )
        return documents
