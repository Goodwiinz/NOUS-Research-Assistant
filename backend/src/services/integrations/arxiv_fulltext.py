"""Transient arXiv full text for harness reads.

Nothing is persisted to Postgres or object storage; ingest stays the only path
to a NOUS document. Extracted text lives in Redis for ``STALE_TTL_S`` only and
the downloaded PDF lives in a temporary directory for the duration of one call.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
import tempfile
import time
import weakref
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

TTL_S = 24 * 3600  # entry is fresh this long; after that a refetch is tried
STALE_TTL_S = 7 * 24 * 3600  # entry survives this long as a fallback
# Keeps one page under the 64 KiB gateway cap after JSON escaping.
MAX_PAGE_CHARS = 48_000
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$")
Fetch = Callable[[str], Awaitable[str]]


def _key(arxiv_id: str) -> str:
    return f"arxiv:fulltext:{arxiv_id}"


_PER_PROCESS_GAP_S = 3.0  # arXiv's minimum spacing, as in _make_async_request
_sleep = asyncio.sleep


def _executor_factory() -> Any:
    # gc.freeze: the worker's collector then never walks, so never dirties, the
    # copy-on-write pages a forked worker inherits from the API process.
    return ProcessPoolExecutor(max_workers=1, initializer=gc.freeze)


def _extract(pdf_path: str) -> str:
    """Sync, top-level (picklable) pypdf parse; same page framing as
    ``ArXivIngestionService.extract_pdf_content``. Runs in the worker process."""
    from pypdf import PdfReader

    full_text = ""
    for page_num, page in enumerate(PdfReader(pdf_path).pages):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            continue
        if page_text.strip():
            full_text += f"[Page {page_num + 1}]\n{page_text}\n\n"
    return full_text


_T = TypeVar("_T")
_MAX_CONCURRENT_PARSES = 2  # per process; each parse is a worker of its own
_parse_slots_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _parse_slots() -> asyncio.Semaphore:
    """The parse slots of the running loop, created on first use.

    A Python 3.11 semaphore binds to the first loop that waits on it, so a
    single module-level one would fail on any later loop (pytest runs one per
    test). Production runs one loop per process, so the cap is per process.
    """
    loop = asyncio.get_running_loop()
    slots = _parse_slots_by_loop.get(loop)
    if slots is None:
        slots = asyncio.Semaphore(_MAX_CONCURRENT_PARSES)
        _parse_slots_by_loop[loop] = slots
    return slots


def _kill_workers(executor: Any) -> None:
    """Kill a process pool's workers: ``shutdown`` never stops a running task.

    Python 3.14 has a public ``kill_workers``; before that CPython keeps the
    workers in ``_processes``. A thread pool (the tests' stand-in) has
    neither, so this is a no-op there.
    """
    kill_workers = getattr(executor, "kill_workers", None)
    if kill_workers is not None:
        kill_workers()
        return
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.kill()


async def _run_in_subprocess(fn: Callable[..., _T], *args: Any) -> _T:
    """Run ``fn(*args)`` in a worker process that belongs to this call alone.

    The worker is killed when the call fails, times out or is cancelled, so a
    stuck pypdf parse cannot outlive its request; a worker that dies breaks
    only its own call; and one caller's timeout never cancels another
    caller's parse. A shared single-worker pool did all three (audit RT-3).

    At most ``_MAX_CONCURRENT_PARSES`` (2) parses run at once in a process,
    so a burst of reads cannot start enough workers to exhaust the pod's
    memory. The arXiv rate gate is no bound here: it lets a download through
    once the queue wait passes 30 s, and spaces only per process when Redis
    fails. A caller waits for a slot inside its own budget; its timeout or
    cancellation ends only that wait.
    """
    async with _parse_slots():
        executor = _executor_factory()
        try:
            return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)
        except BaseException:
            _kill_workers(executor)
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


async def _extract_in_subprocess(pdf_path: str) -> str:
    """Parse ``pdf_path`` with pypdf in a worker process of its own."""
    return await _run_in_subprocess(_extract, pdf_path)


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
            if slot_wait is None:
                # No shared gate: mirror _make_async_request's per-process
                # spacing on the service's own class-level clock.
                elapsed = (
                    time.monotonic() - mod.ArXivIngestionService._last_request_time
                )
                if elapsed < _PER_PROCESS_GAP_S:
                    await _sleep(_PER_PROCESS_GAP_S - elapsed)
                mod.ArXivIngestionService._last_request_time = time.monotonic()
            elif slot_wait > 0:
                await _sleep(slot_wait)
            pdf = await svc.download_paper_pdf(arxiv_id)
            if not pdf:
                raise LookupError("arxiv_unavailable")
            pdf_path = Path(tmp) / "paper.pdf"
            pdf_path.write_bytes(pdf)
            return await _extract_in_subprocess(str(pdf_path))


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
    budget: float | None = None,
) -> dict[str, Any]:
    """One character page; ``budget`` bounds the (re)fetch only, so a stale
    cache entry is still served when a refresh times out."""
    if not _ARXIV_ID.match(arxiv_id):
        raise ValueError("invalid arxiv id")
    limit = max(1, min(int(limit), MAX_PAGE_CHARS))
    offset = max(0, int(offset))
    key = _key(arxiv_id)
    text, fetched_at = await _cached(redis, key)
    if text is None or time.time() - fetched_at > TTL_S:
        try:
            text = await asyncio.wait_for((fetch or fetch_text)(arxiv_id), budget)
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
