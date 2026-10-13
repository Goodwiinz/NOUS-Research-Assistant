"""Scoped PostgreSQL full-text search with a publication window (GOO-435, S5c).

``keyword_search`` is the lexical twin of ``semantic_search``: the same
``documents.search_vector`` column and ``plainto_tsquery`` matching as
``fulltext_search_service`` (every stemmed query word must match, English stop
words and one-letter words are ignored), but written for the discovery tool
rather than for the ``/documents/search`` page:

- it takes the same ``organization_id`` / ``project_ids`` scope as
  ``semantic_search`` and applies it in the one SQL statement (the
  organization filter, plus the live link / Collection / Workspace chain from
  ``project_scope_condition``). ``fulltext_search_service`` can only be scoped
  by an id list, which is unbounded for a workspace-wide grant;
- the publication window is a predicate of that statement. The old service
  filters ``documents.created_at`` (the ingest day), which for an arXiv paper
  is not the year it was published, so a 2020 window would never find a paper
  ingested last week. Here the window is on the same effective publication day
  as ``semantic_search`` (``document_metadata->>'publication_date'``, else the
  ingest day) and each hit says which it is;
- the ranking is ``ts_rank_cd``, with the newer publication day and then the id
  as tie-breaks so the order is deterministic.

Read-only: it never commits, and it raises ``ValueError`` (fail closed) for an
invalid organization or project id, like ``semantic_search``. It does not apply
the junk-document exclusions of ``fulltext_search_service``, which exist only
to hide seeded test data in the shared dev database.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import Select, desc, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document, ProcessingStatus
from src.services.search.semantic_search_service import (
    PublicationDateSource,
    parse_day,
    project_scope_condition,
    publication_text_expressions,
    snippet_from,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
_QUERY_MAX_CHARS = 2000
# Same as ``FullTextSearchService.min_query_length``: shorter words are noise.
_MIN_WORD_CHARS = 2
_NON_WORD = re.compile(r"[^\w\s]")
# The regconfig ``documents.search_vector`` is built with (see
# ``FullTextSearchService.update_document_search_vector``).
_ENGLISH = literal_column("'english'::regconfig")


@dataclass(frozen=True)
class KeywordSearchHit:
    """One matching document.

    ``publication_date`` and ``publication_date_source`` mean what they do on
    ``SemanticSearchHit``. ``rank`` is the raw ``ts_rank_cd`` value, only
    comparable within one result list.
    """

    document_id: str
    title: str
    publication_date: date | None
    publication_date_source: PublicationDateSource
    rank: float
    snippet: str


def search_terms(query: str) -> str:
    """The words ``plainto_tsquery`` receives: lower-cased, punctuation removed.

    Mirrors ``FullTextSearchService._prepare_search_terms``, except that a query
    with no usable word is empty here instead of falling back to the raw text.
    """
    cleaned = _NON_WORD.sub(" ", str(query or "").lower()[:_QUERY_MAX_CHARS])
    return " ".join(word for word in cleaned.split() if len(word) >= _MIN_WORD_CHARS)


def _org_scope(organization_id: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(organization_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Keyword search requires a valid organization scope") from None


def _project_scope(project_ids: Sequence[Any]) -> set[uuid.UUID]:
    try:
        return {uuid.UUID(str(project_id)) for project_id in project_ids}
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Keyword search project_ids must be UUIDs") from None


def _as_day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def build_keyword_search_statement(
    *,
    terms: str,
    organization_id: uuid.UUID,
    project_ids: set[uuid.UUID] | None,
    published_after: date | None,
    published_before: date | None,
    limit: int,
) -> Select[Any]:
    """The single statement ``keyword_search`` runs (exposed for tests)."""
    published_on, published_source = publication_text_expressions()
    tsquery = func.plainto_tsquery(_ENGLISH, terms)
    rank = func.ts_rank_cd(Document.search_vector, tsquery)

    statement = select(
        Document.id.label("document_id"),
        Document.title.label("title"),
        published_on.label("published_on"),
        published_source.label("published_source"),
        rank.label("rank"),
        func.left(Document.content_summary, 600).label("summary"),
        Document.document_metadata["description"].as_string().label("description"),
        func.left(Document.content_text, 1200).label("head"),
    ).where(
        Document.organization_id == organization_id,
        Document.is_deleted.is_(False),
        Document.processing_status == ProcessingStatus.COMPLETED,
        Document.search_vector.bool_op("@@")(tsquery),
    )
    if project_ids is not None:
        statement = statement.where(project_scope_condition(project_ids))
    if published_after is not None:
        statement = statement.where(published_on >= published_after.isoformat())
    if published_before is not None:
        statement = statement.where(published_on <= published_before.isoformat())
    return statement.order_by(desc(rank), desc(published_on), Document.id).limit(limit)


async def keyword_search(
    session: AsyncSession,
    *,
    query: str,
    organization_id: uuid.UUID | str,
    project_ids: Sequence[uuid.UUID | str] | None = None,
    published_after: date | None = None,
    published_before: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[KeywordSearchHit]:
    """Documents containing every word of ``query``, best first.

    ``project_ids`` limits the search to documents linked to those Collections
    (``None`` is the whole organization, an empty list is nothing).
    ``published_after`` / ``published_before`` are inclusive day bounds on the
    publication day described in the module docstring. ``limit`` is clamped to
    ``MAX_LIMIT``. Database errors propagate. Never commits.
    """
    org = _org_scope(organization_id)
    projects = None if project_ids is None else _project_scope(project_ids)
    if projects is not None and not projects:
        return []  # an empty scope is no access, not the whole organization
    terms = search_terms(query)
    page = min(int(limit), MAX_LIMIT)
    if not terms or page < 1:
        return []
    after = None if published_after is None else _as_day(published_after)
    before = None if published_before is None else _as_day(published_before)
    if after is not None and before is not None and after > before:
        return []
    result = await session.execute(
        build_keyword_search_statement(
            terms=terms,
            organization_id=org,
            project_ids=projects,
            published_after=after,
            published_before=before,
            limit=page,
        )
    )
    return [
        KeywordSearchHit(
            document_id=str(row.document_id),
            title=str(row.title or ""),
            publication_date=parse_day(row.published_on),
            publication_date_source=(
                "metadata" if row.published_source == "metadata" else "created_at"
            ),
            rank=float(row.rank),
            snippet=snippet_from(row.summary, row.description, row.head),
        )
        for row in result
    ]
