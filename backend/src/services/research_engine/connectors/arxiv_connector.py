"""ArXiv source connector."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx
from defusedxml import ElementTree as ET

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)

ATOM_NS = "http://www.w3.org/2005/Atom"

logger = logging.getLogger(__name__)

# arXiv asks for one request every three seconds from a single connection,
# across every machine you control. The ingestion service enforces that with a
# Redis slot reservation shared by all pods; this connector used to skip it
# entirely and hit the API directly from Celery, which is the one path that can
# get the whole platform rate-limited on behalf of everything else.
_MAX_ATTEMPTS = 2
_REQUEST_TIMEOUT_S = 30.0


class ArxivConnector(SourceConnector):
    """Connector for the arXiv API."""

    endpoint = "https://export.arxiv.org/api/query"

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search arXiv for papers matching the query."""
        trace = search_trace
        from src.services.arxiv.arxiv_service import field_arxiv_query

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

        from src.services.arxiv.arxiv_service import (
            _acquire_arxiv_rate_slot,
            _retry_after_seconds,
        )

        response = None
        request_attempts = []
        if trace is not None:
            await trace.begin_request(self.endpoint, params)
        for attempt in range(_MAX_ATTEMPTS):
            # Reserve a slot on the shared cross-pod gate before every attempt,
            # including retries — a retry that skips the gate is exactly the
            # burst the gate exists to prevent.
            slot_wait = await _acquire_arxiv_rate_slot()
            if slot_wait:
                await asyncio.sleep(slot_wait)

            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
                # https, not http: the plain-HTTP endpoint redirects and sends
                # the query in clear text on the way.
                request_started_at = datetime.now(timezone.utc).isoformat()
                response = await client.get(
                    self.endpoint,
                    params=params,
                    follow_redirects=True,
                )
                request_attempts.append(
                    {
                        "attempt": attempt + 1,
                        "requested_at": request_started_at,
                        "status_code": response.status_code,
                    }
                )
                response.extensions["search_request_started_at"] = request_started_at
                response.extensions["search_request_attempt"] = attempt + 1
                response.extensions["search_request_attempts"] = request_attempts
                if trace is not None:
                    await trace.record_response(response)

            if response.status_code == 429 and attempt < _MAX_ATTEMPTS - 1:
                wait = _retry_after_seconds(response.headers, default=3.0)
                logger.warning(
                    "arXiv rate limited (429) in research connector; "
                    "retrying in %ss",
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                if trace is not None:
                    await trace.record_page(
                        endpoint=self.endpoint,
                        params=params,
                        response=response,
                        error_type="HTTPStatusError",
                    )
                raise
            break

        if response is None:  # pragma: no cover - loop always assigns
            return []

        root = ET.fromstring(response.text)
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

        total_text = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
        total_available = (
            int(total_text) if total_text and total_text.isdigit() else None
        )
        if trace is not None:
            await trace.record_page(
                endpoint=self.endpoint,
                params=params,
                response=response,
                documents=documents,
                total_available=total_available,
                has_more=(
                    total_available > len(documents)
                    if total_available is not None
                    else None
                ),
                provider_count=len(documents),
            )

        return documents
