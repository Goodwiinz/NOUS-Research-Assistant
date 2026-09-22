"""
ArXiv Data Ingestion Service for RAG System

This service handles downloading, processing, and ingesting arXiv papers
for testing and evaluating the multimodal RAG system.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree.ElementTree import Element  # For type hints only

import aiofiles
import aiohttp
import httpx
from defusedxml import ElementTree as ET
from pypdf import PdfReader

from src.models.document import DocumentType, ProcessingStatus
from src.services.infrastructure.azure_openai_service import azure_openai_service
from src.shared.exceptions import ProcessingError, ValidationError
from src.shared.schemas import DocumentMetadata

# Create a simple IngestionError if not available
try:
    from src.shared.exceptions import IngestionError
except ImportError:

    class IngestionError(Exception):
        """Simple ingestion error"""

        pass


logger = logging.getLogger(__name__)

# --- Query fielding ----------------------------------------------------------
# Marks a query as already using arXiv search syntax: a field prefix (all:,
# ti:, cat:…), quoted phrase, grouping parens, or a boolean operator.
_ARXIV_QUERY_SYNTAX = re.compile(r'["():]|(?:^|\s)(?:AND|OR|ANDNOT)(?:\s|$)')


def field_arxiv_query(q: str) -> str:
    """Scope plain keyword queries to ``all:`` with AND between tokens.

    The arXiv API ORs unfielded space-separated terms: 'retrieval-augmented
    generation' matches ~21k papers in a 60-day window (anything containing
    'generation'), and under a submittedDate sort the caller then gets the
    newest submissions regardless of topic (observed: five sequential arXiv
    ids). Relevance sort masks this by ranking the phrase matches first.
    Queries already written in arXiv syntax pass through untouched.
    """
    if not q or _ARXIV_QUERY_SYNTAX.search(q):
        return q
    return " AND ".join(f"all:{tok}" for tok in q.split())


# --- Shared cross-pod arXiv rate gate ---------------------------------------
# arXiv rate-limits per SOURCE IP. Each pod honoring only its own 3s gate means
# N HPA replicas + the synthetic CronJob collectively exceed arXiv's window and
# 429 (LangSmith thread 301d6031: a first-attempt 429). This gate reserves the
# next request slot in Redis so ALL pods are serialized >= 3s apart against
# arXiv's per-IP limit. Redis-down / unset degrades to the per-process gate.
_ARXIV_RATE_KEY = "arxiv:rategate"
_ARXIV_MIN_INTERVAL_MS = 3000  # arXiv's minimum 3s between requests
# Deep enough for a real ingest batch to queue, bounded by the caller's budget.
# At 15000 only ~5 slots fit, so a 10-paper ingest (the tool's own cap) sent
# everything past request ~6 through with NO reservation — the gate became a
# burst generator at exactly the batch size it was meant to smooth. 30s queues
# the full batch while staying inside AGENT_LLM_TIMEOUT_SECONDS (50s); going
# higher would trade a 429 for a guaranteed tool-call timeout.
_ARXIV_MAX_WAIT_MS = 30000
_ARXIV_GATE_TTL_MS = 20000  # key self-expires so a quiet period resets the gate
# Atomically reserve the next slot: slot = max(now, last + interval); persist it
# and return how long THIS caller must wait. -1 => queue too deep, don't reserve.
_ARXIV_GATE_LUA = """
local last = tonumber(redis.call('GET', KEYS[1]) or '0')
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local maxwait = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local slot = math.max(now, last + interval)
local wait = slot - now
if wait > maxwait then
  return -1
end
redis.call('SET', KEYS[1], slot, 'PX', ttl)
return wait
"""
_arxiv_gate_redis: Any = None
_arxiv_gate_disabled = False


async def _acquire_arxiv_rate_slot() -> Optional[float]:
    """Reserve the next shared arXiv request slot across all pods.

    Returns the seconds to sleep before firing (0.0 = now), or ``None`` when the
    shared gate is unavailable (no REDIS_URL, or a transient Redis error) so the
    caller falls back to the per-process gate. Never raises.
    """
    global _arxiv_gate_redis, _arxiv_gate_disabled
    if _arxiv_gate_disabled:
        return None
    try:
        if _arxiv_gate_redis is None:
            from src.core.config import settings

            if not getattr(settings, "REDIS_URL", None):
                _arxiv_gate_disabled = True  # never going to work; stop trying
                return None
            import redis.asyncio as _redis_async

            _arxiv_gate_redis = _redis_async.from_url(
                settings.REDIS_URL, decode_responses=True
            )
        import time

        now_ms = int(time.time() * 1000)
        # NOTE: this is Redis' server-side Lua EVAL (not Python eval) — the
        # script is a module constant and every ARGV is an int, so there is no
        # code-injection surface.
        res = await _arxiv_gate_redis.eval(
            _ARXIV_GATE_LUA,
            1,
            _ARXIV_RATE_KEY,
            now_ms,
            _ARXIV_MIN_INTERVAL_MS,
            _ARXIV_MAX_WAIT_MS,
            _ARXIV_GATE_TTL_MS,
        )
        wait_ms = int(res)
        # -1 (queue deeper than the cap) => proceed now rather than pile up.
        # Deliberate, and pinned by test_deep_queue_proceeds_now: the caller
        # sits inside AGENT_LLM_TIMEOUT_SECONDS (50s), so waiting the cap would
        # spend the whole budget and time out — a worse failure than a 429,
        # which at least surfaces a clean "rate limited, try again" to the user.
        return 0.0 if wait_ms < 0 else wait_ms / 1000.0
    except Exception:
        # Transient Redis error: fall back to the per-process gate THIS call
        # (don't permanently disable — Redis may recover).
        logger.debug("arXiv shared rate gate unavailable; per-process gate only")
        return None


def _retry_after_seconds(headers: Any, *, default: float, cap: float = 15.0) -> float:
    """Seconds to wait from a Retry-After header, clamped to ``cap``.

    Accepts the delta-seconds form (arXiv sends that). A date-form or absent
    header falls back to ``default``. Clamped because the caller sits inside a
    tool-call timeout — honouring a literal 60s would just move the failure.
    """
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return default
    if not raw:
        return default
    try:
        return max(0.0, min(float(str(raw).strip()), cap))
    except (TypeError, ValueError):
        return default


# arXiv's API gate does not cover PDF fetches, and ingest_papers gathers a
# whole batch at once against a connector with limit=10 — so a 10-paper ingest
# opened ten simultaneous connections to arxiv.org/pdf. That is the least
# polite thing this service does, and it is invisible to the Redis slot gate.
#
# Deliberately a concurrency cap, NOT the 3s API gate: ten papers serialized at
# 3s is ~27s of pure waiting inside the 50s tool budget
# (AGENT_LLM_TIMEOUT_SECONDS), which would trade a burst for a guaranteed
# timeout — the same trade test_deep_queue_proceeds_now rejects for the API
# path. Three at a time removes the burst while a 10-paper batch still fits.
_MAX_CONCURRENT_PDF_DOWNLOADS = 3

# Audit R7-L11: the download was an uncapped response.read() against a URL
# built from a model-supplied id, with redirects followed — one bad id could
# pull an arbitrary-size body into memory. Real arXiv PDFs are well under this.
_MAX_PDF_BYTES = 50 * 1024 * 1024

_pdf_semaphore: Optional[asyncio.Semaphore] = None
_pdf_semaphore_loop: Any = None


def _pdf_download_semaphore() -> asyncio.Semaphore:
    """Per-event-loop semaphore for arxiv.org PDF fetches.

    Rebuilt when the running loop changes: a module-level Semaphore binds to
    the loop that created it, and this service runs under both uvicorn and
    Celery workers.
    """
    global _pdf_semaphore, _pdf_semaphore_loop

    loop = asyncio.get_running_loop()
    if _pdf_semaphore is None or _pdf_semaphore_loop is not loop:
        _pdf_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PDF_DOWNLOADS)
        _pdf_semaphore_loop = loop
    return _pdf_semaphore


def _parse_arxiv_date(value: Any) -> Optional[datetime]:
    """Parse an arXiv timestamp, or None when it cannot be parsed.

    ``published`` is absent or empty on two real paths: an arXiv API *error*
    entry (errors come back as an HTTP 200 Atom feed, not an HTTP error), and
    the metadata-miss stub the agent tool builds when a lookup was rate-limited
    (``tools_impl._tool_ingest_arxiv``). Both used to reach
    ``datetime.fromisoformat("")`` and kill the whole ingest with
    ``Invalid isoformat string: ''`` — losing the papers whose metadata was
    fine. A missing date should cost a field, not the run.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable arXiv date %r — storing None", value)
        return None


class ArXivIngestionService:
    """
    Service for ingesting arXiv papers into the RAG system

    Features:
    - Bulk metadata download from arXiv API
    - PDF fetching and processing
    - Category-based filtering
    - Progress tracking for large datasets
    - Automatic retry for failed downloads
    """

    ARXIV_API_BASE = "https://export.arxiv.org/api/query"
    ARXIV_PDF_BASE = "https://arxiv.org/pdf"
    MAX_RETRIES = 3
    BATCH_SIZE = 100

    # ArXiv category taxonomy
    CATEGORY_GROUPS = {
        "computer_science": ["cs.*"],
        "mathematics": ["math.*"],
        "physics": ["physics.*", "astro-ph.*", "cond-mat.*", "quant-ph.*"],
        "quantitative_biology": ["q-bio.*"],
        "quantitative_finance": ["q-fin.*"],
        "statistics": ["stat.*"],
        "electrical_engineering": ["eess.*"],
        "economics": ["econ.*"],
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.session = None
        self.download_dir = Path(self.config.get("arxiv_download_dir", "data/arxiv"))
        self.download_dir.mkdir(parents=True, exist_ok=True)

    # ArXiv requires >= 3 seconds between API requests.
    _last_request_time: float = 0.0

    async def _make_async_request(self, url: str, params: Dict) -> str:
        """Make asynchronous request using httpx with rate limiting and 429 retry"""
        # Enforce arXiv's minimum 3-second gap between requests. Prefer the
        # shared cross-pod gate; fall back to the per-process gate when Redis
        # is unavailable (same behavior as before this change).
        import time

        slot_wait = await _acquire_arxiv_rate_slot()
        if slot_wait is not None:
            if slot_wait > 0:
                await asyncio.sleep(slot_wait)
        else:
            now = time.monotonic()
            elapsed = now - ArXivIngestionService._last_request_time
            if elapsed < 3.0:
                await asyncio.sleep(3.0 - elapsed)
            ArXivIngestionService._last_request_time = time.monotonic()

        logger.info(f"Async request to: {url}")

        # Trace 019e1912 showed this loop's 10s/20s/30s/40s/50s 429 backoff
        # blow past the outer 30s tool-call timeout, putting the agent in a
        # cancelled state with no usable error. Fail fast on rate limit: at
        # most one short retry, surface clean IngestionError, let the agent
        # tell the user "rate-limited, try again shortly" instead of hanging.
        max_attempts = 2
        saw_rate_limit = False
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    response = await client.get(url, params=params, timeout=20.0)

                    if response.status_code == 429:
                        saw_rate_limit = True
                        if attempt == max_attempts - 1:
                            break
                        # arXiv sends Retry-After on 429 (its own message says
                        # "try again in 60 seconds"); retrying after a fixed 3s
                        # guaranteed a second 429. Honour the header, clamped so
                        # a large value cannot outlive the caller's timeout.
                        wait = _retry_after_seconds(response.headers, default=3.0)
                        logger.warning(
                            "ArXiv rate limited (429), attempt %d/%d, retrying in %ss...",
                            attempt + 1,
                            max_attempts,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        ArXivIngestionService._last_request_time = time.monotonic()
                        continue

                    response.raise_for_status()
                    logger.info(
                        "Response status: %d, length: %d",
                        response.status_code,
                        len(response.text),
                    )
                    return response.text

            except httpx.TimeoutException:
                logger.error("Request to arXiv API timed out after 20 seconds")
                if attempt == max_attempts - 1:
                    raise IngestionError("ArXiv API request timed out")
                await asyncio.sleep(2)
            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                logger.error("Error in _make_async_request: %s", e)
                if attempt == max_attempts - 1:
                    raise
                await asyncio.sleep(2)

        if saw_rate_limit:
            raise IngestionError(
                "ArXiv rate limited (HTTP 429). Try again in 60 seconds."
            )

        raise IngestionError("ArXiv API request failed after retries")

    async def __aenter__(self):
        """Async context manager entry"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300),
            connector=aiohttp.TCPConnector(limit=10),
            headers={"User-Agent": "ArXiv-Client/1.0"},
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()

    async def search_papers(
        self,
        query: str = "all:artificial intelligence",
        max_results: int = 100,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        categories: Optional[List[str]] = None,
        sort_by: str = "submittedDate",
        sort_order: str = "descending",
    ) -> List[Dict[str, Any]]:
        """
        Search for papers on arXiv using the API

        Args:
            query: Search query string (arXiv syntax)
            max_results: Maximum number of results to return
            date_from: Start date for filtering
            date_to: End date for filtering
            categories: List of categories to include
            sort_by: Field to sort by (submittedDate, lastUpdatedDate, relevance)
            sort_order: Sort order (ascending, descending)

        Returns:
            List of paper metadata dictionaries
        """
        if not self.session:
            logger.error(
                "Session not initialized in search_papers. Session object is None"
            )
            raise IngestionError("Session not initialized. Use async context manager.")

        # Build search query
        if categories:
            category_query = " OR ".join([f"cat:{cat}" for cat in categories])
            if query:
                query = f"({query}) AND ({category_query})"
            else:
                query = category_query

        # Prepare API parameters
        params = {
            "search_query": query,
            "start": 0,
            "max_results": min(max_results, 1000),  # ArXiv limit per request
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }

        logger.info(f"ArXiv API request params: {params}")
        logger.info(f"ArXiv API URL: {self.ARXIV_API_BASE}")

        papers = []
        api_offset = 0  # Tracks pagination offset for API calls
        empty_page_retries = 0  # arXiv sporadically returns empty mid-scan pages
        max_api_results = (
            max_results * 5
        )  # Limit total API fetches to prevent infinite loop

        while len(papers) < max_results and api_offset < max_api_results:
            params["start"] = api_offset
            params["max_results"] = min(self.BATCH_SIZE, max_results - len(papers))

            try:
                logger.info(
                    f"Making request to arXiv API... (offset={api_offset}, have {len(papers)} papers)"
                )

                # Use async httpx directly
                response_text = await self._make_async_request(
                    self.ARXIV_API_BASE, params
                )

                if not response_text:
                    raise IngestionError("Empty response from arXiv API")

                # Parse XML response
                try:
                    root = ET.fromstring(response_text)
                except ET.ParseError as e:
                    logger.error(f"Failed to parse XML: {e}")
                    logger.error(f"Response text: {response_text[:1000]}")
                    raise

                # Namespace handling
                namespaces = {
                    "atom": "http://www.w3.org/2005/Atom",
                    "arxiv": "http://arxiv.org/schemas/atom",
                }

                # Extract entries
                entries_in_batch = 0
                for entry in root.findall("atom:entry", namespaces):
                    entries_in_batch += 1
                    paper_data = self._parse_arxiv_entry(entry, namespaces)

                    # Apply date filtering if specified
                    if date_from or date_to:
                        pub_date = _parse_arxiv_date(paper_data.get("published"))
                        if pub_date is None:
                            # Undated entry cannot satisfy a date window;
                            # skip it rather than abort the whole scan.
                            continue
                        # Ensure pub_date is timezone-aware
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)

                        # Ensure date_from and date_to are timezone-aware
                        if date_from and date_from.tzinfo is None:
                            date_from = date_from.replace(tzinfo=timezone.utc)
                        if date_to and date_to.tzinfo is None:
                            date_to = date_to.replace(tzinfo=timezone.utc)

                        if date_from and pub_date < date_from:
                            continue
                        if date_to and pub_date > date_to:
                            continue

                    papers.append(paper_data)
                    if len(papers) >= max_results:
                        break

                # Update API offset for next batch
                api_offset += entries_in_batch

                # A non-empty page means we're past the flaky spot; a later
                # empty page at a different offset gets its own retry budget.
                if entries_in_batch > 0:
                    empty_page_retries = 0

                # Check if we got all available results from ArXiv
                total_available = None
                total_results_elem = root.find(
                    "opensearch:totalResults",
                    {"opensearch": "http://a9.com/-/spec/opensearch/1.1/"},
                )
                if total_results_elem is not None:
                    total_available = int(total_results_elem.text)
                    logger.info(
                        f"ArXiv reports {total_available} total results, fetched {api_offset} so far"
                    )
                    if api_offset >= total_available:
                        logger.info(f"Reached end of available results")
                        break

                if entries_in_batch == 0:
                    # arXiv sporadically returns an empty page mid-pagination
                    # even when totalResults says more remain (known server
                    # quirk). Retry the same offset once before concluding the
                    # scan is done, else results are silently truncated.
                    if (
                        total_available is not None
                        and api_offset < total_available
                        and empty_page_retries < 1
                    ):
                        empty_page_retries += 1
                        logger.warning(
                            "Empty page at offset %d with %d total available; "
                            "retrying once",
                            api_offset,
                            total_available,
                        )
                        continue
                    logger.info(f"No entries in batch, stopping")
                    break

            except Exception as e:
                logger.error(f"Error fetching arXiv papers: {e}")
                raise IngestionError(f"Failed to fetch papers from arXiv: {e}")

        logger.info(f"Fetched {len(papers)} papers from arXiv")
        return papers[:max_results]

    async def get_papers_by_ids(self, paper_ids: List[str]) -> List[Dict[str, Any]]:
        """Batch metadata lookup via the API's ``id_list`` parameter.

        One request (one shared rate-gate slot) for up to ~100 known IDs,
        instead of one search per ID. Invalid IDs come back as error entries
        in the same 200 Atom feed; those are dropped, so the result may be
        shorter than the input — callers match by ID, not position.
        """
        if not paper_ids:
            return []

        params = {
            "id_list": ",".join(paper_ids),
            "max_results": len(paper_ids),
        }
        response_text = await self._make_async_request(self.ARXIV_API_BASE, params)
        if not response_text:
            raise IngestionError("Empty response from arXiv API")
        try:
            root = ET.fromstring(response_text)
        except ET.ParseError as e:
            raise IngestionError(f"Failed to parse arXiv id_list response: {e}")

        namespaces = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        papers = [
            self._parse_arxiv_entry(entry, namespaces)
            for entry in root.findall("atom:entry", namespaces)
        ]
        # Error entries keep their full "http://arxiv.org/api/errors#..." URL
        # as the id (no "/abs/" segment) — filter them out here.
        return [p for p in papers if p["id"] and "://" not in p["id"]]

    def _parse_arxiv_entry(self, entry: Element, namespaces: Dict) -> Dict[str, Any]:
        """Parse a single arXiv entry from XML"""

        # Basic metadata
        # arXiv returns errors as an HTTP 200 Atom feed whose single entry has
        # none of these children, so a chained .find(...).text raises
        # AttributeError on None and takes the whole search down with it.
        def _text(tag: str, default: str = "") -> str:
            el = entry.find(tag, namespaces)
            return (el.text or default).strip() if el is not None else default

        raw_id = _text("atom:id")
        # Split on "/abs/", not "/": pre-2007 IDs are archive-qualified
        # ("math/0309136v1"), so taking the last path segment dropped the
        # "math/" prefix and produced a broken PDF URL + wrong dedup key.
        paper_id = raw_id.rsplit("/abs/", 1)[-1] if raw_id else ""
        title = _text("atom:title")
        abstract = _text("atom:summary")
        published = _text("atom:published")
        updated = _text("atom:updated")

        # Authors
        authors = []
        authors_detailed = []
        for author in entry.findall("atom:author", namespaces):
            name_el = author.find("atom:name", namespaces)
            name = (name_el.text or "").strip() if name_el is not None else ""
            if name:
                authors.append(name)

            affils = []
            # Try to find affiliation using arxiv namespace
            # Namespace definition implies 'arxiv' key is present in namespaces dict passed to this method
            # Usually passed from search_papers which defines it.
            for aff in author.findall("arxiv:affiliation", namespaces):
                affils.append(aff.text)

            authors_detailed.append({"name": name, "affiliations": affils})

        # Categories
        categories = []
        for category in entry.findall("atom:category", namespaces):
            cat = category.get("term")
            if cat:
                categories.append(cat)

        # Links (PDF, DOI, etc.)
        links = {}
        for link in entry.findall("atom:link", namespaces):
            link_title = link.get("title", "")
            href = link.get("href", "")
            if link_title == "pdf":
                links["pdf"] = href
            elif link_title == "doi":
                links["doi"] = href
            elif href.endswith(".pdf"):
                links["pdf"] = href

        # Comment (if available)
        comment_elem = entry.find("arxiv:comment", namespaces)
        comment = comment_elem.text if comment_elem is not None else None

        # Journal reference (if available)
        journal_ref_elem = entry.find("arxiv:journal_ref", namespaces)
        journal_ref = journal_ref_elem.text if journal_ref_elem is not None else None

        # DOI: the <arxiv:doi> element carries the bare DOI even when the feed
        # has no rel="doi" <link> — prefer the link, fall back to the element.
        if "doi" not in links:
            doi_elem = entry.find("arxiv:doi", namespaces)
            doi_text = (doi_elem.text or "").strip() if doi_elem is not None else ""
            if doi_text:
                links["doi"] = f"https://doi.org/{doi_text}"

        # Primary category has its own element; categories[0] is only a guess
        # (the Atom <category> order is not guaranteed to lead with primary).
        primary_elem = entry.find("arxiv:primary_category", namespaces)
        primary_category = (
            primary_elem.get("term")
            if primary_elem is not None and primary_elem.get("term")
            else (categories[0] if categories else None)
        )

        return {
            "id": paper_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "published": published,
            "updated": updated,
            "categories": categories,
            "links": links,
            "comment": comment,
            "journal_ref": journal_ref,
            "primary_category": primary_category,
            "authors_detailed": authors_detailed,
        }

    def _pdf_cache_path(self, paper_id: str) -> Path:
        # Old-style IDs ("math/0309136v1") contain a slash; flatten so the
        # cache stays a single directory.
        return self.download_dir / f"{paper_id.replace('/', '_')}.pdf"

    async def download_paper_pdf(
        self, paper_id: str, pdf_url: Optional[str] = None
    ) -> Optional[bytes]:
        """
        Download PDF for a specific paper

        Args:
            paper_id: arXiv paper ID
            pdf_url: Direct PDF URL (if known)

        Returns:
            PDF content as bytes
        """
        if not self.session:
            raise IngestionError("Session not initialized. Use async context manager.")

        if not pdf_url:
            pdf_url = f"{self.ARXIV_PDF_BASE}/{paper_id}.pdf"

        pdf_path = self._pdf_cache_path(paper_id)

        # Check if already downloaded
        if pdf_path.exists():
            logger.info(f"PDF already exists: {pdf_path}")
            async with aiofiles.open(pdf_path, "rb") as f:
                return await f.read()

        # Download with retry, capped so a whole batch cannot open one
        # connection per paper. Acquired around the network call only — the
        # cache hit above and the disk write below hold no slot.
        for attempt in range(self.MAX_RETRIES):
            try:
                async with (
                    _pdf_download_semaphore(),
                    self.session.get(pdf_url) as response,
                ):
                    response.raise_for_status()

                    # Bounded streaming read. A single .read(n) on a streamed
                    # response returns only what has arrived so far, so the
                    # cache used to get a truncated PDF; drain the stream and
                    # bail the moment the running total passes the cap.
                    chunks: List[bytes] = []
                    downloaded = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > _MAX_PDF_BYTES:
                            raise IngestionError(
                                f"PDF for {paper_id} exceeds "
                                f"{_MAX_PDF_BYTES} bytes; refusing to buffer it."
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)

                    async with aiofiles.open(pdf_path, "wb") as f:
                        await f.write(content)

                    logger.info(f"Downloaded PDF: {paper_id}")
                    return content

            except IngestionError:
                # Oversized/malformed is not transient — don't burn retries.
                raise
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {paper_id}: {e}")
                if attempt == self.MAX_RETRIES - 1:
                    raise IngestionError(f"Failed to download PDF for {paper_id}: {e}")
                await asyncio.sleep(2**attempt)  # Exponential backoff

        return None

    async def extract_pdf_content(self, pdf_content: bytes) -> Dict[str, Any]:
        """
        Extract text content from PDF

        Args:
            pdf_content: PDF file content as bytes

        Returns:
            Dictionary with extracted content
        """
        try:
            pdf_file = BytesIO(pdf_content)
            pdf_reader = PdfReader(pdf_file)

            # Extract text from all pages
            full_text = ""
            page_texts = []

            for page_num, page in enumerate(pdf_reader.pages):
                try:
                    page_text = page.extract_text()
                    page_texts.append({"page": page_num + 1, "text": page_text})
                    full_text += page_text + "\n"
                except Exception as e:
                    logger.warning(
                        f"Failed to extract text from page {page_num + 1}: {e}"
                    )

            # Extract metadata
            metadata = {}
            if pdf_reader.metadata:
                metadata = {
                    "title": pdf_reader.metadata.get("/Title", ""),
                    "author": pdf_reader.metadata.get("/Author", ""),
                    "subject": pdf_reader.metadata.get("/Subject", ""),
                    "creator": pdf_reader.metadata.get("/Creator", ""),
                    "producer": pdf_reader.metadata.get("/Producer", ""),
                    "creation_date": str(pdf_reader.metadata.get("/CreationDate", "")),
                    "modification_date": str(pdf_reader.metadata.get("/ModDate", "")),
                }

            return {
                "full_text": full_text,
                "page_texts": page_texts,
                "num_pages": len(pdf_reader.pages),
                "metadata": metadata,
            }

        except Exception as e:
            logger.error(f"Failed to extract PDF content: {e}")
            raise IngestionError(f"PDF extraction failed: {e}")

    async def ingest_papers(
        self,
        papers: List[Dict[str, Any]],
        download_pdfs: bool = True,
        extract_content: bool = True,
        batch_size: int = 10,
    ) -> List[Any]:
        """
        Ingest a list of papers into the RAG system

        Args:
            papers: List of paper metadata
            download_pdfs: Whether to download PDFs
            extract_content: Whether to extract full text from PDFs
            batch_size: Batch size for processing

        Returns:
            List of created Document objects
        """
        documents = []

        # Process in batches
        for i in range(0, len(papers), batch_size):
            batch = papers[i : i + batch_size]
            logger.info(
                f"Processing batch {i // batch_size + 1}/{(len(papers) - 1) // batch_size + 1}"
            )

            # Create tasks for batch
            tasks = []
            for paper in batch:
                task = self._ingest_single_paper(paper, download_pdfs, extract_content)
                tasks.append(task)

            # Wait for batch completion
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Failed to ingest paper {batch[j]['id']}: {result}")
                elif hasattr(result, "title"):  # Simple check for document-like objects
                    documents.append(result)

        logger.info(f"Successfully ingested {len(documents)}/{len(papers)} papers")
        return documents

    async def _ingest_single_paper(
        self, paper: Dict[str, Any], download_pdfs: bool, extract_content: bool
    ) -> Any:  # Return Any since we're creating a simple document object
        """Ingest a single paper"""
        paper_id = paper["id"]

        # Create document metadata dict
        metadata_dict = {
            "title": paper["title"],
            "description": (
                paper["abstract"][:500] + "..."
                if len(paper["abstract"]) > 500
                else paper["abstract"]
            ),
            "authors": paper["authors"],
            "publication_date": _parse_arxiv_date(paper.get("published")),
            "categories": paper["categories"],
            "tags": paper["categories"],  # Use categories as tags
            "arxiv_id": paper_id,
            "arxiv_categories": paper["categories"],
            "arxiv_primary_category": paper.get("primary_category"),
            "journal_reference": paper.get("journal_ref"),
            "doi": (paper.get("links") or {}).get("doi"),
            "comment": paper.get("comment"),
            "source": "arxiv",
            "language": "en",
            "modality": "text",
            "content_type": "academic_paper",
        }

        # Prepare content
        content_parts = []

        # Add abstract
        content_parts.append(f"# Abstract\n\n{paper['abstract']}")

        # Add authors
        if paper["authors"]:
            content_parts.append(f"\n# Authors\n\n{', '.join(paper['authors'])}")

        # Add categories
        if paper["categories"]:
            content_parts.append(f"\n# Categories\n\n{', '.join(paper['categories'])}")

        # Download and extract PDF content if requested. Track whether full
        # text was actually obtained so the persisted metadata is honest: a
        # PDF fetch/extract failure still yields a COMPLETED abstract-only
        # document (legit graceful degradation), but downstream consumers must
        # be able to tell it apart from a full-text ingest — otherwise the
        # agent may narrate "I read the paper" over an abstract-only doc.
        has_full_text = False
        if download_pdfs and paper["links"].get("pdf"):
            try:
                pdf_content = await self.download_paper_pdf(
                    paper_id, paper["links"]["pdf"]
                )

                if extract_content and pdf_content:
                    extracted = await self.extract_pdf_content(pdf_content)

                    # Add first page or excerpt
                    if extracted["full_text"]:
                        preview = extracted["full_text"][:2000]
                        content_parts.append(f"\n# Content Preview\n\n{preview}...")
                        has_full_text = True

                    # Update metadata with PDF info
                    metadata_dict["num_pages"] = extracted.get("num_pages")
                    metadata_dict["pdf_path"] = str(self._pdf_cache_path(paper_id))

            except Exception as e:
                logger.warning(f"Failed to process PDF for {paper_id}: {e}")
                metadata_dict["pdf_extraction_failed"] = True

        # Record full-text availability for every path (full-text success,
        # PDF failure, or metadata-only ingest with download_pdfs=False).
        metadata_dict["has_full_text"] = has_full_text

        # Combine all content
        full_content = "\n".join(content_parts)

        # Create document data dictionary (will be converted to Document model by the service layer)
        document_data = {
            "title": metadata_dict["title"],
            "content_text": full_content,
            "document_metadata": metadata_dict,
            "processing_status": ProcessingStatus.COMPLETED,
            "document_type": (
                "PDF" if download_pdfs and paper["links"].get("pdf") else "TEXT"
            ),
            "filename": f"{paper_id}.pdf",
            "mime_type": (
                "application/pdf"
                if download_pdfs and paper["links"].get("pdf")
                else "text/plain"
            ),
            "file_size_bytes": (
                len(pdf_content) if "pdf_content" in locals() and pdf_content else 0
            ),
        }

        # Return a simple object with the required attributes
        class SimpleDocument:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)
                if hasattr(self, "document_metadata"):
                    self.metadata = self.document_metadata
                else:
                    self.metadata = {}
                self.status = self.processing_status

        # Set correct document type enum value
        if "PDF" in document_data["document_type"]:
            document_data["document_type"] = DocumentType.PDF
        else:
            document_data["document_type"] = DocumentType.TEXT

        return SimpleDocument(**document_data)

    async def create_evaluation_dataset(
        self,
        papers: List[Dict[str, Any]],
        num_questions: int = 5,
        difficulty_levels: List[str] = ["easy", "medium", "hard"],
    ) -> Dict[str, Any]:
        """
        Create an evaluation dataset from arXiv papers

        Args:
            papers: List of paper metadata
            num_questions: Number of questions to generate per paper
            difficulty_levels: List of difficulty levels to sample from

        Returns:
            Evaluation dataset dictionary
        """
        import random

        evaluation_data = {
            "dataset_info": {
                "name": "ArXiv RAG Evaluation Dataset",
                "created_at": datetime.now().isoformat(),
                "num_papers": len(papers),
                "questions_per_paper": num_questions,
            },
            "test_cases": [],
        }

        # Sample papers (limit to reasonable number for demo)
        sample_papers = random.sample(papers, min(50, len(papers)))

        for paper in sample_papers:
            # Generate questions based on paper content
            questions = await self._generate_questions_for_paper(paper, num_questions)

            test_case = {
                "paper_id": paper["id"],
                "paper_title": paper["title"],
                "paper_abstract": paper["abstract"],
                "categories": paper["categories"],
                "questions": questions,
                "ground_truth_context": {
                    "title": paper["title"],
                    "abstract": paper["abstract"],
                    "authors": paper["authors"],
                    "categories": paper["categories"],
                },
            }

            evaluation_data["test_cases"].append(test_case)

        # Save evaluation dataset
        eval_path = self.download_dir / "evaluation_dataset.json"
        async with aiofiles.open(eval_path, "w") as f:
            await f.write(json.dumps(evaluation_data, indent=2))

        logger.info(
            f"Created evaluation dataset with {len(evaluation_data['test_cases'])} test cases"
        )
        return evaluation_data

    async def _generate_questions_for_paper(
        self, paper: Dict[str, Any], num_questions: int
    ) -> List[Dict[str, Any]]:
        """
        Generate questions for a specific paper using Azure OpenAI
        """
        questions = []

        # Try to use LLM first
        if azure_openai_service.is_chat_available():
            try:
                system_prompt = """
                You are an expert research evaluator. Generate evaluation questions for a research paper based on its title and abstract.
                Return a JSON object with a key 'questions' containing a list of objects.
                Each object must have:
                - question: The text of the question
                - difficulty: 'easy', 'medium', or 'hard'
                - expected_answer_type: One of 'contribution', 'methodology', 'problem_statement', 'results', 'comparison'
                - answer_relevancy_score: 0.0 to 1.0 (target score for evaluation)
                """

                user_content = f"""
                Title: {paper["title"]}
                Abstract: {paper["abstract"]}
                Categories: {", ".join(paper["categories"])}
                
                Generate {num_questions} questions for this paper.
                """

                response = await azure_openai_service.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.7,
                    max_tokens=2000,
                )

                content = response.get("content", "")
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()

                data = json.loads(content)
                llm_questions = data.get("questions", [])

                for i, q in enumerate(llm_questions):
                    question = {
                        "id": f"{paper['id']}_q_{i + 1}",
                        "question": q["question"],
                        "difficulty": q["difficulty"],
                        "expected_answer_type": q["expected_answer_type"],
                        "requires_context": ["title", "abstract", "categories"],
                        "evaluation_criteria": {
                            "answer_relevancy": q.get("answer_relevancy_score", 0.8),
                            "faithfulness": 0.8,
                            "contextual_precision": 0.7,
                        },
                        "source": "llm_generated",
                    }
                    questions.append(question)

                if questions:
                    logger.info(
                        f"Generated {len(questions)} questions using LLM for paper {paper['id']}"
                    )
                    return questions

            except Exception as e:
                logger.warning(f"Failed to generate questions using LLM: {e}")
                # Fallback to templates below

        # Fallback Template questions based on paper content
        templates = [
            {
                "question": f"What is the main contribution of '{paper['title']}'?",
                "difficulty": "medium",
                "expected_answer_type": "contribution",
            },
            {
                "question": f"Which methods are used in this paper about {paper['primary_category']}?",
                "difficulty": "easy",
                "expected_answer_type": "methodology",
            },
            {
                "question": f"Who are the authors of '{paper['title']}' and what is their main research area?",
                "difficulty": "easy",
                "expected_answer_type": "author_info",
            },
            {
                "question": f"What problem does '{paper['title']}' address in the field of {paper['primary_category']}?",
                "difficulty": "medium",
                "expected_answer_type": "problem_statement",
            },
            {
                "question": f"How does this paper compare to previous work in {paper['categories'][0] if paper['categories'] else 'the field'}?",
                "difficulty": "hard",
                "expected_answer_type": "comparison",
            },
        ]

        # Select random templates
        import random

        selected_templates = random.sample(
            templates, min(num_questions, len(templates))
        )

        for i, template in enumerate(selected_templates):
            question = {
                "id": f"{paper['id']}_q_{i + 1}",
                "question": template["question"],
                "difficulty": template["difficulty"],
                "expected_answer_type": template["expected_answer_type"],
                "requires_context": ["title", "abstract", "categories"],
                "evaluation_criteria": {
                    "answer_relevancy": 0.7,
                    "faithfulness": 0.8,
                    "contextual_precision": 0.6,
                },
                "source": "template_fallback",
            }
            questions.append(question)

        return questions

    def get_category_statistics(self, papers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Get statistics about paper categories

        Args:
            papers: List of paper metadata

        Returns:
            Dictionary with category statistics
        """
        category_counts = {}
        group_counts = {group: 0 for group in self.CATEGORY_GROUPS.keys()}

        for paper in papers:
            for category in paper["categories"]:
                category_counts[category] = category_counts.get(category, 0) + 1

            # Group by main category groups
            for group, patterns in self.CATEGORY_GROUPS.items():
                if any(
                    cat.split(".")[0] in pattern.replace("*", "")
                    for pattern in patterns
                    for cat in paper["categories"]
                ):
                    group_counts[group] += 1

        # Sort categories by count
        sorted_categories = sorted(
            category_counts.items(), key=lambda x: x[1], reverse=True
        )

        return {
            "total_papers": len(papers),
            "unique_categories": len(category_counts),
            "top_categories": sorted_categories[:20],
            "category_distribution": category_counts,
            "group_distribution": group_counts,
        }
