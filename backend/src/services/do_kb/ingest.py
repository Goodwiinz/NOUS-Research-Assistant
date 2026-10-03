"""Ingest: push existing Documents into the org's DO KB.

DO KB is the retrieval backend (Qdrant was removed; PostgreSQL full-text is
the always-on fallback). Failure-isolated: every exception is swallowed and
logged — a KB outage must never fail ingestion. Callers on the ingestion path
record the outcome in ``documents.do_kb_sync_status`` (audit D1) so a failed
sync is visible and re-drivable by the satellite reconciler
(``src.tasks.reconcile_tasks``) instead of silently dropped.

Canonical Spaces key layout:
    documents/{organization_id}/{document_id}.{ext}

Where ``ext`` is ``pdf`` for original uploads (already in Spaces) or
``txt`` for content-text fallbacks. One KB data source per organization
points at ``documents/{organization_id}/`` so adding more files is just an
upload + re-index — no per-doc data source spam on the KB.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.document import Document

# Single source of truth for the canonical text-mirror key, shared with the
# document-delete path + storage reconciler so their key derivation can't drift
# from what ``_upload_canonical_text`` writes (drift would leak the .txt object
# on delete — audit finding D6). Re-exported here for backwards compatibility.
from src.services.documents.object_keys import canonical_text_key
from src.services.documents.satellite_state import pending_writes

from .client import DOKnowledgeBaseClient, DOKnowledgeBaseError, get_do_kb_client
from .pre_flight import ensure_content_text_for_kb
from .provisioner import ensure_kb_for_org

logger = logging.getLogger(__name__)

__all__ = [
    "canonical_text_key",
    "sync_document_to_kb",
    "sync_documents_to_kb",
    "unsync_document_from_kb",
]


def _source_item_path(src: dict) -> Optional[str]:
    """Best-effort extract of a data source's Spaces item_path (defensive against
    the exact list-response shape)."""
    spaces = src.get("spaces_data_source") or src.get("spaces") or {}
    if isinstance(spaces, dict):
        return spaces.get("item_path") or spaces.get("key")
    return None


async def _existing_data_source_uuid(api, kb_uuid: str, key: str) -> Optional[str]:
    """Return the uuid of an existing data source whose item_path == ``key``, else
    None. Best-effort: any error / unknown shape returns None so the caller adds
    normally (never blocks ingest on a failed/uncertain dedup lookup)."""
    try:
        for src in await api.list_data_sources(kb_uuid=kb_uuid):
            if _source_item_path(src) == key:
                uuid = src.get("uuid")
                if uuid:
                    return str(uuid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("do_kb list_data_sources failed (will add): %s", exc)
    return None


_DO_KB_INGEST_METRIC = "do_kb_ingest_total"


def _record_metric(status: str) -> None:
    """Best-effort ingest-outcome counter; observability must never break ingest.

    Was a dead no-op: it looked up ``metrics.agent_do_kb_ingest_total``, a
    module attribute that is defined nowhere, so every call silently did
    nothing. Route through the registered ``increment_counter`` instead —
    the same wiring the RAG read path (`_record_do_kb_read`) uses.
    """
    try:
        from src.observability.metrics import increment_counter

        increment_counter(_DO_KB_INGEST_METRIC, attributes={"status": status})
    except Exception:  # pragma: no cover - observability is optional
        pass


def _resolve_spaces_source(document: Document) -> Optional[tuple[str, str]]:
    """Return (bucket, key) when document points at a Spaces object, else None."""
    backend = getattr(document, "storage_backend", None)
    storage_path = getattr(document, "storage_path", None)
    if backend == "s3" and storage_path:
        return settings.S3_BUCKET_NAME, storage_path
    return None


async def _upload_canonical_text(document: Document) -> Optional[tuple[str, str]]:
    """Mirror the document's text into the KB bucket under the canonical key
    ``documents/{org}/{doc}.txt`` and return ``(bucket, key)``.

    This is the PRIMARY ingest source: it guarantees an object exists under the
    ``documents/{org}/`` prefix the KB data source reads, regardless of where the
    original file lives (local / MinIO / a different bucket). The key is
    deterministic from the document id, so re-runs simply overwrite (idempotent);
    we do NOT mutate the document's real ``storage_path``/``storage_backend``,
    which must keep pointing at the original upload.
    """
    text = getattr(document, "content_text", None)
    if not text:
        return None
    try:
        from src.core.s3_client import S3StorageHelper

        helper = S3StorageHelper()
        key = canonical_text_key(document)
        # upload_file is sync/blocking (boto3); offload so we don't stall the
        # event loop for every document during bulk ingest (audit A8).
        await asyncio.to_thread(
            helper.upload_file,
            key,
            text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
        )
        return helper.bucket, key
    except Exception as exc:
        logger.warning(
            "do_kb canonical text upload failed",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
        return None


async def sync_document_to_kb(
    session: AsyncSession,
    document: Document,
    *,
    client: Optional[DOKnowledgeBaseClient] = None,
    trigger_indexing: bool = True,
) -> Optional[str]:
    """Add the document to its organization's DO KB and stage the data source UUID.

    Returns the data source UUID on success, or None if skipped/failed.
    Always swallows exceptions — DO KB is best-effort by design (PostgreSQL
    full-text search keeps working without it), so a KB failure must never
    propagate into the caller's pipeline. Ingestion-path callers translate a
    None return into ``documents.do_kb_sync_status='failed'`` for the
    reconciler.
    """
    if not settings.DO_KB_ENABLED:
        return None

    if document.do_kb_data_source_uuid:
        return document.do_kb_data_source_uuid

    # Defense in depth (R2-H11): callers (backfill/reconciler) should already
    # filter out soft-deleted docs, but a cheap re-check here means a stale
    # in-memory Document or a future caller that forgets the filter still
    # can't push a deleted doc's content into DO KB. Refresh from the DB first
    # (codex P1) — the Document instance may have been loaded by a batch query
    # some time ago, and a delete committed since then would leave the cached
    # `is_deleted` stale. A race after this refresh (delete lands mid-upload)
    # still shrinks to milliseconds instead of minutes, and self-heals: the
    # delete path only unsyncs a doc that already has a data-source uuid, so
    # if the race sets the uuid just after delete, the reconciler's deleted-doc
    # sweep (do_kb_data_source_uuid IS NOT NULL) retries the cleanup.
    try:
        await session.refresh(document, attribute_names=["is_deleted"])
    except Exception as exc:  # noqa: BLE001 - refresh failure isn't fatal
        logger.warning(
            "do_kb is_deleted refresh failed — using cached value",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
    if document.is_deleted:
        logger.info(
            "do_kb skip — document soft-deleted",
            extra={"document_id": str(document.id)},
        )
        return None

    api = client or get_do_kb_client()

    try:
        # This helper is often called with a request/ingest session that also
        # contains unrelated pending work. Do not commit that caller-owned
        # transaction from the DO KB adapter; the caller decides when to commit.
        kb_uuid = await ensure_kb_for_org(
            session, document.organization_id, client=api, commit=False
        )
    except DOKnowledgeBaseError as exc:
        logger.warning(
            "do_kb provisioning skipped",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
        _record_metric("provision_failed")
        return None
    except Exception as exc:  # noqa: BLE001 - failure isolation is intentional
        logger.warning(
            "do_kb provisioning unexpected error",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
        _record_metric("provision_error")
        return None

    # Pre-flight guard: extract text locally for large/complex PDFs so the
    # canonical .txt path is used instead of the raw PDF (DO KB's server-side
    # parser times out on big/complex PDFs). No-op for small PDFs, non-PDFs, and
    # documents that already have content_text.
    await ensure_content_text_for_kb(document)

    # Prefer the canonical text object in the KB bucket (guarantees presence
    # under the documents/{org}/ prefix the data source reads). Only fall back to
    # the original Spaces object when the document has no extracted text.
    source = await _upload_canonical_text(document) or _resolve_spaces_source(document)
    if source is None:
        logger.info(
            "do_kb skip — no source",
            extra={"document_id": str(document.id), "kb_uuid": kb_uuid},
        )
        _record_metric("skipped_no_source")
        return None

    bucket, key = source

    # Idempotency (A7): if a data source for this exact item_path already exists
    # (e.g. a prior run added it but the commit persisting the uuid failed), reuse
    # it instead of adding a duplicate. Best-effort — any list error / unknown
    # response shape falls through to a normal add, so the worst case is the
    # pre-existing behavior (a possible duplicate), never a missing document.
    ds_uuid = await _existing_data_source_uuid(api, kb_uuid, key)
    if ds_uuid is None:
        try:
            data_source = await api.add_spaces_data_source(
                kb_uuid=kb_uuid, bucket=bucket, key=key
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "do_kb add_data_source failed",
                extra={
                    "document_id": str(document.id),
                    "kb_uuid": kb_uuid,
                    "error": str(exc),
                },
            )
            _record_metric("add_data_source_failed")
            return None
        ds_uuid = data_source.uuid

    document.do_kb_data_source_uuid = ds_uuid
    document.do_kb_indexed_at = datetime.now(timezone.utc)
    # "registered": the data source is added but indexing has NOT been confirmed
    # (start_indexing below is fire-and-forget and may fail). The old code wrote
    # "indexed" here, before the kick, which lied whenever the kick 400'd.
    # ponytail: a reconciliation poller (client.get_indexing_job — already exists,
    # called nowhere) would flip this to "indexed" once DO confirms. Not built
    # now; the honest "registered" is the smallest fix that stops the lie.
    document.do_kb_index_status = "registered"

    if trigger_indexing:
        try:
            await api.start_indexing(kb_uuid=kb_uuid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "do_kb start_indexing failed (non-fatal)",
                extra={"kb_uuid": kb_uuid, "error": str(exc)},
            )
            _record_metric("indexing_kick_failed")

    _record_metric("ok")
    return ds_uuid


async def sync_documents_to_kb(
    session: AsyncSession,
    documents: list[Document],
    *,
    client: Optional[DOKnowledgeBaseClient] = None,
) -> list[Optional[str]]:
    """Bulk variant: one indexing kick after all data sources are added."""
    if not settings.DO_KB_ENABLED or not documents:
        return [None] * len(documents)

    api = client or get_do_kb_client()
    results: list[Optional[str]] = []
    # Track org ids of successfully-synced docs. organization_id is a plain
    # column (no async lazy-load), unlike `doc.organization.do_kb_uuid` which is
    # an unawaited relationship that resolves to None in async context → the old
    # code left seen_kbs empty and NEVER kicked indexing (audit A1).
    synced_org_ids: set[str] = set()

    for doc in documents:
        ds_uuid = await sync_document_to_kb(
            session, doc, client=api, trigger_indexing=False
        )
        results.append(ds_uuid)
        if ds_uuid:
            synced_org_ids.add(str(doc.organization_id))

    for org_id in synced_org_ids:
        try:
            kb_uuid = await ensure_kb_for_org(session, org_id, client=api, commit=False)
            await api.start_indexing(kb_uuid=kb_uuid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "do_kb bulk start_indexing failed",
                extra={"org_id": org_id, "error": str(exc)},
            )
            _record_metric("indexing_kick_failed")

    return results


async def unsync_document_from_kb(
    session: AsyncSession,
    document: Document,
    *,
    client: Optional[DOKnowledgeBaseClient] = None,
) -> bool:
    """Remove a document's data source from DO KB and clear its DB columns.

    Inverse of ``sync_document_to_kb``. Use when a document is failing to index
    (e.g. a PDF that keeps timing out) and needs a clean re-sync: call this, fix
    the document (populate ``content_text``), then call ``sync_document_to_kb``
    again.

    Returns True when the data source was deleted (or was already absent), False
    on error. Never raises — cleanup must not block other operations.
    """
    if not settings.DO_KB_ENABLED:
        return True

    if document.is_deleted and pending_writes(document, "do_kb"):
        return await _cleanup_unknown_deleted_write(session, document, client=client)

    ds_uuid = document.do_kb_data_source_uuid
    if not ds_uuid:
        return True

    api = client or get_do_kb_client()

    try:
        kb_uuid = await ensure_kb_for_org(session, document.organization_id, client=api)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "do_kb unsync — provisioning failed",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
        return False

    try:
        await api.delete_data_source(kb_uuid=kb_uuid, ds_uuid=ds_uuid)
    except DOKnowledgeBaseError as exc:
        if exc.status_code == 404:
            logger.info(
                "do_kb data source already absent",
                extra={"document_id": str(document.id), "ds_uuid": ds_uuid},
            )
        else:
            logger.warning(
                "do_kb delete_data_source failed",
                extra={
                    "document_id": str(document.id),
                    "ds_uuid": ds_uuid,
                    "error": str(exc),
                },
            )
            _record_metric("unsync_failed")
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "do_kb delete_data_source failed",
            extra={
                "document_id": str(document.id),
                "ds_uuid": ds_uuid,
                "error": str(exc),
            },
        )
        _record_metric("unsync_failed")
        return False

    document.do_kb_data_source_uuid = None
    document.do_kb_indexed_at = None
    document.do_kb_index_status = None

    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "do_kb unsync persist failed",
            extra={"document_id": str(document.id), "error": str(exc)},
        )
        return False

    _record_metric("unsynced")
    return True


async def _cleanup_unknown_deleted_write(
    session: AsyncSession, document: Document, *, client=None
) -> bool:
    """Discover every accepted source, including a UUID lost with its worker.

    Derive keys from the scoped document, never from caller-editable metadata.
    Unknown writer tokens remain: an earlier cleanup cannot prove a dispatched
    remote operation has finished. Their retries are ordered fairly.
    """
    document_id, organization_id = document.id, document.organization_id
    api = client or get_do_kb_client()
    try:
        kb_uuid = await ensure_kb_for_org(session, organization_id, client=api)
        text_key = canonical_text_key(document)
        keys = {text_key}
        if document.storage_backend == "s3" and document.storage_path:
            keys.add(document.storage_path)
        identifiers = (
            {document.do_kb_data_source_uuid}
            if document.do_kb_data_source_uuid
            else set()
        )
        # A failed listing is a failed cleanup, never proof of absence.
        for source in await api.list_data_sources(kb_uuid=kb_uuid):
            if _source_item_path(source) in keys and source.get("uuid"):
                identifiers.add(str(source["uuid"]))
        await session.rollback()
        for identifier in identifiers:
            try:
                await api.delete_data_source(kb_uuid=kb_uuid, ds_uuid=identifier)
            except DOKnowledgeBaseError as exc:
                if exc.status_code != 404:
                    raise
        from src.core.s3_client import S3StorageHelper

        await asyncio.to_thread(S3StorageHelper().delete_file, text_key)
        current = (
            await session.execute(
                select(Document)
                .where(
                    Document.id == document_id,
                    Document.organization_id == organization_id,
                    Document.is_deleted == True,
                )
                .with_for_update()
                .execution_options(populate_existing=True, autoflush=False)
            )
        ).scalar_one_or_none()
        if current is not None and current.do_kb_data_source_uuid in identifiers:
            current.do_kb_data_source_uuid = None
            current.do_kb_indexed_at = None
            current.do_kb_index_status = None
        await session.commit()
        return True
    except Exception:
        await session.rollback()
        logger.warning(
            "DO KB unknown-write cleanup failed; intent retained",
            extra={"document_id": str(document_id)},
            exc_info=True,
        )
        return False
