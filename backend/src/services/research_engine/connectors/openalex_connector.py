"""OpenAlex Works search; metadata and abstracts, without implicit full-text access."""

from typing import Any, Literal, Optional, cast

import httpx

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.connectors.provider_http import get

# Referenced-work ids per ``openalex_id:`` filter request (OpenAlex allows 100).
ID_BATCH_SIZE = 50


def _short_id(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def work_document(work: dict[str, Any]) -> SourceDocument:
    """One OpenAlex Work as a metadata-only document (no full text is fetched)."""
    index = work.get("abstract_inverted_index") or {}
    words = sorted(
        (position, word) for word, positions in index.items() for position in positions
    )
    location = work.get("primary_location") or {}
    oa = work.get("best_oa_location") or {}
    return SourceDocument(
        connector_type="openalex",
        external_id=work.get("id"),
        title=work.get("display_name") or "",
        authors=[
            a["author"]["display_name"]
            for a in work.get("authorships") or []
            if (a.get("author") or {}).get("display_name")
        ],
        abstract=" ".join(word for _, word in words) or None,
        url=location.get("landing_page_url") or work.get("doi") or work.get("id"),
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


class OpenAlexConnector(SourceConnector):
    endpoint = "https://api.openalex.org/works"

    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.transport = transport  # test seam: httpx.MockTransport

    def _client(self) -> httpx.AsyncClient:
        if self.transport is None:
            return httpx.AsyncClient(timeout=30.0)
        return httpx.AsyncClient(timeout=30.0, transport=self.transport)

    async def _page(
        self,
        client: httpx.AsyncClient,
        trace: SearchTrace,
        url: str,
        params: dict[str, Any],
        *,
        keep: int | None = None,
        has_more: bool | None = None,
        cursor_page: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """One traced GET; returns the body and the (at most ``keep``) results kept."""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        await trace.begin_request(url, params)
        response = await get(
            client,
            url,
            provider="openalex",
            params=params,
            headers=headers,
            search_trace=trace,
        )
        data = cast(dict[str, Any], response.json())
        results = data.get("results") or []
        kept = results if keep is None else results[:keep]
        meta = data.get("meta") or {}
        if cursor_page:
            has_more = bool(results) and bool(meta.get("next_cursor"))
        await trace.record_page(
            endpoint=url,
            params=params,
            response=response,
            documents=[work_document(work) for work in kept],
            total_available=meta.get("count"),
            next_cursor=meta.get("next_cursor"),
            has_more=has_more,
            provider_count=len(results),
        )
        return data, kept

    async def citations(
        self,
        work_id: str,
        direction: Literal["backward", "forward"],
        max_results: int,
        *,
        search_trace: SearchTrace,
        into: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Referenced (backward) or citing (forward) Works of one seed Work.

        ``work_id`` is an OpenAlex id (``W123``) or ``doi:10.x/y``. Metadata
        only, bounded by ``max_results``; every request is recorded on the trace.
        Works are appended to ``into`` page by page, so a caller keeps what was
        already fetched when a later page fails.
        """
        if not 1 <= max_results <= 200:
            raise ValueError("max_results must be between 1 and 200")
        trace = search_trace
        works: list[dict[str, Any]] = [] if into is None else into
        async with self._client() as client:
            seed: dict[str, Any] = {}
            if direction == "backward" or not work_id.startswith("W"):
                seed, _ = await self._page(
                    client,
                    trace,
                    f"{self.endpoint}/{work_id}",
                    {"select": "id,referenced_works"},
                    keep=0,
                )
            seed_id = _short_id(seed.get("id") or work_id)
            if direction == "backward":
                refs = [_short_id(r) for r in seed.get("referenced_works") or []]
                wanted = refs[:max_results]
                for start in range(0, len(wanted), ID_BATCH_SIZE):
                    batch = wanted[start : start + ID_BATCH_SIZE]
                    last = start + ID_BATCH_SIZE >= len(wanted)
                    _, kept = await self._page(
                        client,
                        trace,
                        self.endpoint,
                        {
                            "filter": "openalex_id:" + "|".join(batch),
                            "per_page": len(batch),
                        },
                        has_more=len(refs) > max_results or not last,
                    )
                    works.extend(kept)
                return works[:max_results]
            cursor = "*"
            while len(works) < max_results:
                params = {
                    "filter": f"cites:{seed_id}",
                    "per_page": min(100, max_results - len(works)),
                    "cursor": cursor,
                }
                data, kept = await self._page(
                    client,
                    trace,
                    self.endpoint,
                    params,
                    keep=max_results - len(works),
                    cursor_page=True,
                )
                works.extend(kept)
                next_cursor = (data.get("meta") or {}).get("next_cursor")
                if not kept or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        return works

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
        async with self._client() as client:
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
                    documents.append(work_document(work))
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
