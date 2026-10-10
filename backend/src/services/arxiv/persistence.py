"""Tenant-safe durable persistence shared by every arXiv ingest entry point."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, cast
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document, ProcessingStatus

from .storage import delete_arxiv_storage, store_arxiv_pdf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArxivPersistenceResult:
    document_ids: list[str]
    reused_document_ids: set[str]
    kb_sync_failed: bool = False
    failed_papers: dict[str, str] = field(default_factory=dict)


def _arxiv_id(source_document: Any) -> Optional[str]:
    metadata = getattr(source_document, "document_metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("arxiv_id")
    return str(value).strip() if value else None


def _document_arxiv_id(document: Document) -> Optional[str]:
    value = document.arxiv_id
    return str(value).strip() if value else _arxiv_id(document)


def _arxiv_document_title(source_document: Any) -> str:
    metadata = getattr(source_document, "document_metadata", None)
    metadata_title = metadata.get("title") if isinstance(metadata, dict) else None
    title = metadata_title or getattr(source_document, "title", None)
    return " ".join(str(title or "Untitled").split())


def _has_durable_storage(document: Document) -> bool:
    """Return whether a row owns a usable durable source artifact."""
    if (
        document.processing_status != ProcessingStatus.COMPLETED
        or not str(document.content_text or "").strip()
        or not document.checksum_sha256
        or not document.file_size_bytes
    ):
        return False
    if document.storage_backend in {"s3", "supabase"}:
        return bool(document.storage_path)
    if document.storage_backend == "local":
        from src.core.config import settings

        return (
            settings.STORAGE_BACKEND == "local"
            and Path(str(document.file_path)).is_file()
        )
    return False


async def existing_arxiv_document(
    db: AsyncSession, organization_id: Any, arxiv_id: Optional[str]
) -> Optional[Document]:
    """Return the best live candidate for this tenant and exact revision."""
    if not arxiv_id:
        return None
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            or_(
                Document.arxiv_id == arxiv_id,
                Document.document_metadata["arxiv_id"].as_string() == arxiv_id,
            ),
            Document.is_deleted.is_(False),
        )
        .order_by(Document.created_at.asc(), Document.id.asc())
    )
    candidates = cast(list[Document], list((await db.execute(stmt)).scalars().all()))
    if not candidates:
        return None
    # A canonical row wins. Otherwise prefer the legacy row that already owns
    # durable storage, then the oldest row for stable in-place repair.
    return max(
        candidates,
        key=lambda document: (
            document.arxiv_id == arxiv_id,
            _has_durable_storage(document),
            document.search_vector is not None,
        ),
    )


async def existing_arxiv_document_id(
    db: AsyncSession, organization_id: Any, arxiv_id: Optional[str]
) -> Optional[Any]:
    """Compatibility wrapper returning only the selected document id."""
    document = await existing_arxiv_document(db, organization_id, arxiv_id)
    return document.id if document is not None else None


async def _document_for_update(
    db: AsyncSession, organization_id: Any, document_id: Any
) -> Optional[Document]:
    stmt = (
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == organization_id,
            Document.is_deleted.is_(False),
        )
        .with_for_update()
    )
    return cast(Optional[Document], (await db.execute(stmt)).scalar_one_or_none())


async def _canonical_arxiv_document(
    db: AsyncSession, organization_id: Any, arxiv_id: str
) -> Optional[Document]:
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.arxiv_id == arxiv_id,
            Document.is_deleted.is_(False),
        )
        .with_for_update()
        .limit(1)
    )
    return cast(Optional[Document], (await db.execute(stmt)).scalar_one_or_none())


def _repair_document(
    document: Document,
    source_document: Any,
    arxiv_id: str,
    storage_fields: dict[str, Any],
) -> None:
    document.arxiv_id = arxiv_id
    document.title = _arxiv_document_title(source_document)
    for name, value in storage_fields.items():
        setattr(document, name, value)
    source_text = getattr(source_document, "content_text", None)
    if str(source_text or "").strip():
        document.content_text = source_text
    source_summary = getattr(source_document, "content_summary", None)
    if source_summary is not None:
        document.content_summary = source_summary
    metadata = dict(document.document_metadata or {})
    metadata.update(jsonable_encoder(getattr(source_document, "document_metadata", {})))
    document.document_metadata = metadata
    document.processing_status = ProcessingStatus.COMPLETED
    document.processing_error = None


def _canonicalize_arxiv_identity(
    document: Document, source_document: Any, arxiv_id: str
) -> None:
    document.arxiv_id = arxiv_id
    metadata = dict(document.document_metadata or {})
    metadata.update(jsonable_encoder(getattr(source_document, "document_metadata", {})))
    document.document_metadata = metadata


def _managed_storage_fields(document: Document) -> Optional[dict[str, Any]]:
    backend = document.storage_backend
    if backend in {"s3", "supabase"} and document.storage_path:
        return {
            "storage_backend": backend,
            "storage_path": document.storage_path,
            "file_path": document.file_path,
        }
    if backend == "local" and Path(str(document.file_path)).is_file():
        return {
            "storage_backend": backend,
            "storage_path": None,
            "file_path": document.file_path,
        }
    return None


def _needs_kb_sync(document: Document) -> bool:
    from src.shared.enums import SatelliteSyncStatus

    return (
        not document.do_kb_data_source_uuid
        or document.do_kb_sync_status != SatelliteSyncStatus.COMPLETED.value
    )


async def existing_document(
    db: AsyncSession, organization_id: Any, checksum: Optional[str]
) -> Optional[Document]:
    """Lock and return this tenant's live document for the content checksum."""
    if not checksum:
        return None
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.checksum_sha256 == checksum,
            Document.is_deleted.is_(False),
        )
        .with_for_update()
        .limit(1)
    )
    return cast(Optional[Document], (await db.execute(stmt)).scalar_one_or_none())


async def existing_document_id(
    db: AsyncSession, organization_id: Any, checksum: Optional[str]
) -> Optional[Any]:
    """Compatibility wrapper returning only the checksum winner id."""
    document = await existing_document(db, organization_id, checksum)
    return document.id if document is not None else None


async def _delete_promoted(storage_fields: dict[str, Any]) -> None:
    await asyncio.to_thread(delete_arxiv_storage, storage_fields)


async def _cleanup_promoted(storage_items: Iterable[dict[str, Any]]) -> None:
    for storage_fields in reversed(list(storage_items)):
        try:
            await _delete_promoted(storage_fields)
        except Exception:  # noqa: BLE001 - preserve the original ingest failure
            logger.warning(
                "arxiv ingest rollback left an orphaned object: %s",
                storage_fields.get("storage_path") or storage_fields.get("file_path"),
                exc_info=True,
            )


async def persist_arxiv_documents(
    source_documents: Iterable[Any],
    *,
    user_id: Optional[str | UUID],
    organization_id: str | UUID,
) -> ArxivPersistenceResult:
    """Persist arXiv documents once, with durable storage and KB dual-write."""
    from src.core.database import AsyncSessionLocal

    sources = list(source_documents)
    document_ids_by_index: list[Optional[str]] = [None] * len(sources)
    reused_document_ids: set[str] = set()
    kb_document_ids: set[str] = set()
    failed_papers: dict[str, str] = {}
    promoted_storage: list[dict[str, Any]] = []
    staged_documents: list[
        tuple[int, Any, str, Optional[Any], UUID, dict[str, Any]]
    ] = []
    discarded_storage: list[dict[str, Any]] = []
    reuse_repairs: list[tuple[int, str, Any]] = []

    try:
        # Close the read transaction before potentially slow object-storage I/O.
        pending_sources: list[tuple[int, Any, str, Optional[Any]]] = []
        async with AsyncSessionLocal() as lookup_db:
            for index, source_document in enumerate(sources):
                arxiv_id = _arxiv_id(source_document)
                if not arxiv_id:
                    logger.warning("arxiv ingest skipped document without arxiv_id")
                    continue

                existing = await existing_arxiv_document(
                    lookup_db, organization_id, arxiv_id
                )
                if existing is not None and _has_durable_storage(existing):
                    document_id = str(existing.id)
                    document_ids_by_index[index] = document_id
                    reused_document_ids.add(document_id)
                    needs_write = (
                        existing.arxiv_id is None or existing.search_vector is None
                    )
                    if needs_write:
                        reuse_repairs.append((index, arxiv_id, existing.id))
                    elif _needs_kb_sync(existing):
                        kb_document_ids.add(document_id)
                    continue
                pending_sources.append(
                    (
                        index,
                        source_document,
                        arxiv_id,
                        existing.id if existing is not None else None,
                    )
                )

        # Promotion happens outside a database transaction. If a later write
        # fails, promoted_storage is the complete compensation manifest.
        for index, source_document, arxiv_id, existing_id in pending_sources:
            document_uuid = uuid4()
            promotion_task = asyncio.create_task(
                asyncio.to_thread(
                    store_arxiv_pdf,
                    source_document,
                    organization_id,
                    document_uuid,
                )
            )
            try:
                # Shield the worker thread so cancellation cannot abandon a
                # successful upload before it reaches the compensation list.
                storage_fields = await asyncio.shield(promotion_task)
            except asyncio.CancelledError as cancellation:
                try:
                    storage_fields = await asyncio.shield(promotion_task)
                except BaseException:
                    raise cancellation
                promoted_storage.append(storage_fields)
                raise cancellation
            except Exception:  # noqa: BLE001 - one bad paper must not lose the batch
                logger.warning(
                    "arxiv ingest storage promotion failed for %s",
                    arxiv_id,
                    exc_info=True,
                )
                failed_papers[arxiv_id] = "durable storage promotion failed"
                continue
            promoted_storage.append(storage_fields)
            staged_documents.append(
                (
                    index,
                    source_document,
                    arxiv_id,
                    existing_id,
                    document_uuid,
                    storage_fields,
                )
            )

        if staged_documents or reuse_repairs:
            async with AsyncSessionLocal() as db:
                async with db.begin():
                    vector_documents: dict[str, Document] = {}

                    for index, arxiv_id, document_id in reuse_repairs:
                        document = await _document_for_update(
                            db, organization_id, document_id
                        )
                        if document is None:
                            candidate = await existing_arxiv_document(
                                db, organization_id, arxiv_id
                            )
                            if candidate is not None:
                                document = await _document_for_update(
                                    db, organization_id, candidate.id
                                )
                        if document is None:
                            logger.warning(
                                "arxiv repair target disappeared: %s", arxiv_id
                            )
                            failed_papers[arxiv_id] = (
                                "document was removed before repair"
                            )
                            reused_document_ids.discard(str(document_id))
                            document_ids_by_index[index] = None
                            continue
                        try:
                            loser_id = str(document_id)
                            async with db.begin_nested():
                                document.arxiv_id = arxiv_id
                                await db.flush()
                        except IntegrityError:
                            winner = await _canonical_arxiv_document(
                                db, organization_id, arxiv_id
                            )
                            if winner is None:
                                raise
                            reused_document_ids.discard(loser_id)
                            reused_document_ids.add(str(winner.id))
                            document = winner
                            document_ids_by_index[index] = str(winner.id)
                        if document.search_vector is None:
                            vector_documents[str(document.id)] = document
                        if _needs_kb_sync(document):
                            kb_document_ids.add(str(document.id))

                    for (
                        index,
                        source_document,
                        arxiv_id,
                        repair_document_id,
                        document_uuid,
                        storage_fields,
                    ) in staged_documents:
                        document = None
                        if repair_document_id is not None:
                            document = await _document_for_update(
                                db, organization_id, repair_document_id
                            )
                        if document is None:
                            candidate = await existing_arxiv_document(
                                db, organization_id, arxiv_id
                            )
                            if candidate is not None:
                                document = await _document_for_update(
                                    db, organization_id, candidate.id
                                )

                        was_existing = document is not None
                        uses_promoted_storage = not (
                            document is not None and _has_durable_storage(document)
                        )
                        replaced_storage = (
                            _managed_storage_fields(document)
                            if document is not None and uses_promoted_storage
                            else None
                        )
                        is_new_document = document is None
                        if is_new_document:
                            checksum_winner = await existing_document(
                                db,
                                organization_id,
                                storage_fields.get("checksum_sha256"),
                            )
                            if checksum_winner is not None:
                                winner_arxiv_id = _document_arxiv_id(checksum_winner)
                                if winner_arxiv_id not in (None, arxiv_id):
                                    discarded_storage.append(storage_fields)
                                    failed_papers[arxiv_id] = (
                                        "content checksum conflicts with another "
                                        "arXiv revision"
                                    )
                                    continue
                                try:
                                    if _has_durable_storage(checksum_winner):
                                        if checksum_winner.arxiv_id is None:
                                            async with db.begin_nested():
                                                _canonicalize_arxiv_identity(
                                                    checksum_winner,
                                                    source_document,
                                                    arxiv_id,
                                                )
                                                await db.flush()
                                        discarded_storage.append(storage_fields)
                                    else:
                                        old_storage = _managed_storage_fields(
                                            checksum_winner
                                        )
                                        async with db.begin_nested():
                                            _repair_document(
                                                checksum_winner,
                                                source_document,
                                                arxiv_id,
                                                storage_fields,
                                            )
                                            await db.flush()
                                        if old_storage is not None:
                                            discarded_storage.append(old_storage)
                                        vector_documents[str(checksum_winner.id)] = (
                                            checksum_winner
                                        )
                                except IntegrityError:
                                    concurrent_exact = await _canonical_arxiv_document(
                                        db, organization_id, arxiv_id
                                    )
                                    if concurrent_exact is None:
                                        raise
                                    discarded_storage.append(storage_fields)
                                    if not _has_durable_storage(concurrent_exact):
                                        failed_papers[arxiv_id] = (
                                            "concurrent exact arXiv revision is "
                                            "not durably stored"
                                        )
                                        continue
                                    checksum_winner = concurrent_exact
                                if checksum_winner.search_vector is None:
                                    vector_documents[str(checksum_winner.id)] = (
                                        checksum_winner
                                    )
                                document_id = str(checksum_winner.id)
                                document_ids_by_index[index] = document_id
                                reused_document_ids.add(document_id)
                                if _needs_kb_sync(checksum_winner):
                                    kb_document_ids.add(document_id)
                                continue

                            document = Document(
                                id=document_uuid,
                                arxiv_id=arxiv_id,
                                title=_arxiv_document_title(source_document),
                                **storage_fields,
                                content_text=getattr(
                                    source_document, "content_text", None
                                ),
                                content_summary=getattr(
                                    source_document, "content_summary", None
                                ),
                                document_metadata=jsonable_encoder(
                                    getattr(source_document, "document_metadata", {})
                                ),
                                processing_status=ProcessingStatus.COMPLETED,
                                uploaded_by_user_id=user_id,
                                organization_id=organization_id,
                                is_public=False,
                            )
                        assert document is not None
                        try:
                            # Exact-revision and checksum races are contained to
                            # this paper; the outer batch remains committable.
                            async with db.begin_nested():
                                if is_new_document:
                                    db.add(document)
                                elif uses_promoted_storage:
                                    _repair_document(
                                        document,
                                        source_document,
                                        arxiv_id,
                                        storage_fields,
                                    )
                                else:
                                    document.arxiv_id = arxiv_id
                                await db.flush()
                        except IntegrityError:
                            winner = await _canonical_arxiv_document(
                                db, organization_id, arxiv_id
                            )
                            exact_winner = winner is not None
                            if winner is None:
                                winner = await existing_document(
                                    db,
                                    organization_id,
                                    storage_fields.get("checksum_sha256"),
                                )
                            if winner is None:
                                raise
                            if not exact_winner:
                                winner_arxiv_id = _document_arxiv_id(winner)
                                if winner_arxiv_id not in (None, arxiv_id):
                                    discarded_storage.append(storage_fields)
                                    failed_papers[arxiv_id] = (
                                        "content checksum conflicts with another "
                                        "arXiv revision"
                                    )
                                    continue
                            if _has_durable_storage(winner):
                                discarded_storage.append(storage_fields)
                                if not exact_winner and winner.arxiv_id is None:
                                    async with db.begin_nested():
                                        _canonicalize_arxiv_identity(
                                            winner, source_document, arxiv_id
                                        )
                                        await db.flush()
                            else:
                                old_storage = _managed_storage_fields(winner)
                                async with db.begin_nested():
                                    _repair_document(
                                        winner,
                                        source_document,
                                        arxiv_id,
                                        storage_fields,
                                    )
                                    await db.flush()
                                if old_storage is not None:
                                    discarded_storage.append(old_storage)
                                vector_documents[str(winner.id)] = winner
                            if winner.search_vector is None:
                                vector_documents[str(winner.id)] = winner
                            document_id = str(winner.id)
                            document_ids_by_index[index] = document_id
                            reused_document_ids.add(document_id)
                            if _needs_kb_sync(winner):
                                kb_document_ids.add(document_id)
                            continue

                        if replaced_storage is not None:
                            discarded_storage.append(replaced_storage)
                        document_ids_by_index[index] = str(document.id)
                        if was_existing:
                            reused_document_ids.add(str(document.id))
                        if uses_promoted_storage:
                            vector_documents[str(document.id)] = document
                        else:
                            discarded_storage.append(storage_fields)
                            if document.search_vector is None:
                                vector_documents[str(document.id)] = document
                        if _needs_kb_sync(document):
                            kb_document_ids.add(str(document.id))

                    if vector_documents:
                        from src.services.search.fulltext_search_service import (
                            fulltext_search_service,
                        )

                        await fulltext_search_service.async_update_document_search_vectors(
                            list(vector_documents), db
                        )

        # Committed rows own successful promotions. Clean only race/checksum
        # losers plus managed objects replaced by an in-place repair.
        promoted_storage[:] = discarded_storage
        await _cleanup_promoted(promoted_storage)
        promoted_storage.clear()
    except asyncio.CancelledError:
        await _cleanup_promoted(promoted_storage)
        raise
    except Exception:
        await _cleanup_promoted(promoted_storage)
        raise

    kb_sync_failed = False
    from src.core.config import settings

    if settings.DO_KB_ENABLED and kb_document_ids:
        from src.services.do_kb import sync_documents_to_kb
        from src.shared.enums import SatelliteSyncStatus

        try:
            async with AsyncSessionLocal() as kb_db:
                stmt = select(Document).where(
                    Document.id.in_(kb_document_ids),
                    Document.organization_id == organization_id,
                    Document.is_deleted.is_(False),
                )
                current_documents = cast(
                    list[Document], list((await kb_db.execute(stmt)).scalars().all())
                )
                for document in current_documents:
                    document.do_kb_sync_status = SatelliteSyncStatus.PENDING.value
                await kb_db.commit()

                try:
                    results = await sync_documents_to_kb(kb_db, current_documents)
                except Exception:
                    for document in current_documents:
                        document.do_kb_sync_status = SatelliteSyncStatus.FAILED.value
                    await kb_db.commit()
                    raise
                for document, data_source_uuid in zip(current_documents, results):
                    document.do_kb_sync_status = (
                        SatelliteSyncStatus.COMPLETED.value
                        if data_source_uuid
                        else SatelliteSyncStatus.FAILED.value
                    )
                await kb_db.commit()
                kb_sync_failed = any(result is None for result in results)
        except Exception as exc:  # noqa: BLE001 - PostgreSQL remains authoritative
            kb_sync_failed = True
            logger.warning("do_kb dual-write skipped for arxiv ingest: %s", exc)

    return ArxivPersistenceResult(
        document_ids=[
            document_id
            for document_id in document_ids_by_index
            if document_id is not None
        ],
        reused_document_ids=reused_document_ids,
        kb_sync_failed=kb_sync_failed,
        failed_papers=failed_papers,
    )
