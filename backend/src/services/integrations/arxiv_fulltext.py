"""Transient arXiv full text for harness reads.

Nothing is persisted to Postgres or object storage; ingest stays the only path
to a NOUS document. Extracted text lives in Redis for ``STALE_TTL_S`` only and
the downloaded PDF lives in a temporary directory for the duration of one call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

TTL_S = 24 * 3600  # entry is fresh this long; after that a refetch is tried
STALE_TTL_S = 7 * 24 * 3600  # entry survives this long as a fallback
# Keeps one page under the 64 KiB gateway cap after JSON escaping.
MAX_PAGE_CHARS = 48_000
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$")
Fetch = Callable[[str], Awaitable[str]]


def _key(arxiv_id: str) -> str:
    return f"arxiv:fulltext:{arxiv_id}"


async def fetch_text(arxiv_id: str) -> str:
    """Download + extract through the existing service (rate gate, 429 handling).

    The PDF is written to a temporary directory that is removed on return, so a
    ``tools:read`` caller cannot fill the shared ``data/arxiv`` volume.
    """
    from src.services.arxiv import arxiv_service as mod

    with tempfile.TemporaryDirectory(prefix="arxiv-fulltext-") as tmp:
        async with mod.ArXivIngestionService({"arxiv_download_dir": tmp}) as svc:
            # download_paper_pdf skips the cross-pod gate (only the API
            # request path uses it); the helper is module-private but it is
            # the one shared arXiv gate, so reserve a slot the same way.
            slot_wait = await mod._acquire_arxiv_rate_slot()
            if slot_wait:
                await asyncio.sleep(slot_wait)
            pdf = await svc.download_paper_pdf(arxiv_id)
            if not pdf:
                raise LookupError("arxiv_unavailable")
            # extract_pdf_content is an async def with a purely synchronous
            # pypdf body; run it on its own loop in a worker thread so the
            # parse never blocks the API event loop.
            extracted = await asyncio.to_thread(
                lambda: asyncio.run(svc.extract_pdf_content(pdf))
            )
    return str(extracted.get("full_text") or "")


async def _cached(redis: Any, key: str) -> tuple[str | None, float]:
    """(text, fetched_at); an unreachable cache is a miss."""
    try:
        raw = await redis.get(key)
    except Exception:
        logger.warning("arxiv fulltext cache read failed", exc_info=True)
        return None, 0.0
    if raw:
        try:
            entry = json.loads(raw)
            return str(entry["text"]), float(entry["fetched_at"])
        except (ValueError, KeyError, TypeError):
            # Legacy plain-string value or garbage: treat as a miss.
            logger.debug("arxiv fulltext cache entry unreadable: %s", key)
    return None, 0.0


async def _store(redis: Any, key: str, text: str) -> None:
    try:
        await redis.set(
            key, json.dumps({"text": text, "fetched_at": time.time()}), ex=STALE_TTL_S
        )
    except Exception:
        logger.warning("arxiv fulltext cache write failed", exc_info=True)


async def get_page(
    arxiv_id: str,
    *,
    offset: int,
    limit: int,
    redis: Any,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    if not _ARXIV_ID.match(arxiv_id):
        raise ValueError("invalid arxiv id")
    limit = max(1, min(int(limit), MAX_PAGE_CHARS))
    offset = max(0, int(offset))
    key = _key(arxiv_id)
    text, fetched_at = await _cached(redis, key)
    if text is None or time.time() - fetched_at > TTL_S:
        try:
            text = await (fetch or fetch_text)(arxiv_id)
        except Exception:
            if text is None:
                raise
            logger.warning(
                "arxiv fulltext refetch failed; serving stale", exc_info=True
            )
        else:
            if text:  # never cache an empty extraction; the next call retries
                await _store(redis, key, text)
    chunk = text[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(text) else None
    return {
        "arxiv_id": arxiv_id,
        "offset": offset,
        "next_offset": nxt,
        "total_chars": len(text),
        "text": chunk,
    }
