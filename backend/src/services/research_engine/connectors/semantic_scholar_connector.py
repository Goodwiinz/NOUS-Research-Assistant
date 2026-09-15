"""Semantic Scholar source connector."""

from typing import Any, Dict, List, Optional

import httpx

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.connectors.provider_http import get


class SemanticScholarConnector(SourceConnector):
    """Connector for the Semantic Scholar API."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
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
        if self.api_key:
            headers["x-api-key"] = self.api_key

        papers: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            while len(papers) < max_results:
                response = await get(
                    client,
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    provider="semantic_scholar",
                    params=params,
                    headers=headers,
                )
                data = response.json()
                batch = data.get("data") or []
                papers.extend(batch)
                next_offset = data.get("next")
                if (
                    not batch
                    or not isinstance(next_offset, int)
                    or next_offset <= params.get("offset", 0)
                ):
                    break
                params = {
                    **params,
                    "offset": next_offset,
                    "limit": min(100, max_results - len(papers)),
                }
        documents: List[SourceDocument] = []

        for paper in papers[:max_results]:
            authors = [a["name"] for a in paper.get("authors") or [] if a.get("name")]
            documents.append(
                SourceDocument(
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
            )

        return documents
