"""Celery tasks for the pgvector document embeddings (GOO-435, Plan 07 S5a).

- ``embed_documents`` embeds the documents a finished ingest left behind. It is
  enqueued after the ingest has committed (``enqueue_document_embeddings``),
  so an embedding problem can never fail or delay document processing.
- ``backfill_document_embeddings`` embeds every completed document that has no
  ``document_embeddings`` row yet. Manual trigger only: it has no beat entry.

Both are no-ops unless ``DOCUMENT_EMBEDDINGS_ENABLED``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# A provider outage fails every document; stop the backfill rather than
# hammer it, and let the operator re-trigger it.
MAX_CONSECUTIVE_FAILURES = 10
_MAX_BATCH_SIZE = 500


def _zero_counts() -> dict[str, int]:
    return {"scanned": 0, "embedded": 0, "unchanged": 0, "skipped": 0, "failed": 0}


def enqueue_document_embeddings(
    organization_id: UUID | str, document_ids: list[str]
) -> bool:
    """Queue ``embed_documents`` once an ingest has committed; never raises.

    Returns whether a task was queued. A no-op unless
    ``DOCUMENT_EMBEDDINGS_ENABLED``.
    """
    from src.core.config import settings

    if not settings.DOCUMENT_EMBEDDINGS_ENABLED or not document_ids:
        return False
    try:
        embed_documents.delay(str(organization_id), [str(d) for d in document_ids])
    except Exception:  # noqa: BLE001 - the ingest has already succeeded
        logger.warning(
            "could not queue document embeddings: organization_id=%s count=%d",
            organization_id,
            len(document_ids),
            exc_info=True,
        )
        return False
    return True


async def _embed_documents(
    organization_id: str, document_ids: list[str]
) -> dict[str, int]:
    from sqlalchemy import select

    from src.core.config import settings
    from src.core.database import AsyncSessionLocal
    from src.models.document import Document
    from src.services.embedding.document_embedding_service import (
        document_embedding_service,
    )

    counts = _zero_counts()
    if not settings.DOCUMENT_EMBEDDINGS_ENABLED:
        return counts
    org = UUID(str(organization_id))
    async with AsyncSessionLocal() as session:
        for raw_id in document_ids:
            document = (
                await session.execute(
                    select(Document).where(
                        Document.id == UUID(str(raw_id)),
                        Document.organization_id == org,
                        Document.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            counts["scanned"] += 1
            if document is None:
                counts["skipped"] += 1
                continue
            outcome = await document_embedding_service.embed_document(session, document)
            counts[outcome.value if outcome.value in counts else "skipped"] += 1
            session.expunge_all()
    return counts


async def _backfill(batch_size: int, organization_id: str | None) -> dict[str, int]:
    from sqlalchemy import select

    from src.core.config import settings
    from src.core.database import AsyncSessionLocal
    from src.models.document import Document, ProcessingStatus
    from src.models.document_embedding import DocumentEmbedding
    from src.services.embedding.document_embedding_service import (
        EmbedOutcome,
        document_embedding_service,
    )

    counts = _zero_counts()
    if not settings.DOCUMENT_EMBEDDINGS_ENABLED:
        return counts
    page = max(1, min(int(batch_size), _MAX_BATCH_SIZE))
    org = None if organization_id is None else UUID(str(organization_id))
    last_id: UUID | None = None
    consecutive_failures = 0
    async with AsyncSessionLocal() as session:
        while consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            # Keyset pagination: a document that cannot be embedded stays
            # "lacking a row" and must not be fetched again by this run.
            candidates = (
                select(Document.id)
                .outerjoin(
                    DocumentEmbedding, DocumentEmbedding.document_id == Document.id
                )
                .where(
                    DocumentEmbedding.document_id.is_(None),
                    Document.is_deleted.is_(False),
                    Document.processing_status == ProcessingStatus.COMPLETED,
                )
                .order_by(Document.id)
                .limit(page)
            )
            if org is not None:
                candidates = candidates.where(Document.organization_id == org)
            if last_id is not None:
                candidates = candidates.where(Document.id > last_id)
            ids = list((await session.execute(candidates)).scalars().all())
            if not ids:
                break
            last_id = ids[-1]
            for document_id in ids:
                document = (
                    await session.execute(
                        select(Document).where(
                            Document.id == document_id,
                            Document.is_deleted.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                counts["scanned"] += 1
                if document is None:
                    counts["skipped"] += 1
                    continue
                outcome = await document_embedding_service.embed_document(
                    session, document
                )
                counts[outcome.value if outcome.value in counts else "skipped"] += 1
                # Keep memory flat: each Document carries its whole text.
                session.expunge_all()
                if outcome is EmbedOutcome.FAILED:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.warning(
                            "document embedding backfill stopped after %d consecutive "
                            "failures",
                            consecutive_failures,
                        )
                        break
                else:
                    consecutive_failures = 0
    logger.info("document embedding backfill finished: %s", counts)
    return counts


@celery_app.task(name="src.tasks.document_embedding_tasks.embed_documents")
def embed_documents(organization_id: str, document_ids: list[str]) -> dict[str, int]:
    """Embed the given documents of one organization (no-op while the flag is off)."""
    return run_async(_embed_documents(organization_id, document_ids))


@celery_app.task(name="src.tasks.document_embedding_tasks.backfill_document_embeddings")
def backfill_document_embeddings(
    batch_size: int = 100, organization_id: str | None = None
) -> dict[str, int]:
    """Embed completed documents that lack a ``document_embeddings`` row.

    ``batch_size`` rows are read per page (1 to 500); ``organization_id``
    limits the run to one organization. A no-op while
    ``DOCUMENT_EMBEDDINGS_ENABLED`` is off. Trigger it by hand::

        celery -A src.tasks.celery_app call \\
            src.tasks.document_embedding_tasks.backfill_document_embeddings
    """
    return run_async(_backfill(batch_size, organization_id))
