"""Crossref source connector."""

import re
from typing import Any, Dict, List, Optional

import httpx

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.connectors.provider_http import get

JATS_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
MAX_RESULTS_LIMIT = 200


class CrossrefConnector(SourceConnector):
    """Connector for the Crossref REST API."""

    def __init__(self, mailto: Optional[str] = None) -> None:
        self.mailto = mailto

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> List[SourceDocument]:
        """Search Crossref for works matching the query."""
        max_results = min(max_results, MAX_RESULTS_LIMIT)

        params: Dict[str, Any] = {"query": query, "rows": max_results}
        headers: Dict[str, str] = {}
        if self.mailto:
            headers["User-Agent"] = f"RAGSystem/2.1 (mailto:{self.mailto})"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await get(
                client,
                "https://api.crossref.org/works",
                provider="crossref",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        items = data.get("message", {}).get("items", [])
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
        return documents
