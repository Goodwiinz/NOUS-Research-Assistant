"""Transient arXiv full text for harness reads.

Nothing is persisted to Postgres or object storage; ingest stays the only path
to a NOUS document. Extracted text lives in Redis for ``TTL_S`` only.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

TTL_S = 24 * 3600
# Keeps one page under the 64 KiB gateway cap after JSON escaping.
MAX_PAGE_CHARS = 48_000
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$")
Fetch = Callable[[str], Awaitable[str]]


def _key(arxiv_id: str) -> str:
    return f"arxiv:fulltext:{arxiv_id}"


async def fetch_text(arxiv_id: str) -> str:
    """Download + extract through the existing service (rate gate, 429 handling)."""
    from src.services.arxiv.arxiv_service import ArXivIngestionService

    async with ArXivIngestionService() as svc:
        pdf = await svc.download_paper_pdf(arxiv_id)
        if not pdf:
            raise LookupError("arxiv_unavailable")
        extracted = await svc.extract_pdf_content(pdf)
    return str(extracted.get("full_text") or "")


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
    text = await redis.get(_key(arxiv_id))
    if text is None:
        text = await (fetch or fetch_text)(arxiv_id)
        await redis.set(_key(arxiv_id), text, ex=TTL_S)
    chunk = text[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(text) else None
    return {
        "arxiv_id": arxiv_id,
        "offset": offset,
        "next_offset": nxt,
        "total_chars": len(text),
        "text": chunk,
    }
