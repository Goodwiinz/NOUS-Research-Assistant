"""Semantic document search on pgvector (GOO-435, Plan 07 S5b).

``semantic_search`` embeds a query through ``DocumentEmbeddingService`` and
returns the nearest documents by cosine distance from ``document_embeddings``.
It is a read-only primitive: it never commits, and it returns ``[]`` instead of
raising when ``SEMANTIC_SEARCH_ENABLED`` is off or the query cannot be embedded.
``semantic_search_strict`` is the same search for a caller that must tell those
two cases from "no hits": it raises ``SemanticSearchUnavailable`` instead.

Tenancy and scoping are all in the one SQL statement:

- the organization filter is applied to both ``document_embeddings`` and
  ``documents``, and an invalid organization raises (fail closed, like
  ``fulltext_search_service``);
- ``project_ids`` limits results to documents linked to those Collections
  through ``collection_documents``; ``None`` means the whole organization and
  an empty list means nothing. The link, its Collection and the Collection's
  Workspace must all be live;
- soft-deleted documents and documents that have not finished processing never
  match.

The date window sits in the same WHERE as everything else, so it narrows the
candidates before ``ORDER BY ... LIMIT`` instead of filtering a bounded pool
afterwards (the alphaXiv defect: a 2020 window over a pool of recent hits
returned nothing). The ANN index is a trap for that rule: HNSW returns its
``hnsw.ef_search`` nearest rows first and the WHERE only prunes them, so a
filtered query can come back short or empty. Verified on a real server: with
``hnsw.iterative_scan`` off, a 2020 window over 120 closer recent documents
returned nothing through the index. Hence two plans, chosen per query by the
installed pgvector version:

- pgvector 0.8 or newer: ``ORDER BY embedding <=> :q`` stays index-eligible and
  the query sets ``hnsw.iterative_scan = strict_order``, so the index keeps
  producing neighbours until the filters are satisfied (bounded by
  ``hnsw.max_scan_tuples``; it is still approximate nearest neighbour search);
- older pgvector (no iterative scan): the ordering is written so the index
  cannot serve it, which makes the scan exact over the filtered rows. Correct
  at any size, slower on a very large organization; upgrade pgvector to get the
  index back.

Publication date. ``documents`` has no publication column. The date is
``document_metadata->>'publication_date'`` (set by arXiv ingestion, ISO
timestamp; also accepts ``YYYY-MM`` and ``YYYY``) and, for a document without
one, the day it was ingested (``documents.created_at``, UTC). Each hit says
which it is in ``publication_date_source``. Both are compared as ISO ``YYYY-MM-DD``
text so one malformed value can never make the query fail.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Sequence

from sqlalchemy import Select, case, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, ProcessingStatus
from src.models.document_embedding import DocumentEmbedding
from src.models.workspace import Workspace
from src.services.embedding.document_embedding_service import (
    EmbeddingRejected,
    document_embedding_service,
)

logger = logging.getLogger(__name__)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
SNIPPET_CHARS = 300
_QUERY_MAX_CHARS = 2000
# ``hnsw.ef_search`` is at least pgvector's default (40) and four times the
# page size, so a filtered page still has candidates to choose from.
_MIN_EF_SEARCH = 40

PublicationDateSource = Literal["metadata", "created_at"]


@dataclass(frozen=True)
class SemanticSearchHit:
    """One matching document.

    ``publication_date`` is ``document_metadata['publication_date']`` when the
    document has one (``publication_date_source == "metadata"``), otherwise the
    UTC day it was ingested (``"created_at"``); ``None`` only if the stored
    value is not a real calendar date. ``similarity`` is ``1 - cosine distance``.
    """

    document_id: str
    title: str
    publication_date: date | None
    publication_date_source: PublicationDateSource
    similarity: float
    snippet: str


def _org_scope(organization_id: Any) -> uuid.UUID:
    """The organization as a UUID, or ``ValueError`` (fail closed)."""
    try:
        return uuid.UUID(str(organization_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(
            "Semantic search requires a valid organization scope"
        ) from None


def _project_scope(project_ids: Sequence[Any]) -> set[uuid.UUID]:
    try:
        return {uuid.UUID(str(project_id)) for project_id in project_ids}
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Semantic search project_ids must be UUIDs") from None


def _as_day(value: date) -> date:
    return value.date() if isinstance(value, datetime) else value


def publication_text_expressions() -> tuple[Any, Any]:
    """SQL for the effective publication day as ISO text, and where it came from."""
    raw = Document.document_metadata["publication_date"].as_string()
    from_metadata = case(
        (raw.regexp_match(r"^\d{4}-\d{2}-\d{2}"), func.substr(raw, 1, 10)),
        (raw.regexp_match(r"^\d{4}-\d{2}$"), raw + "-01"),
        (raw.regexp_match(r"^\d{4}$"), raw + "-01-01"),
        else_=None,
    )
    ingested = func.to_char(func.timezone("UTC", Document.created_at), "YYYY-MM-DD")
    effective = func.coalesce(from_metadata, ingested)
    source = case((from_metadata.is_not(None), "metadata"), else_="created_at")
    return effective, source


def project_scope_condition(project_ids: set[uuid.UUID]) -> Any:
    """``Document`` is linked to one of ``project_ids``, every ancestor live.

    The link, its Collection and the Collection's Workspace must all be live,
    so a soft-deleted parent revokes its documents even though no cascade
    touched them. Shared by the semantic and the keyword search, which must
    scope a document identically.
    """
    return exists().where(
        CollectionDocument.document_id == Document.id,
        CollectionDocument.collection_id.in_(project_ids),
        CollectionDocument.is_deleted.is_(False),
        Collection.id == CollectionDocument.collection_id,
        Collection.is_deleted.is_(False),
        Workspace.id == Collection.workspace_id,
        Workspace.is_deleted.is_(False),
    )


def build_semantic_search_statement(
    *,
    query_vector: list[float],
    organization_id: uuid.UUID,
    project_ids: set[uuid.UUID] | None,
    published_after: date | None,
    published_before: date | None,
    limit: int,
    exact: bool = False,
) -> Select[Any]:
    """The single statement ``semantic_search`` runs (exposed for tests).

    ``exact`` writes the ordering so no ANN index can serve it (see the module
    docstring); the default leaves it index-eligible.
    """
    published_on, published_source = publication_text_expressions()
    distance = DocumentEmbedding.embedding.cosine_distance(query_vector)

    statement = (
        select(
            Document.id.label("document_id"),
            Document.title.label("title"),
            published_on.label("published_on"),
            published_source.label("published_source"),
            distance.label("distance"),
            func.left(Document.content_summary, 600).label("summary"),
            Document.document_metadata["description"].as_string().label("description"),
            func.left(Document.content_text, 1200).label("head"),
        )
        .select_from(DocumentEmbedding)
        .join(Document, Document.id == DocumentEmbedding.document_id)
        .where(
            DocumentEmbedding.organization_id == organization_id,
            Document.organization_id == organization_id,
            Document.is_deleted.is_(False),
            Document.processing_status == ProcessingStatus.COMPLETED,
        )
    )
    if project_ids is not None:
        statement = statement.where(project_scope_condition(project_ids))
    if published_after is not None:
        statement = statement.where(published_on >= published_after.isoformat())
    if published_before is not None:
        statement = statement.where(published_on <= published_before.isoformat())
    # ``(a <=> b) + 0`` is no longer the index's ordering operator, so the
    # planner must sort the filtered rows exactly.
    ordering = distance + 0 if exact else distance
    return statement.order_by(ordering).limit(limit)


_HEADING = re.compile(r"^\s*#+\s*abstract\s*", re.IGNORECASE)


def snippet_from(summary: str | None, description: str | None, head: str | None) -> str:
    """The first of summary, metadata description or text head, trimmed."""
    for candidate in (summary, description, head):
        text = " ".join(_HEADING.sub("", str(candidate or "")).split())
        if text:
            if len(text) > SNIPPET_CHARS:
                return text[: SNIPPET_CHARS - 1].rstrip() + "…"
            return text
    return ""


def parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _to_hit(row: Any) -> SemanticSearchHit:
    source: PublicationDateSource = (
        "metadata" if row.published_source == "metadata" else "created_at"
    )
    return SemanticSearchHit(
        document_id=str(row.document_id),
        title=str(row.title or ""),
        publication_date=parse_day(row.published_on),
        publication_date_source=source,
        similarity=1.0 - float(row.distance),
        snippet=snippet_from(row.summary, row.description, row.head),
    )


def _version_at_least(version: str | None, minimum: tuple[int, int, int]) -> bool:
    parts = [int(part) for part in re.findall(r"\d+", version or "")[:3]]
    return tuple(parts + [0] * (3 - len(parts))) >= minimum


async def _prepare_index_scan(session: AsyncSession, limit: int) -> bool:
    """Set per-transaction HNSW options; return whether the index may be used.

    ``set_config(..., true)`` is ``SET LOCAL``: it ends with the caller's
    transaction. ``False`` means pgvector cannot iterate the index, so the
    caller must ask for the exact plan.
    """
    version = (
        await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    ).scalar()
    if not _version_at_least(version, (0, 8, 0)):
        return False
    await session.execute(
        select(
            func.set_config(
                "hnsw.ef_search", str(max(_MIN_EF_SEARCH, limit * 4)), True
            ),
            func.set_config("hnsw.iterative_scan", "strict_order", True),
        )
    )
    return True


class SemanticSearchUnavailable(Exception):
    """The semantic channel cannot answer right now, as opposed to having no hits.

    ``reason`` is a stable token: ``disabled`` (``SEMANTIC_SEARCH_ENABLED`` is
    off), ``embedding_failed`` (the provider errored or timed out) or
    ``embedding_rejected`` (it answered with a vector the column cannot hold).
    Only ``semantic_search_strict`` raises it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def semantic_search_strict(
    session: AsyncSession,
    *,
    query: str,
    organization_id: uuid.UUID | str,
    project_ids: Sequence[uuid.UUID | str] | None = None,
    published_after: date | None = None,
    published_before: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SemanticSearchHit]:
    """``semantic_search``, but a channel that cannot answer raises.

    Same arguments, scoping and window as ``semantic_search``. The difference is
    only the failure contract: a disabled flag or a query that could not be
    embedded raises ``SemanticSearchUnavailable`` instead of returning ``[]``, so
    a caller that reports per-channel status (``discover_papers``) can tell "the
    provider is down" from "nothing matched". Zero hits is still ``[]``.
    """
    if not settings.SEMANTIC_SEARCH_ENABLED:
        raise SemanticSearchUnavailable("disabled")
    org = _org_scope(organization_id)
    projects = None if project_ids is None else _project_scope(project_ids)
    if projects is not None and not projects:
        return []  # an empty scope is no access, not the whole organization
    text = " ".join(str(query or "").split())[:_QUERY_MAX_CHARS]
    page = min(int(limit), MAX_LIMIT)
    if not text or page < 1:
        return []
    after = None if published_after is None else _as_day(published_after)
    before = None if published_before is None else _as_day(published_before)
    if after is not None and before is not None and after > before:
        return []

    try:
        embedded = await document_embedding_service.embed_text(text, for_query=True)
    except EmbeddingRejected as rejected:
        logger.warning("semantic search query not embedded: %s", rejected)
        raise SemanticSearchUnavailable("embedding_rejected") from rejected
    except Exception as failure:  # noqa: BLE001 - reported as "unavailable"
        logger.warning("semantic search query embedding failed", exc_info=True)
        raise SemanticSearchUnavailable("embedding_failed") from failure

    index_allowed = await _prepare_index_scan(session, page)
    result = await session.execute(
        build_semantic_search_statement(
            query_vector=embedded.vector,
            organization_id=org,
            project_ids=projects,
            published_after=after,
            published_before=before,
            limit=page,
            exact=not index_allowed,
        )
    )
    return [_to_hit(row) for row in result]


async def semantic_search(
    session: AsyncSession,
    *,
    query: str,
    organization_id: uuid.UUID | str,
    project_ids: Sequence[uuid.UUID | str] | None = None,
    published_after: date | None = None,
    published_before: date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SemanticSearchHit]:
    """Documents nearest to ``query``, best first; ``[]`` when the channel is off.

    ``published_after`` / ``published_before`` are inclusive day bounds on the
    publication date described in the module docstring. ``limit`` is clamped
    to ``MAX_LIMIT``. Raises ``ValueError`` for an invalid organization or
    project id; database errors propagate. Never commits. A disabled flag or a
    query that cannot be embedded is ``[]`` here (use ``semantic_search_strict``
    to be told which).
    """
    try:
        return await semantic_search_strict(
            session,
            query=query,
            organization_id=organization_id,
            project_ids=project_ids,
            published_after=published_after,
            published_before=published_before,
            limit=limit,
        )
    except SemanticSearchUnavailable:
        return []
