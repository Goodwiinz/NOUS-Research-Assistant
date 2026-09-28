"""OpenAlex Works search; metadata and abstracts, without implicit full-text access."""

from typing import Any, Optional

import httpx

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get


class OpenAlexConnector(SourceConnector):
    endpoint = "https://api.openalex.org/works"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> list[SourceDocument]:
        if not 1 <= max_results <= 200:
            raise ValueError("max_results must be between 1 and 200")
        trace = search_trace
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        documents: list[SourceDocument] = []
        cursor = "*"
        async with httpx.AsyncClient(timeout=30.0) as client:
            while len(documents) < max_results:
                params = {
                    "search": query,
                    "per_page": min(100, max_results - len(documents)),
                    "cursor": cursor,
                }
                if trace is not None:
                    await trace.begin_request(self.endpoint, params)
                response = await get(
                    client,
                    self.endpoint,
                    provider="openalex",
                    params=params,
                    headers=headers,
                    search_trace=trace,
                )
                data = response.json()
                page_start = len(documents)
                for work in data.get("results") or []:
                    index = work.get("abstract_inverted_index") or {}
                    words = sorted(
                        (position, word)
                        for word, positions in index.items()
                        for position in positions
                    )
                    location = work.get("primary_location") or {}
                    oa = work.get("best_oa_location") or {}
                    documents.append(
                        SourceDocument(
                            connector_type="openalex",
                            external_id=work.get("id"),
                            title=work.get("display_name") or "",
                            authors=[
                                a["author"]["display_name"]
                                for a in work.get("authorships") or []
                                if (a.get("author") or {}).get("display_name")
                            ],
                            abstract=" ".join(word for _, word in words) or None,
                            url=location.get("landing_page_url")
                            or work.get("doi")
                            or work.get("id"),
                            metadata={
                                "doi": work.get("doi"),
                                "pmid": (work.get("ids") or {}).get("pmid"),
                                "pmcid": (work.get("ids") or {}).get("pmcid"),
                                "publication_date": work.get("publication_date"),
                                "publication_type": work.get("type"),
                                "open_access": work.get("open_access"),
                                "full_text_url": oa.get("pdf_url"),
                                "license": oa.get("license"),
                            },
                        )
                    )
                next_cursor = (data.get("meta") or {}).get("next_cursor")
                if trace is not None:
                    await trace.record_page(
                        endpoint=self.endpoint,
                        params=params,
                        response=response,
                        documents=documents[page_start:],
                        total_available=(data.get("meta") or {}).get("count"),
                        next_cursor=next_cursor,
                        has_more=bool(data.get("results")) and bool(next_cursor),
                        provider_count=len(data.get("results") or []),
                    )
                if not data.get("results") or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        return documents[:max_results]
