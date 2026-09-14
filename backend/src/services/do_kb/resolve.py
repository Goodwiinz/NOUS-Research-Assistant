"""Shared document-resolution logic for DO Knowledge Base chunks.

Both the agent tool (``tools_impl._tool_do_kb_retrieve``) and the RAG node
(``_nodes_rag._try_primary_do_kb_read``) need to:

1. Map storage-key ``document_id`` values from DO KB back to canonical
   ``Document.id`` + ``Document.title`` rows.
2. Optionally post-filter by project membership via ``CollectionDocument``.

This module extracts that near-identical logic into one helper so callers
stay DRY and any future bug-fix lands in a single place.
"""

from __future__ import annotations

import logging
from typing import Collection, Optional, Sequence
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import CollectionDocument
from src.models.document import Document
from src.services.do_kb.models import Chunk

logger = logging.getLogger(__name__)


def _escape_like(s: str) -> str:
    """Escape SQL LIKE wildcards in untrusted strings.

    Storage keys come from the DO Knowledge Base API — an external service.
    Backslash escapes ``%`` (multi-char wildcard) and ``_`` (single char) so
    a malformed/malicious key cannot broaden the suffix match.
    Use with ``Column.like(pattern, escape='\\\\')``.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def resolve_and_filter_chunks(
    *,
    chunks: Sequence[Chunk],
    org_id: UUID,
    session: AsyncSession,
    project_id: Optional[str] = None,
    allowed_document_ids: Optional[Collection[UUID]] = None,
) -> tuple[dict[str, tuple[str, str]], list[Chunk]]:
    """Resolve DO KB chunks to Documents and optionally filter by project.

    Parameters
    ----------
    chunks:
        Raw chunks from ``DOKnowledgeBaseClient.retrieve()``.
    org_id:
        The current user's ``organization_id``.
    session:
        An open SQLAlchemy ``AsyncSession`` for DB queries.
    project_id:
        When set, post-filters chunks so only documents belonging to this
        project (via ``CollectionDocument``) survive.

    Returns
    -------
    (title_by_key, chunks_to_emit)
        *title_by_key* maps each storage-key leaf to ``(doc_id_str, title)``.
        *chunks_to_emit* is the (possibly filtered) list of chunks the caller
        should use.  When *project_id* is active and either no chunks resolve
        or all resolved chunks are filtered out, *chunks_to_emit* is empty —
        callers decide what that means (return ``None`` for fallback or return
        an empty payload).
    """
    if not chunks:
        return {}, []

    # ------------------------------------------------------------------
    # Step 1: storage-key extraction → DB lookup → suffix match
    # ------------------------------------------------------------------
    storage_keys = {c.document_id for c in chunks if c.document_id}
    title_by_key: dict[str, tuple[str, str]] = {}

    if storage_keys:
        # DO KB reports each chunk's ``item_name`` as the *basename* of the
        # indexed object key. NOUS's KB data source is the canonical text
        # mirror ``documents/{org}/{doc}.txt`` (see
        # ``documents.object_keys.canonical_text_key``), so DO returns
        # ``"<doc_uuid>.txt"`` — the stem IS the ``Document.id``. Resolve by it
        # directly; matching the raw key against ``Document.storage_path`` never
        # hits, because storage_path is the *original* upload key
        # (``…/{doc}/{ts}_{hash}.pdf``, or NULL for local-backend docs).
        #
        # Storage-path suffix matching stays as a fallback for any data source
        # whose reported key is a full original-object path instead of the
        # ``.txt`` mirror.
        doc_id_by_key: dict[str, UUID] = {}
        for k in storage_keys:
            stem = k.rsplit("/", 1)[-1]
            if stem.endswith(".txt"):
                stem = stem[:-4]
            try:
                doc_id_by_key[k] = UUID(stem)
            except ValueError:
                pass

        filters = [Document.storage_path == k for k in storage_keys]
        filters += [
            Document.storage_path.like(f"%/{_escape_like(k)}", escape="\\")
            for k in storage_keys
        ]
        if doc_id_by_key:
            filters.append(Document.id.in_(set(doc_id_by_key.values())))

        rows = await session.execute(
            select(Document.id, Document.storage_path, Document.title)
            .where(Document.organization_id == org_id)
            .where(or_(*filters))
        )
        title_by_doc_id: dict[UUID, Optional[str]] = {}
        doc_ids_by_storage_alias: dict[str, set[UUID]] = {}
        for doc_id, storage_path, title in rows:
            title_by_doc_id[doc_id] = title
            if storage_path:
                leaf = storage_path.rsplit("/", 1)[-1]
                for alias in {storage_path, leaf}:
                    doc_ids_by_storage_alias.setdefault(alias, set()).add(doc_id)

        # Key ``title_by_key`` by the RAW KB key (``c.document_id``) so
        # downstream lookups (``_ranked_doc_ids``, ``_shape_do_kb_context``, the
        # project filter below) resolve without re-deriving the stem.
        for k in storage_keys:
            candidate_ids = set(doc_ids_by_storage_alias.get(k, ()))
            canonical_id = doc_id_by_key.get(k)
            if canonical_id is not None and canonical_id in title_by_doc_id:
                candidate_ids.add(canonical_id)

            # A provider key is trustworthy only when every matching canonical
            # and storage identity belongs to the same document. Resolve this
            # before project/allowed-ID filtering so those scopes cannot hide a
            # competing same-org document and manufacture uniqueness.
            if len(candidate_ids) != 1:
                continue

            resolved_id = next(iter(candidate_ids))
            title = title_by_doc_id[resolved_id]
            fallback_title = str(resolved_id) if resolved_id == canonical_id else k
            title_by_key[k] = (str(resolved_id), title or fallback_title)

    # ------------------------------------------------------------------
    # Step 2: project scoping via CollectionDocument
    # ------------------------------------------------------------------
    chunks_to_emit: list[Chunk] = list(chunks)

    if project_id and not title_by_key and chunks:
        # No chunks resolved to known docs under project scope — callers
        # should treat this as empty to avoid leaking unscoped content.
        return title_by_key, []

    if project_id and title_by_key:
        resolved_doc_ids = {
            UUID(doc_id) for doc_id, _ in title_by_key.values() if doc_id
        }
        if resolved_doc_ids:
            try:
                pid = UUID(str(project_id))
            except (ValueError, TypeError):
                logger.warning(
                    "do_kb resolve: invalid project_id %r — refusing to "
                    "serve unscoped chunks",
                    project_id,
                )
                return title_by_key, []
            if pid is not None:
                membership_rows = await session.execute(
                    select(CollectionDocument.document_id).where(
                        CollectionDocument.collection_id == pid,
                        CollectionDocument.document_id.in_(resolved_doc_ids),
                    )
                )
                in_project = {str(r[0]) for r in membership_rows}
                chunks_to_emit = [
                    c
                    for c in chunks
                    if (title_by_key.get(c.document_id or "", (None, None))[0] or "")
                    in in_project
                ]

    if allowed_document_ids is not None:
        allowed = set(allowed_document_ids)
        title_by_key = {
            key: value
            for key, value in title_by_key.items()
            if UUID(value[0]) in allowed
        }
        chunks_to_emit = [
            chunk
            for chunk in chunks_to_emit
            if (chunk.document_id or "") in title_by_key
        ]

    return title_by_key, chunks_to_emit
