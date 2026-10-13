"""``discover_papers``: one deterministic discovery round for a harness (GOO-435, S5c).

The harness (Claude Code, Codex) runs the loop; this module is the server half,
a single round. It runs every search it needs at once, merges what comes back
and returns a short ranked list for the model to read:

1. library keyword: PostgreSQL full-text search over the grant's scope;
2. library semantic: pgvector nearest neighbours of the question (``unavailable``
   when ``SEMANTIC_SEARCH_ENABLED`` is off, the embedding provider fails, or
   ``document_embeddings`` was never created);
3. arXiv keyword: the same ``search_arxiv`` implementation the read tool uses;
4. acronym recovery: when the keywords mix acronyms with other words, one more
   keyword call (library and arXiv) with only the acronyms, because an
   all-terms-must-match query otherwise misses papers that use the acronym
   alone.

Every call carries the same publication window, and it is applied where the
data is read: as a predicate of the SQL statement for the library, as a
``submittedDate`` filter inside the arXiv query. A window applied to a page
that was already fetched returns nothing for "papers from 2020" when the page
holds recent papers (alphaXiv's behaviour; see
``docs/plans/2026-10-12-alphaxiv-discover-papers-reference.md``).

Calls run concurrently, each with its own timeout, and one failing call never
sinks the others: it is recorded in ``calls`` with a status. The library calls
each open their own session (an ``AsyncSession`` is not safe for concurrent
use); the caller's session is only used to resolve the grant's scope.

Merging. Hits are deduplicated by exact id, then by arXiv id or DOI, then by
normalized title; an arXiv paper that is already in the library is shown as the
library copy. Calls are fused with reciprocal-rank fusion (k = 60). A paper
that several independent searches found (library keyword, library semantic,
arXiv) is scaled up by ``AGREEMENT_BONUS`` per extra search, the usual
meta-search consensus bonus: a semantic search always returns its nearest
neighbours whatever their distance, so without it nothing could ever dominate
them. ``prioritize`` then nudges the score by publication age (and a citation
count when the document metadata has one); it reorders, it never excludes.
Items scoring below 0.3 x the best are dropped and at most 15 are returned, so
a paper that all the searches agree on comes back alone. When only two
channels answer (``sources=["library"]``, or one channel failed), the cut rises
so that a paper only one of them found still falls below it (``cut_ratio``). Every returned id
comes from a call that succeeded in this round (the citation gate;
``_cite_gate`` enforces it).

``difficulty`` does not trigger server-side follow-ups. It sets how deep each
call reads (10, 20 or 30 results, arXiv included) and is echoed back as
``followup_rounds_allowed`` for the harness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.document import Document
from src.services.agent._pii_redact import redact_pii
from src.services.agent.tool_session import tool_session
from src.services.search.keyword_search_service import keyword_search
from src.services.search.semantic_search_service import (
    SemanticSearchUnavailable,
    project_scope_condition,
    semantic_search_strict,
)

logger = logging.getLogger(__name__)

MAX_KEYWORDS = 8
MAX_KEYWORD_CHARS = 100
MAX_QUESTION_CHARS = 500
MAX_PAPERS = 15
RELATIVE_CUT = 0.3
RRF_K = 60
TITLE_CHARS = 300
SNIPPET_CHARS = 300
MAX_AUTHORS = 5
AUTHOR_CHARS = 100
# How far ``prioritize`` may move a score: a paper of the favoured age scores
# at most this much more than an otherwise identical one. About ten places in
# one result list, so a nudge rather than a re-sort.
PRIORITY_WEIGHT = 0.15
# The deepest a call reads (difficulty 8-10). The public search_arxiv tool stays
# at 5 papers; the caller of ``discover`` passes ``_tool_search_arxiv`` a cap of
# this size instead (read_tools._discover_papers).
MAX_DEPTH = 30
# Each search (channel) beyond the first that returned a paper scales its fused
# score by this much: 3 channels = x1.5, 2 = x1.25. Calls on the same channel
# (a keyword call and its acronym-only twin) are one vote, not two.
AGREEMENT_BONUS = 0.25
# How far above a lone channel's best possible share the cut sits when fewer
# than three channels answered (see ``cut_ratio``).
LONE_HIT_MARGIN = 1.15
# Per-call budgets. arXiv waits on a shared rate gate (3 s apart), so it gets
# the longest; the whole round is bounded by the slowest call.
LIBRARY_TIMEOUT_SECONDS = 20.0
SEMANTIC_TIMEOUT_SECONDS = 25.0
ARXIV_TIMEOUT_SECONDS = 45.0

Prioritize = Literal["default", "recency", "historical"]
Source = Literal["library", "arxiv"]
CallStatus = Literal["ok", "empty", "unavailable", "error"]
PRIORITIES: tuple[str, ...] = ("default", "recency", "historical")
SOURCES: tuple[str, ...] = ("library", "arxiv")
_SOURCE_ORDER: tuple[Source, ...] = ("library", "arxiv")

ArxivSearch = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class DiscoverArgumentError(ValueError):
    """Arguments rejected before any search runs; the message is safe to return."""


class ChannelUnavailable(Exception):
    """A channel that cannot run here (not an error): ``reason`` is a stable token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ChannelFailed(Exception):
    """A channel whose upstream answered with an error payload."""


# --- arguments ---------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverRequest:
    keywords: tuple[str, ...]
    question: str
    difficulty: int
    prioritize: Prioritize = "default"
    published_after: date | None = None
    published_before: date | None = None
    sources: tuple[Source, ...] = ("library", "arxiv")

    @property
    def depth(self) -> int:
        """Results each call reads: 10, 20 or 30."""
        return 10 if self.difficulty <= 3 else 20 if self.difficulty <= 7 else MAX_DEPTH

    @property
    def followup_rounds_allowed(self) -> int:
        """Follow-up calls the harness may spend: 0, 1 or 2."""
        return 0 if self.difficulty <= 3 else 1 if self.difficulty <= 7 else 2


def _clean(value: str) -> str:
    printable = "".join(ch for ch in value if ch.isprintable() or ch.isspace())
    return " ".join(printable.split())


def _text(arguments: dict[str, Any], name: str, limit: int) -> str:
    value = arguments.get(name)
    cleaned = _clean(value) if isinstance(value, str) else ""
    if not 1 <= len(cleaned) <= limit:
        raise DiscoverArgumentError(f"{name} must be 1-{limit} characters")
    return cleaned


def _day(arguments: dict[str, Any], name: str) -> date | None:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise DiscoverArgumentError(f"{name} must be a date like 2024-01-31")


def parse_arguments(arguments: dict[str, Any]) -> DiscoverRequest:
    """Validate the model's arguments; raises ``DiscoverArgumentError``."""
    raw_keywords = arguments.get("keywords")
    if (
        not isinstance(raw_keywords, list)
        or not 1 <= len(raw_keywords) <= MAX_KEYWORDS
        or not all(isinstance(item, str) for item in raw_keywords)
    ):
        raise DiscoverArgumentError(f"keywords must be 1-{MAX_KEYWORDS} strings")
    keywords: list[str] = []
    for item in raw_keywords:
        keyword = _clean(item)
        if not 1 <= len(keyword) <= MAX_KEYWORD_CHARS:
            raise DiscoverArgumentError(
                f"each keyword must be 1-{MAX_KEYWORD_CHARS} characters"
            )
        if keyword not in keywords:
            keywords.append(keyword)
    question = _text(arguments, "question", MAX_QUESTION_CHARS)
    difficulty = arguments.get("difficulty")
    if isinstance(difficulty, bool) or not isinstance(difficulty, int):
        raise DiscoverArgumentError("difficulty must be an integer from 1 to 10")
    if not 1 <= difficulty <= 10:
        raise DiscoverArgumentError("difficulty must be an integer from 1 to 10")
    prioritize = arguments.get("prioritize", "default")
    if prioritize not in PRIORITIES:
        raise DiscoverArgumentError("prioritize must be default, recency or historical")
    after = _day(arguments, "published_after")
    before = _day(arguments, "published_before")
    if after is not None and before is not None and after > before:
        raise DiscoverArgumentError(
            "published_after must not be later than published_before"
        )
    raw_sources = arguments.get("sources", list(SOURCES))
    if (
        not isinstance(raw_sources, list)
        or not 1 <= len(raw_sources) <= len(SOURCES)
        or any(item not in SOURCES for item in raw_sources)
    ):
        raise DiscoverArgumentError("sources must list library and/or arxiv")
    # Fixed order, duplicates collapsed.
    sources = tuple(source for source in _SOURCE_ORDER if source in raw_sources)
    return DiscoverRequest(
        keywords=tuple(keywords),
        question=question,
        difficulty=difficulty,
        prioritize=prioritize,
        published_after=after,
        published_before=before,
        sources=sources,
    )


# --- acronym recovery ----------------------------------------------------------

_TOKEN_EDGES = "\"'`()[]{}<>,.;:!?"
_ACRONYM_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,9}")


def _is_acronym(token: str) -> bool:
    """2-10 characters, a letter first, letters/digits/hyphens, >= 2 capitals."""
    return (
        _ACRONYM_SHAPE.fullmatch(token) is not None
        and sum(char.isupper() for char in token) >= 2
    )


def acronym_terms(keywords: Sequence[str]) -> list[str]:
    """The acronyms of ``keywords``, when other words are searched with them.

    An all-terms-must-match query for ``["GRPO", "reward models"]`` misses a
    paper about GRPO that never says "reward models", so the round also searches
    ``GRPO`` alone. With nothing but acronyms the main query already is that
    search, so nothing is returned.
    """
    acronyms: list[str] = []
    others = 0
    for keyword in keywords:
        for raw in keyword.split():
            token = raw.strip(_TOKEN_EDGES)
            if not token:
                continue
            if _is_acronym(token):
                if token not in acronyms:
                    acronyms.append(token)
            else:
                others += 1
    return acronyms if acronyms and others else []


# --- candidates ------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One paper as one call returned it."""

    id: str
    source: Source
    title: str
    published: date | None = None
    date_source: str | None = None
    authors: tuple[str, ...] = ()
    snippet: str = ""
    arxiv_id: str | None = None
    doi: str | None = None
    citations: int | None = None


@dataclass
class CallRecord:
    name: str
    query: str
    status: CallStatus
    candidates: list[Candidate] = field(default_factory=list)
    reason: str | None = None

    @property
    def channel(self) -> str:
        """The search this call belongs to: an acronym call is its main call's twin."""
        return self.name.removesuffix("_acronyms")

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": self.name,
            "query": self.query,
            "status": self.status,
            "count": len(self.candidates),
        }
        if self.reason:
            entry["reason"] = self.reason
        return entry


def _arxiv_key(value: str | None) -> str | None:
    """An arXiv id without its version or ``arXiv:`` prefix, lower-cased."""
    if not value:
        return None
    key = re.sub(
        r"^(?:arxiv:|https?://arxiv\.org/(?:abs|pdf)/)", "", value.strip(), flags=re.I
    )
    key = re.sub(r"(?:\.pdf)?(?:v\d+)?$", "", key, flags=re.I).strip().lower()
    return key or None


def _doi_key(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(
        r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", value.strip(), flags=re.I
    )
    key = key.strip().lower()
    return key or None


def _title_key(title: str) -> str | None:
    folded = unicodedata.normalize("NFKD", title).casefold()
    words = re.findall(
        r"[^\W_]+", "".join(c for c in folded if not unicodedata.combining(c))
    )
    key = " ".join(words)
    # A short title ("Introduction") says nothing about identity.
    return key if len(words) >= 2 and len(key) >= 16 else None


def _iso_day(value: Any) -> date | None:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _shorten(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _author_list(raw: Any) -> tuple[str, ...]:
    """Up to ``MAX_AUTHORS`` names from what ingest persisted (strings or objects)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    names: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and _clean(name):
            names.append(_shorten(_clean(name), AUTHOR_CHARS))
        if len(names) == MAX_AUTHORS:
            break
    return tuple(names)


def _count(raw: Any) -> int | None:
    try:
        number = int(str(raw))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


# --- channels ----------------------------------------------------------------------

_unprovisioned_logged = False
_MISSING_OBJECT_STATES = frozenset({"42P01", "42704"})  # undefined table / type


def _sqlstate(error: DBAPIError) -> str | None:
    orig = error.orig
    for holder in (orig, getattr(orig, "__cause__", None)):
        for attribute in ("sqlstate", "pgcode"):
            code = getattr(holder, attribute, None)
            if isinstance(code, str):
                return code
    return None


def _embeddings_table_missing(error: DBAPIError) -> bool:
    """``document_embeddings`` (or the ``vector`` type) does not exist."""
    state = _sqlstate(error)
    if state is not None:
        return state in _MISSING_OBJECT_STATES
    message = str(error.orig).lower()
    return "does not exist" in message and (
        "document_embeddings" in message or "vector" in message
    )


async def _library_candidates(
    session: AsyncSession,
    hits: Sequence[Any],
    *,
    organization_id: uuid.UUID,
    project_ids: Sequence[uuid.UUID],
) -> list[Candidate]:
    """Library hits as candidates, with the identity fields search does not return.

    One extra query by id adds authors, arXiv id, DOI and citation count. It
    repeats the organization and scope predicates, so a hit that left the scope
    (or was deleted) since the search ran is dropped rather than shown.
    """
    if not hits:
        return []
    ids = [uuid.UUID(hit.document_id) for hit in hits]
    metadata = Document.document_metadata
    rows = await session.execute(
        select(
            Document.id,
            Document.arxiv_id,
            metadata["arxiv_id"].as_string(),
            metadata["doi"].as_string(),
            metadata["authors"],
            metadata["citation_count"].as_string(),
        ).where(
            Document.id.in_(ids),
            Document.organization_id == organization_id,
            Document.is_deleted.is_(False),
            project_scope_condition(set(project_ids)),
        )
    )
    identity = {str(row[0]): row for row in rows.all()}
    candidates: list[Candidate] = []
    for hit in hits:
        row = identity.get(hit.document_id)
        if row is None:
            continue
        candidates.append(
            Candidate(
                id=hit.document_id,
                source="library",
                title=_shorten(redact_pii(hit.title or ""), TITLE_CHARS),
                published=hit.publication_date,
                date_source=hit.publication_date_source,
                authors=_author_list(row[4]),
                snippet=_shorten(redact_pii(hit.snippet or ""), SNIPPET_CHARS),
                arxiv_id=_arxiv_key(row[1] or row[2]),
                doi=_doi_key(row[3]),
                citations=_count(row[5]),
            )
        )
    return candidates


async def _library_keyword(
    *,
    query: str,
    organization_id: uuid.UUID,
    project_ids: Sequence[uuid.UUID],
    after: date | None,
    before: date | None,
    limit: int,
) -> list[Candidate]:
    async with tool_session() as session:
        hits = await keyword_search(
            session,
            query=query,
            organization_id=organization_id,
            project_ids=project_ids,
            published_after=after,
            published_before=before,
            limit=limit,
        )
        return await _library_candidates(
            session, hits, organization_id=organization_id, project_ids=project_ids
        )


async def _library_semantic(
    *,
    query: str,
    organization_id: uuid.UUID,
    project_ids: Sequence[uuid.UUID],
    after: date | None,
    before: date | None,
    limit: int,
) -> list[Candidate]:
    global _unprovisioned_logged
    if not settings.SEMANTIC_SEARCH_ENABLED:
        raise ChannelUnavailable("disabled")
    async with tool_session() as session:
        try:
            # A SAVEPOINT: a missing table aborts only the savepoint, so the
            # session (and the identity query below) stays usable.
            async with session.begin_nested():
                hits = await semantic_search_strict(
                    session,
                    query=query,
                    organization_id=organization_id,
                    project_ids=project_ids,
                    published_after=after,
                    published_before=before,
                    limit=limit,
                )
        except SemanticSearchUnavailable as unavailable:
            # The provider failed or answered with an unusable vector: the
            # channel cannot answer, which is not the same as no hits.
            raise ChannelUnavailable(unavailable.reason) from unavailable
        except DBAPIError as error:
            if not _embeddings_table_missing(error):
                raise
            # The jd01 migration skips itself when pgvector is not installed,
            # so a deployment can enable the flag and still have no table.
            if not _unprovisioned_logged:
                _unprovisioned_logged = True
                logger.warning(
                    "SEMANTIC_SEARCH_ENABLED is on but document_embeddings does "
                    "not exist; discover_papers skips the semantic call. "
                    "Install pgvector and re-run migration jd01_document_embeddings."
                )
            raise ChannelUnavailable("not_provisioned") from error
        return await _library_candidates(
            session, hits, organization_id=organization_id, project_ids=project_ids
        )


_ARXIV_SYNTAX = re.compile(r"[\"():\[\]]")
_ARXIV_OPERATORS = frozenset({"AND", "OR", "ANDNOT"})


def _arxiv_terms(query: str) -> str:
    """Plain keywords only: no field prefix, quote, group or boolean operator.

    ``search_arxiv`` passes a query that already uses arXiv syntax through
    untouched. A model-supplied ``) OR (`` must not be able to step outside the
    publication window the round composes around the query.
    """
    return " ".join(
        token
        for token in _ARXIV_SYNTAX.sub(" ", query).split()
        if token not in _ARXIV_OPERATORS
    )


async def _arxiv_keyword(
    *,
    arxiv_search: ArxivSearch,
    query: str,
    after: date | None,
    before: date | None,
    limit: int,
) -> list[Candidate]:
    terms = _arxiv_terms(query)
    if not terms:
        return []
    arguments: dict[str, Any] = {
        "query": terms,
        "max_results": limit,
        # Without a window the tool defaults to the last 12 months; 0 = unbounded.
        "recency_days": 0,
    }
    if after is not None:
        arguments["published_after"] = after.isoformat()
    if before is not None:
        arguments["published_before"] = before.isoformat()
    payload = await arxiv_search(arguments)
    if payload.get("error") or payload.get("is_error"):
        logger.warning("discover_papers arxiv error payload: %s", payload.get("error"))
        raise ChannelFailed("arxiv returned an error")
    candidates: list[Candidate] = []
    for paper in payload.get("papers") or []:
        identifier = str(paper.get("id") or "").strip()
        if not identifier:
            continue
        published = _iso_day(paper.get("published"))
        candidates.append(
            Candidate(
                id=identifier,
                source="arxiv",
                title=_shorten(str(paper.get("title") or ""), TITLE_CHARS),
                published=published,
                date_source="arxiv" if published else None,
                authors=_author_list(paper.get("authors")),
                snippet=_shorten(str(paper.get("abstract") or ""), SNIPPET_CHARS),
                arxiv_id=_arxiv_key(identifier),
            )
        )
    return candidates


# --- one round ------------------------------------------------------------------------


@dataclass(frozen=True)
class _CallSpec:
    name: str
    query: str
    timeout: float
    run: Callable[[], Awaitable[list[Candidate]]]


async def _run_call(spec: _CallSpec) -> CallRecord:
    """One call, never raising: a failure is a status, not a failed round."""
    try:
        candidates = await asyncio.wait_for(spec.run(), timeout=spec.timeout)
    except ChannelUnavailable as unavailable:
        return CallRecord(
            spec.name, spec.query, "unavailable", reason=unavailable.reason
        )
    except asyncio.TimeoutError:
        logger.warning("discover_papers %s timed out", spec.name)
        return CallRecord(spec.name, spec.query, "error", reason="timeout")
    except Exception:  # noqa: BLE001 - one channel must not sink the round
        logger.warning("discover_papers %s failed", spec.name, exc_info=True)
        return CallRecord(spec.name, spec.query, "error", reason="upstream_error")
    return CallRecord(
        spec.name, spec.query, "ok" if candidates else "empty", candidates=candidates
    )


def _plan(
    request: DiscoverRequest,
    *,
    organization_id: uuid.UUID,
    project_ids: Sequence[uuid.UUID],
    arxiv_search: ArxivSearch,
) -> list[_CallSpec]:
    """The calls of one round, in the order their results are fused."""
    window: dict[str, Any] = {
        "after": request.published_after,
        "before": request.published_before,
        "limit": request.depth,
    }
    library: dict[str, Any] = {
        "organization_id": organization_id,
        "project_ids": project_ids,
        **window,
    }
    arxiv: dict[str, Any] = {"arxiv_search": arxiv_search, **window}

    def library_keyword(name: str, query: str) -> _CallSpec:
        run = partial(_library_keyword, query=query, **library)
        return _CallSpec(name, query, LIBRARY_TIMEOUT_SECONDS, run)

    def arxiv_keyword(name: str, query: str) -> _CallSpec:
        run = partial(_arxiv_keyword, query=query, **arxiv)
        return _CallSpec(name, query, ARXIV_TIMEOUT_SECONDS, run)

    use_library = "library" in request.sources
    use_arxiv = "arxiv" in request.sources
    keywords = " ".join(request.keywords)
    specs: list[_CallSpec] = []
    if use_library:
        specs.append(library_keyword("library_keyword", keywords))
        specs.append(
            _CallSpec(
                "library_semantic",
                request.question,
                SEMANTIC_TIMEOUT_SECONDS,
                partial(_library_semantic, query=request.question, **library),
            )
        )
    if use_arxiv:
        specs.append(arxiv_keyword("arxiv_keyword", keywords))
    acronyms = " ".join(acronym_terms(request.keywords))
    if acronyms:
        if use_library:
            specs.append(library_keyword("library_keyword_acronyms", acronyms))
        if use_arxiv:
            specs.append(arxiv_keyword("arxiv_keyword_acronyms", acronyms))
    return specs


# --- merge and rank --------------------------------------------------------------------


@dataclass
class _Cluster:
    members: list[Candidate] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)  # call -> best rank
    order: int = 0  # first appearance, for a stable tie-break


@dataclass(frozen=True)
class RankedPaper:
    paper: Candidate
    score: float
    found_by: tuple[str, ...]


def _identity_keys(candidate: Candidate) -> list[tuple[str, str]]:
    keys = [("id", f"{candidate.source}:{candidate.id}")]
    arxiv_id = _arxiv_key(candidate.arxiv_id)
    if arxiv_id:
        keys.append(("arxiv", arxiv_id))
    doi = _doi_key(candidate.doi)
    if doi:
        keys.append(("doi", doi))
    title = _title_key(candidate.title)
    if title:
        keys.append(("title", title))
    return keys


def _cluster(records: Sequence[CallRecord]) -> list[_Cluster]:
    """Group candidates that are one paper: exact id, arXiv id/DOI, then title."""
    parent: list[int] = []
    seen: dict[tuple[str, str], int] = {}
    nodes: list[tuple[Candidate, str, int]] = []  # candidate, call, rank

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for record in records:
        for rank, candidate in enumerate(record.candidates, start=1):
            index = len(nodes)
            nodes.append((candidate, record.name, rank))
            parent.append(index)
            for key in _identity_keys(candidate):
                other = seen.setdefault(key, index)
                if other != index:
                    parent[find(index)] = find(other)
    clusters: dict[int, _Cluster] = {}
    for index, (candidate, call, rank) in enumerate(nodes):
        cluster = clusters.setdefault(find(index), _Cluster(order=index))
        cluster.members.append(candidate)
        cluster.ranks[call] = min(rank, cluster.ranks.get(call, rank))
    return sorted(clusters.values(), key=lambda cluster: cluster.order)


def _representative(members: Sequence[Candidate]) -> Candidate:
    """The library copy when there is one, filled in from the other members."""
    library = [member for member in members if member.source == "library"]
    chosen = (library or list(members))[0]
    dated = next(
        (m for m in members if m.published and m.date_source != "created_at"), None
    )
    cited = [m.citations for m in members if m.citations is not None]
    return replace(
        chosen,
        published=dated.published if dated else chosen.published,
        date_source=dated.date_source if dated else chosen.date_source,
        authors=chosen.authors or next((m.authors for m in members if m.authors), ()),
        snippet=chosen.snippet or next((m.snippet for m in members if m.snippet), ""),
        citations=(
            chosen.citations
            if chosen.citations is not None
            else (cited[0] if cited else None)
        ),
    )


def _priority_factor(paper: Candidate, prioritize: Prioritize, today: date) -> float:
    """0..1: how much ``prioritize`` favours this paper."""
    if prioritize == "default" or paper.published is None:
        return 0.0
    age_years = max(0.0, (today - paper.published).days / 365.25)
    if prioritize == "recency":
        return max(0.0, 1.0 - age_years / 5.0)
    age = min(1.0, age_years / 10.0)
    if paper.citations is None:
        return age
    return 0.5 * age + 0.5 * min(1.0, math.log10(1 + paper.citations) / 4.0)


def rank_papers(
    records: Sequence[CallRecord], prioritize: Prioritize, today: date
) -> list[RankedPaper]:
    """Fuse, adjust and order every distinct paper (before the cut).

    Reciprocal-rank fusion over the calls that succeeded, scaled by
    ``AGREEMENT_BONUS`` per extra channel that found the paper, then
    ``prioritize`` scales each score by up to ``PRIORITY_WEIGHT``. Equal scores fall back to the
    favoured age (newer for ``recency``, older for ``historical``, undated last),
    then to the order the papers were first returned.
    """
    call_order = {record.name: index for index, record in enumerate(records)}
    channel_of = {record.name: record.channel for record in records}
    scored: list[tuple[RankedPaper, int]] = []
    for cluster in _cluster([r for r in records if r.status == "ok"]):
        paper = _representative(cluster.members)
        fused = sum(1.0 / (RRF_K + rank) for rank in cluster.ranks.values())
        agreeing = len({channel_of[call] for call in cluster.ranks})
        fused *= 1.0 + AGREEMENT_BONUS * (agreeing - 1)
        score = fused * (
            1.0 + PRIORITY_WEIGHT * _priority_factor(paper, prioritize, today)
        )
        found_by = tuple(sorted(cluster.ranks, key=call_order.__getitem__))
        scored.append((RankedPaper(paper, score, found_by), cluster.order))

    def order(item: tuple[RankedPaper, int]) -> tuple[float, float, int, str]:
        ranked, appearance = item
        published = ranked.paper.published
        age_tie = math.inf
        if published is not None and prioritize != "default":
            age_tie = float(
                -published.toordinal()
                if prioritize == "recency"
                else published.toordinal()
            )
        # round(): fusion sums floats in different orders; equal means equal.
        return (-round(ranked.score, 12), age_tie, appearance, ranked.paper.id)

    return [ranked for ranked, _ in sorted(scored, key=order)]


def cut_ratio(channels_answered: int) -> float:
    """The relative cut for a round in which this many channels answered.

    A paper only one channel found scores at most ``1 / (n x (1 + bonus x (n - 1)))``
    of one that all ``n`` channels ranked first: 0.22 with three channels, 0.4
    with two. ``RELATIVE_CUT`` drops it with three but not with two, so the cut
    is raised to sit ``LONE_HIT_MARGIN`` above that ceiling. With one channel
    nothing can agree, so only ``RELATIVE_CUT`` applies.
    """
    if channels_answered < 2:
        return RELATIVE_CUT
    agreement = channels_answered * (1.0 + AGREEMENT_BONUS * (channels_answered - 1))
    return max(RELATIVE_CUT, LONE_HIT_MARGIN / agreement)


def select_papers(
    ranked: Sequence[RankedPaper], channels_answered: int = 3
) -> list[RankedPaper]:
    """The relative cut (``cut_ratio``) decides; at most 15.

    There is no minimum: when one paper dominates it is the answer.
    """
    if not ranked:
        return []
    top = ranked[0].score
    ratio = cut_ratio(channels_answered)
    kept = [item for item in ranked if item.score >= ratio * top]
    return kept[:MAX_PAPERS]


def _cite_gate(
    selected: Sequence[RankedPaper], records: Sequence[CallRecord]
) -> list[RankedPaper]:
    """Drop anything no successful call of this round returned (never expected)."""
    observed = {
        (candidate.source, candidate.id)
        for record in records
        if record.status == "ok"
        for candidate in record.candidates
    }
    gated = [
        item for item in selected if (item.paper.source, item.paper.id) in observed
    ]
    if len(gated) != len(selected):
        logger.error("discover_papers dropped ids that no call returned")
    return gated


def _paper_json(item: RankedPaper, top: float) -> dict[str, Any]:
    paper = item.paper
    return {
        "id": paper.id,
        "source": paper.source,
        "title": paper.title,
        "published": paper.published.isoformat() if paper.published else None,
        "date_source": paper.date_source,
        "authors": list(paper.authors[:MAX_AUTHORS]),
        "snippet": paper.snippet[:SNIPPET_CHARS],
        # Relative to the best paper of this round (1.0): the cut is at 0.3.
        "score": round(item.score / top, 3) if top else 0.0,
        "found_by": list(item.found_by),
    }


async def discover(
    request: DiscoverRequest,
    *,
    organization_id: uuid.UUID,
    project_ids: Sequence[uuid.UUID],
    arxiv_search: ArxivSearch,
    today: date | None = None,
) -> dict[str, Any]:
    """Run one round and return the tool payload (an ``error`` payload if every call failed).

    ``project_ids`` is the grant's scope, already resolved (and re-checked) by
    the caller; the library calls search nothing outside it.
    """
    specs = _plan(
        request,
        organization_id=organization_id,
        project_ids=project_ids,
        arxiv_search=arxiv_search,
    )
    records = list(await asyncio.gather(*(_run_call(spec) for spec in specs)))
    calls = [record.as_dict() for record in records]
    if not any(record.status in ("ok", "empty") for record in records):
        return {"error": "upstream_unavailable", "calls": calls}
    day = today or datetime.now(timezone.utc).date()
    # Channels that returned something; an empty or failed one cannot agree.
    answered = len({record.channel for record in records if record.status == "ok"})
    selected = _cite_gate(
        select_papers(rank_papers(records, request.prioritize, day), answered),
        records,
    )
    top = selected[0].score if selected else 0.0
    return {
        "papers": [_paper_json(item, top) for item in selected],
        "calls": calls,
        "window": {
            "after": (
                request.published_after.isoformat() if request.published_after else None
            ),
            "before": (
                request.published_before.isoformat()
                if request.published_before
                else None
            ),
        },
        "prioritize": request.prioritize,
        "difficulty": request.difficulty,
        "followup_rounds_allowed": request.followup_rounds_allowed,
    }
