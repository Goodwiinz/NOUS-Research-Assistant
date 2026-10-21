"""Bounded PDF page selection shared by native chat and integration tools.

This module never reads documents or owns a database session. Callers supply
already-authorized text and must recheck access after asynchronous work.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Literal

logger = logging.getLogger(__name__)

MAX_RERANK_PAGES = 100
MAX_PAGE_FILTER_SOURCE_CHARS = 1024 * 1024
# Native PDF chat reads the whole extracted text up to this ceiling so a long
# thesis is ranked across every page, not just the first megabyte.
MAX_PAGE_SCAN_SOURCE_CHARS = 16 * 1024 * 1024
PAGE_RERANK_TIMEOUT_SECONDS = 10.0
Page = tuple[int, str, int]
RankedPage = tuple[int, str, int, float]
PageFilterFallbackReason = Literal[
    "disabled", "provider_error", "timeout", "no_results", "no_text", "source_too_large"
]

_PAGE_MARKER = re.compile(r"(?m)^[ \t]*\[Page[ \t]+(\d{1,9})\][ \t]*\r?$")
_QUERY_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}")
_QUERY_STOPWORDS = frozenset(
    {"only", "attached", "file", "document", "please", "what", "give"}
)
_MAX_QUERY_TERMS = 32


def query_terms(query: str | None) -> list[str]:
    """Distinct lowercased query terms used for cheap lexical page scoring."""
    terms: list[str] = []
    for match in _QUERY_TERM.findall(query or ""):
        term = match.lower()
        if term not in _QUERY_STOPWORDS and term not in terms:
            terms.append(term)
            if len(terms) >= _MAX_QUERY_TERMS:
                break
    return terms


def highest_page_number(source: str) -> int | None:
    """Largest ``[Page N]`` marker; blank pages may have no marker at all."""
    highest = None
    for marker in _PAGE_MARKER.finditer(source):
        number = int(marker.group(1))
        if highest is None or number > highest:
            highest = number
    return highest


def scan_pages(
    source: str,
    *,
    max_pages: int | None = None,
    max_chars: int | None = None,
    page_range: tuple[int, int] | None = None,
    query: str | None = None,
) -> tuple[list[tuple[int, str, int]], int]:
    """Count all markers while copying bounded candidate text.

    ``page_range`` uses the extracted text's one-based page numbers. Apply it
    before the candidate cap so a narrow selection beyond page 100 still works.

    Without ``query`` the cap keeps the first ``max_pages`` pages. With a
    ``query`` and more pages than the cap, candidates are the pages with the
    best lexical overlap across the whole document (or, when the query has no
    usable terms, pages spread evenly across it), returned in page order. The
    scoring reads each page's bounded prefix once, so the work stays linear in
    the source and memory stays at ``max_pages`` copied prefixes.
    """
    terms = query_terms(query) if query is not None else None
    if terms is not None and max_pages is not None:
        return _scan_ranked_candidates(
            source,
            terms,
            max_pages=max_pages,
            max_chars=max_chars,
            page_range=page_range,
        )
    pages: list[tuple[int, str, int]] = []
    total = 0

    def append_page(marker: re.Match[str], end: int) -> None:
        nonlocal total
        total += 1
        number = int(marker.group(1))
        if page_range is not None and not page_range[0] <= number <= page_range[1]:
            return
        if max_pages is not None and len(pages) >= max_pages:
            return
        text, length = _page_text(source, marker, end, total == 1, max_chars)
        pages.append((number, text, length))

    previous = None
    # Nine digits already allow implausibly large page counts. Longer markers
    # remain text, avoiding both integer conversion failures and huge metadata.
    for marker in _PAGE_MARKER.finditer(source):
        if previous is not None:
            append_page(previous, marker.start())
        previous = marker
    if previous is not None:
        append_page(previous, len(source))
    return pages, total


def _span_text(
    source: str, start: int, end: int, max_chars: int | None
) -> tuple[str, int]:
    # Find stripped bounds without first allocating the entire page body.
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    stop = end if max_chars is None else min(end, start + max_chars)
    return source[start:stop], end - start


def _page_text(
    source: str,
    marker: re.Match[str],
    end: int,
    first: bool,
    max_chars: int | None,
) -> tuple[str, int]:
    text, length = _span_text(source, marker.end(), end, max_chars)
    if first:
        preamble, prefix_length = _span_text(source, 0, marker.start(), max_chars)
        if prefix_length:
            text = preamble + (f"\n{text}" if length else "")
            length = prefix_length + (1 + length if length else 0)
    return text[:max_chars], length


def _iter_page_bounds(source: str) -> Iterator[tuple[re.Match[str], int]]:
    previous = None
    for marker in _PAGE_MARKER.finditer(source):
        if previous is not None:
            yield previous, marker.start()
        previous = marker
    if previous is not None:
        yield previous, len(source)


def _scan_ranked_candidates(
    source: str,
    terms: list[str],
    *,
    max_pages: int,
    max_chars: int | None,
    page_range: tuple[int, int] | None,
) -> tuple[list[tuple[int, str, int]], int]:
    def in_range(marker: re.Match[str]) -> bool:
        number = int(marker.group(1))
        return page_range is None or page_range[0] <= number <= page_range[1]

    spread: set[int] | None = None
    if not terms:
        eligible = sum(1 for marker, _ in _iter_page_bounds(source) if in_range(marker))
        if eligible > max_pages:
            # No lexical signal: sample the whole document, never its head.
            step = (eligible - 1) / max(max_pages - 1, 1)
            spread = {round(index * step) for index in range(max_pages)}
    # Min-heap of the best candidates; ties keep the earlier page.
    heap: list[tuple[int, int, int, int, str, int]] = []
    total = 0
    ordinal = -1
    for marker, end in _iter_page_bounds(source):
        total += 1
        if not in_range(marker):
            continue
        ordinal += 1
        if spread is not None and ordinal not in spread:
            continue
        text, length = _page_text(source, marker, end, total == 1, max_chars)
        lowered = text.lower()
        counts = [lowered.count(term) for term in terms]
        matched = sum(1 for count in counts if count)
        occurrences = sum(min(count, 50) for count in counts)
        item = (matched, occurrences, -ordinal, int(marker.group(1)), text, length)
        if len(heap) < max_pages:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)
    if terms and ordinal + 1 > max_pages and not any(item[0] for item in heap):
        # No page shares a term with the query; spread instead of the head.
        return _scan_ranked_candidates(
            source, [], max_pages=max_pages, max_chars=max_chars, page_range=page_range
        )
    ordered = sorted(heap, key=lambda item: -item[2])
    return [(number, text, length) for _, _, _, number, text, length in ordered], total


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


@dataclass(frozen=True)
class PageFilterFallback:
    """Ranking did not run or produced nothing; callers use a text excerpt."""

    reason: PageFilterFallbackReason
    total_pages: int

    def metadata(self) -> dict[str, Any]:
        return {
            "accepted_pages": [],
            "total_pages": self.total_pages,
            "short_circuited": False,
            "model": None,
            "fallback": self.reason,
        }


try:
    from prometheus_client import REGISTRY, Counter

    _existing = getattr(REGISTRY, "_names_to_collectors", {}).get(
        "pdf_page_filter_fallback_total"
    )
    _FALLBACKS: Any = _existing or Counter(
        "pdf_page_filter_fallback_total",
        "PDF page selections that fell back to a keyword excerpt or full-text search",
        ["surface", "reason"],
    )
except Exception:  # pragma: no cover - metrics are optional
    _FALLBACKS = None


def record_page_filter_fallback(
    reason: PageFilterFallbackReason, *, document_id: str, surface: str
) -> None:
    """Log and count a degraded page selection at the point of fallback."""
    level = logging.WARNING if reason in {"provider_error", "timeout"} else logging.INFO
    logger.log(
        level,
        "PDF page filter fell back: reason=%s document_id=%s surface=%s",
        reason,
        document_id,
        surface,
        extra={
            "page_filter_fallback": reason,
            "document_id": document_id,
            "surface": surface,
        },
    )
    if _FALLBACKS is not None:
        try:
            _FALLBACKS.labels(surface=surface, reason=reason).inc()
        except Exception:  # pragma: no cover - never fail a turn on metrics
            logger.debug("page filter fallback metric failed", exc_info=True)


async def rank_pages(
    pages: list[Page],
    total_pages: int,
    query: str,
    *,
    top_n: int,
    short_circuit: int,
    timeout_seconds: float | None = None,
    before_provider: Callable[[], Awaitable[Any]] | None = None,
) -> PageSelection | PageFilterFallback:
    """Return selected pages in page order, or the reason to fall back.

    An optional hook lets a read-only caller release a connection before remote
    I/O. Native chat closes its own read session before calling this function.
    """
    from src.services.search.cohere_rerank_service import cohere_rerank_service

    if not any(page[1] for page in pages):
        return PageFilterFallback("no_text", total_pages)
    if total_pages <= short_circuit:
        accepted = [(number, text, length, 1.0) for number, text, length in pages]
        return PageSelection(
            sorted(accepted, key=lambda page: page[0]), total_pages, True
        )
    if not cohere_rerank_service.is_enabled:
        return PageFilterFallback("disabled", total_pages)
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
            timeout=(
                PAGE_RERANK_TIMEOUT_SECONDS
                if timeout_seconds is None
                else timeout_seconds
            ),
        )
        if not outcome.succeeded:
            return PageFilterFallback("provider_error", total_pages)
        scores = {result.index: result.relevance_score for result in outcome.results}
        accepted = [
            (number, text, length, scores[index])
            for index, (number, text, length) in enumerate(candidates)
            if index in scores
        ]
        if not accepted:
            # An empty ranking would answer with no paper context at all.
            return PageFilterFallback("no_results", total_pages)
        accepted.sort(key=lambda page: page[0])
        return PageSelection(
            accepted,
            total_pages,
            False,
            cohere_rerank_service.model,
            (time.monotonic() - started) * 1000,
        )
    except asyncio.TimeoutError:
        return PageFilterFallback("timeout", total_pages)
    except Exception:
        logger.warning("PDF page rerank unavailable", exc_info=True)
        return PageFilterFallback("provider_error", total_pages)


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
