"""Bounded PDF page selection shared by native chat and integration tools.

This module never reads documents or owns a database session. Callers supply
already-authorized text and must recheck access after asynchronous work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MAX_RERANK_PAGES = 100
MAX_PAGE_FILTER_SOURCE_CHARS = 1024 * 1024
PAGE_RERANK_TIMEOUT_SECONDS = 10.0
Page = tuple[int, str, int]
RankedPage = tuple[int, str, int, float]


def scan_pages(
    source: str,
    *,
    max_pages: int | None = None,
    max_chars: int | None = None,
    page_range: tuple[int, int] | None = None,
) -> tuple[list[tuple[int, str, int]], int]:
    """Count all markers while copying bounded candidate text.

    ``page_range`` uses the extracted text's one-based page numbers. Apply it
    before the candidate cap so a narrow selection beyond page 100 still works.
    """
    pages: list[tuple[int, str, int]] = []
    total = 0

    def span_text(start: int, end: int) -> tuple[str, int]:
        # Find stripped bounds without first allocating the entire page body.
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        stop = end if max_chars is None else min(end, start + max_chars)
        return source[start:stop], end - start

    def append_page(marker: re.Match[str], end: int) -> None:
        nonlocal total
        total += 1
        number = int(marker.group(1))
        if page_range is not None and not page_range[0] <= number <= page_range[1]:
            return
        if max_pages is not None and len(pages) >= max_pages:
            return
        text, length = span_text(marker.end(), end)
        if total == 1:
            preamble, prefix_length = span_text(0, marker.start())
            if prefix_length:
                text = preamble + (f"\n{text}" if length else "")
                length = prefix_length + (1 + length if length else 0)
        pages.append((int(marker.group(1)), text[:max_chars], length))

    previous = None
    # Nine digits already allow implausibly large page counts. Longer markers
    # remain text, avoiding both integer conversion failures and huge metadata.
    for marker in re.finditer(r"(?m)^[ \t]*\[Page[ \t]+(\d{1,9})\][ \t]*\r?$", source):
        if previous is not None:
            append_page(previous, marker.start())
        previous = marker
    if previous is not None:
        append_page(previous, len(source))
    return pages, total


@dataclass(frozen=True)
class PageSelection:
    pages: list[RankedPage]
    total_pages: int
    short_circuited: bool
    model: str | None = None
    elapsed_ms: float | None = None

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "accepted_pages": [page[0] for page in self.pages],
            "total_pages": self.total_pages,
            "short_circuited": self.short_circuited,
            "model": self.model,
        }
        if self.elapsed_ms is not None:
            result["timing_ms"] = {
                "average": self.elapsed_ms,
                "min": self.elapsed_ms,
                "max": self.elapsed_ms,
                "count": 1,
            }
            result["priorities"] = [
                {"page": number, "score": score}
                for number, _text, _length, score in self.pages
            ]
        return result


async def rank_pages(
    pages: list[Page],
    total_pages: int,
    query: str,
    *,
    top_n: int,
    short_circuit: int,
    timeout_seconds: float = PAGE_RERANK_TIMEOUT_SECONDS,
    before_provider: Callable[[], Awaitable[Any]] | None = None,
) -> PageSelection | None:
    """Return selected pages in page order; None requests the caller's fallback.

    An optional hook lets a read-only caller release a connection before remote
    I/O. Native chat closes its own read session before calling this function.
    """
    from src.services.search.cohere_rerank_service import cohere_rerank_service

    if not any(page[1] for page in pages):
        return None
    if total_pages <= short_circuit:
        accepted = [(number, text, length, 1.0) for number, text, length in pages]
        return PageSelection(
            sorted(accepted, key=lambda page: page[0]), total_pages, True
        )
    if not cohere_rerank_service.is_enabled:
        return None
    candidates = [page for page in pages if page[1]]
    if before_provider is not None:
        await before_provider()
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(
            cohere_rerank_service.rerank_with_outcome(
                query,
                documents=[
                    {"id": str(number), "content": text}
                    for number, text, _ in candidates
                ],
                top_n=top_n,
            ),
            timeout=timeout_seconds,
        )
        if not outcome.succeeded:
            return None
        scores = {result.index: result.relevance_score for result in outcome.results}
        accepted = [
            (number, text, length, scores[index])
            for index, (number, text, length) in enumerate(candidates)
            if index in scores
        ]
        accepted.sort(key=lambda page: page[0])
        return PageSelection(
            accepted,
            total_pages,
            False,
            cohere_rerank_service.model,
            (time.monotonic() - started) * 1000,
        )
    except Exception:
        logger.warning("PDF page rerank unavailable", exc_info=True)
        return None


def json_text_prefix(text: str, budget: int, *, ensure_ascii: bool = False) -> str:
    """Longest prefix fitting an escaped UTF-8 JSON string's content budget."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        size = len(json.dumps(text[:mid], ensure_ascii=ensure_ascii).encode()) - 2
        if size <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]
