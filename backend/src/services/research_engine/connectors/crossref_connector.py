"""Crossref source connector."""

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

import httpx

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get

logger = logging.getLogger(__name__)
JATS_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
MAX_RESULTS_LIMIT = 200
# GOO-319: DOIs per ``filter=updates:`` request and notices per response.
NOTICE_BATCH = 20
NOTICE_ROWS = 1000


class CrossrefConnector(SourceConnector):
    """Connector for the Crossref REST API."""

    endpoint = "https://api.crossref.org/works"

    def __init__(self, mailto: Optional[str] = None) -> None:
        self.mailto = mailto

    def _headers(self) -> Dict[str, str]:
        if not self.mailto:
            return {}
        return {"User-Agent": f"RAGSystem/2.1 (mailto:{self.mailto})"}

    async def update_notices(self, dois: Sequence[str]) -> Dict[str, List[Dict]]:
        """Correction/retraction records (GOO-319): ``{doi: [{notice_doi, type,
        date, asserted_by}]}`` from the Crossref works that list the DOI in ``update-to``
        (``GET /works?filter=updates:{doi}``, at most ``NOTICE_BATCH`` DOIs a
        request). A DOI whose request failed is absent from the result, so
        the caller can never read an outage as "no notice". Types are
        normalized (``expression-of-concern`` -> ``expression_of_concern``),
        not filtered."""
        wanted = sorted({doi.strip().lower() for doi in dois if doi.strip()})
        found: Dict[str, List[Dict]] = {}
        async with httpx.AsyncClient(timeout=60.0) as client:
            for start in range(0, len(wanted), NOTICE_BATCH):
                batch = wanted[start : start + NOTICE_BATCH]
                params = {
                    "filter": ",".join(f"updates:{doi}" for doi in batch),
                    "rows": NOTICE_ROWS,
                }
                try:
                    response = await get(
                        client,
                        self.endpoint,
                        provider="crossref",
                        params=params,
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    items = response.json()["message"]["items"]
                except Exception as error:  # noqa: BLE001 - recorded as failed
                    # Provider errors can carry URLs; log the type only.
                    logger.warning(
                        "crossref update notices failed: %s", type(error).__name__
                    )
                    continue
                batch_notices: Dict[str, List[Dict]] = {doi: [] for doi in batch}
                for item in items:
                    for notice in _notices(item):
                        if notice["target"] in batch_notices:
                            target = notice.pop("target")
                            if notice not in batch_notices[target]:
                                batch_notices[target].append(notice)
                for doi, notices in batch_notices.items():
                    found[doi] = sorted(
                        notices, key=lambda n: (n["notice_doi"], n["type"])
                    )
        return found

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
        headers = self._headers()

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
                        # GOO-319: the publisher's deposit of this record
                        # version (``indexed`` moves on every reindex).
                        "provider_updated": (item.get("deposited") or {}).get(
                            "date-time"
                        ),
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


def _notices(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One notice per ``update-to`` entry of a Crossref work."""
    notice_doi = str(item.get("DOI") or "").lower()
    out = []
    for update in item.get("update-to") or []:
        target = str(update.get("DOI") or "").strip().lower()
        kind = re.sub(r"[\s-]+", "_", str(update.get("type") or "").strip().lower())
        if not target or not kind or not notice_doi:
            continue
        updated = update.get("updated") or {}
        date = updated.get("date-time")
        if date is None and updated.get("date-parts"):
            date = "-".join(str(p) for p in updated["date-parts"][0])
        out.append(
            {
                "target": target,
                "notice_doi": notice_doi,
                "type": kind,
                "date": date,
                # ``publisher`` or e.g. ``retraction-watch`` (Crossref-hosted).
                "asserted_by": update.get("source"),
            }
        )
    return out
