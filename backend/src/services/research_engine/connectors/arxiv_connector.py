"""ArXiv source connector."""

from typing import Any, List

import httpx
from defusedxml import ElementTree as ET

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument

ATOM_NS = "http://www.w3.org/2005/Atom"

_REQUEST_TIMEOUT_S = 30.0


class ArxivConnector(SourceConnector):
    """Connector for the arXiv API."""

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> List[SourceDocument]:
        """Search arXiv for papers matching the query."""
        from src.services.arxiv.arxiv_service import (
            field_arxiv_query,
            request_arxiv_api,
        )

        # Per-token all: AND fielding. The previous f"all:{query}" fielded
        # only the FIRST token of a multiword query ('all:retrieval-augmented
        # generation') — the rest stayed unfielded and got OR'd by arXiv
        # (codex audit on #1406, finding 4).
        params = {
            "search_query": field_arxiv_query(query),
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

        response_text = await request_arxiv_api(
            "https://export.arxiv.org/api/query",
            params,
            timeout=_REQUEST_TIMEOUT_S,
            client_factory=httpx.AsyncClient,
        )
        root = ET.fromstring(response_text)
        documents: List[SourceDocument] = []

        for entry in root.findall(f"{{{ATOM_NS}}}entry"):
            entry_id = entry.findtext(f"{{{ATOM_NS}}}id", default="")
            title = entry.findtext(f"{{{ATOM_NS}}}title", default="")
            summary = entry.findtext(f"{{{ATOM_NS}}}summary", default="")
            authors = [
                author.findtext(f"{{{ATOM_NS}}}name", default="")
                for author in entry.findall(f"{{{ATOM_NS}}}author")
            ]

            documents.append(
                SourceDocument(
                    connector_type="arxiv",
                    external_id=entry_id,
                    title=title,
                    authors=authors,
                    abstract=summary,
                    url=entry_id,
                )
            )

        return documents
