"""Scheduled satellite-index reconciler (audit P2.3 / finding D1).

Ingestion fans documents out to satellite indexes — the Neo4j knowledge graph
and the DO Knowledge Base — on a best-effort basis: a satellite failure is
deliberately non-fatal and the document still reaches COMPLETED. P2.1 made
those outcomes visible (``documents.neo4j_index_status`` /
``documents.do_kb_sync_status``); this task is the missing second half:
recovery used to be manual-CLI-only (``scripts/repair_kg.py``,
``scripts/backfill_do_kb.py``), so drifted documents stayed drifted until a
human noticed.

Every 30 minutes (beat entry in ``celery_app.py``) it scans COMPLETED,
non-deleted documents whose either satellite status is ``'failed'``, plus
deleted documents whose DO KB data-source deletion or Neo4j subgraph cleanup
still needs retry. It
iterates org-by-org with a bounded keyset cursor and a hard per-run rate cap.

Two-stage safety (values-controllable, no image rebuild):

- ``RECONCILER_ENABLED`` (default true) — report-only: logs and returns what
  it WOULD re-drive, touching nothing.
- ``RECONCILER_APPLY`` (default false) — actually re-drives:
  Neo4j via the extracted idempotent core of the repair CLI
  (``src.services.knowledge_graph.repair.repair_document_graph``, upserts) and
  DO KB via the same ``sync_document_to_kb`` core the backfill CLI uses
  (idempotent: skips docs already carrying a data-source uuid, reuses an
  existing data source for the same key), with one org-level indexing kick
  after the batch instead of one per document.

Every action is logged with document id + organization id.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from celery import current_app
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, or_

from src.core.database import SessionLocal
from src.models.document import Document, ProcessingStatus
from src.services.knowledge_graph.repair import repair_document_graph
from src.shared.enums import SatelliteSyncStatus
from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app  # noqa: F401 - binds tasks to the app
from src.tasks.processing_tasks import _sync_document_to_kb_blocking

logger = logging.getLogger(__name__)

_FAILED = SatelliteSyncStatus.FAILED.value
_COMPLETED = SatelliteSyncStatus.COMPLETED.value


def _reconcilable_filters():
    """Filter list selecting reconcilable documents.

    Single source of truth shared by the count, the org listing, and the
    per-org batches — a drifted count would misreport the backlog (house
    rule: count queries must apply the same filters as the result query).

    Only COMPLETED active documents are re-driven: a PROCESSING document's
    pipeline is still running, and a FAILED document needs reprocessing.
    Deleted documents are selected separately only while a DO KB deletion
    handle remains or their Neo4j cleanup is marked failed.
    """
    return [
        or_(
            and_(
                Document.is_deleted == False,  # noqa: E712
                Document.processing_status == ProcessingStatus.COMPLETED,
                or_(
                    Document.neo4j_index_status == _FAILED,
                    Document.do_kb_sync_status == _FAILED,
                ),
            ),
            and_(
                Document.is_deleted == True,  # noqa: E712
                Document.do_kb_data_source_uuid.is_not(None),
                Document.do_kb_data_source_uuid != "",
            ),
            and_(
                Document.is_deleted == True,  # noqa: E712
                Document.neo4j_index_status == _FAILED,
            ),
        ),
    ]


async def _kick_do_kb_indexing(org_ids: set[str]) -> None:
    """One org-level DO KB indexing kick per touched org (mirrors backfill)."""
    from src.core.database import AsyncSessionLocal
    from src.services.do_kb.client import get_do_kb_client
    from src.services.do_kb.provisioner import ensure_kb_for_org

    api = get_do_kb_client()
    async with AsyncSessionLocal() as session:
        for org_id in org_ids:
            try:
                kb_uuid = await ensure_kb_for_org(session, org_id, client=api)
                await api.start_indexing(kb_uuid=kb_uuid)
                logger.info(
                    "reconciler: kicked DO KB indexing for org %s (kb %s)",
                    org_id,
                    kb_uuid,
                )
            except Exception as exc:  # noqa: BLE001 - kick is best-effort
                logger.warning(
                    "reconciler: DO KB indexing kick failed for org %s: %s",
                    org_id,
                    exc,
                )


class _LazyExtractionService:
    """Create the spaCy extraction service once per run, only if needed."""

    def __init__(self) -> None:
        self._service = None

    def get(self):
        if self._service is None:
            from src.services.processing.entity_extraction_service import (
                EntityExtractionService,
            )

            self._service = EntityExtractionService()
        return self._service


def _redrive_neo4j(document, extraction) -> bool:
    """Re-drive one document's Neo4j index; returns True when repaired."""
    outcome = repair_document_graph(document, extraction_service=extraction.get())
    if outcome.ok:
        document.neo4j_index_status = _COMPLETED
        document.neo4j_indexed_at = datetime.now(timezone.utc)
        logger.info(
            "reconciler: re-drove Neo4j for document %s (org %s): "
            "entities %s/%s, relationships %s",
            document.id,
            document.organization_id,
            outcome.entities_created,
            outcome.entities_found,
            outcome.relationships_created,
        )
        return True
    document.neo4j_index_status = _FAILED
    logger.warning(
        "reconciler: Neo4j re-drive still failing for document %s (org %s): "
        "errors=%s skipped=%s error=%s",
        document.id,
        document.organization_id,
        outcome.errors,
        outcome.skipped_reason,
        outcome.error_message,
    )
    return False


def _redrive_do_kb(document) -> bool:
    """Re-drive one document's DO KB sync; returns True when resynced.

    ``sync_document_to_kb`` is the same idempotent core the backfill CLI
    (``scripts/backfill_do_kb.py`` -> ``src.services.do_kb``) drives: it
    short-circuits documents already carrying a data-source uuid and reuses an
    existing data source for the same object key, so replays never duplicate.
    ``trigger_indexing=False`` — the caller batches one kick per org.
    """
    ds_uuid = _sync_document_to_kb_blocking(document, trigger_indexing=False)
    if ds_uuid:
        document.do_kb_sync_status = _COMPLETED
        logger.info(
            "reconciler: re-synced DO KB for document %s (org %s): ds=%s",
            document.id,
            document.organization_id,
            ds_uuid,
        )
        return True
    document.do_kb_sync_status = _FAILED
    logger.warning(
        "reconciler: DO KB re-sync still failing for document %s (org %s)",
        document.id,
        document.organization_id,
    )
    return False


def _cleanup_deleted_do_kb_document(document) -> bool:
    """Retry one soft-deleted document's DO KB data-source removal."""

    async def _run() -> bool:
        from src.core.database import AsyncSessionLocal
        from src.services.do_kb import unsync_document_from_kb

        async with AsyncSessionLocal() as session:
            current = await session.get(Document, document.id)
            if (
                current is None
                or not current.is_deleted
                or not current.do_kb_data_source_uuid
            ):
                return True
            return await unsync_document_from_kb(session, current)

    try:
        return run_async(_run())
    except Exception as exc:  # noqa: BLE001 - cleanup must remain retryable
        logger.warning(
            "reconciler: DO KB delete retry failed for document %s (org %s): %s",
            document.id,
            document.organization_id,
            exc,
        )
        return False


def _cleanup_deleted_document_graph(document) -> bool:
    """Remove a soft-deleted document's Neo4j subgraph and record the outcome.

    Sets ``neo4j_index_status`` to completed on success and failed otherwise;
    a failed row stays selected by ``_reconcilable_filters`` so the next run
    retries. Only deletes (relationships, then orphaned nodes, org-scoped);
    never re-indexes. The caller owns the commit.
    """
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    try:
        KnowledgeGraphService().delete_document_graph(
            str(document.id), str(document.organization_id)
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must remain retryable
        document.neo4j_index_status = _FAILED
        logger.warning(
            "reconciler: Neo4j cleanup failed for deleted document %s (org %s): %s",
            document.id,
            document.organization_id,
            exc,
        )
        return False
    document.neo4j_index_status = _COMPLETED
    return True


@current_app.task(name="src.tasks.reconcile_tasks.reconcile_satellite_indexes")
def reconcile_satellite_indexes() -> dict:
    """Beat task: reconcile documents whose satellite indexes drifted."""
    from src.core.config import get_settings

    settings_local = get_settings()
    if not settings_local.RECONCILER_ENABLED:
        logger.info("reconcile_satellite_indexes: skipped (RECONCILER_ENABLED=false)")
        return {"skipped": "reconciler-disabled"}

    apply_mode = bool(settings_local.RECONCILER_APPLY)
    cap = max(1, int(settings_local.RECONCILER_MAX_DOCS_PER_RUN))
    page_size = max(1, int(settings_local.RECONCILER_BATCH_SIZE))
    do_kb_enabled = bool(settings_local.DO_KB_ENABLED)

    summary: dict = {
        "mode": "apply" if apply_mode else "report-only",
        "eligible": 0,
        "scanned": 0,
        "kg_repaired": 0,
        "kg_still_failed": 0,
        "do_kb_resynced": 0,
        "do_kb_still_failed": 0,
        "do_kb_cleanup_succeeded": 0,
        "do_kb_cleanup_failed": 0,
        "kg_cleanup_succeeded": 0,
        "kg_cleanup_failed": 0,
        "do_kb_skipped_disabled": 0,
    }
    report: list[dict] = []
    orgs_to_kick: set[str] = set()
    extraction = _LazyExtractionService()

    db = SessionLocal()
    try:
        filters = _reconcilable_filters()
        summary["eligible"] = db.query(Document.id).filter(*filters).count()
        if summary["eligible"] == 0:
            logger.info("reconcile_satellite_indexes: nothing to reconcile")
            return summary

        # Org-scoped iteration: process one organization at a time, keyset
        # cursor on Document.id inside each org, global per-run rate cap.
        org_rows = (
            db.query(Document.organization_id)
            .filter(*filters)
            .distinct()
            .order_by(Document.organization_id)
            .all()
        )

        budget = cap
        try:
            for (org_id,) in org_rows:
                if budget <= 0:
                    break
                cursor = None
                while budget > 0:
                    query = (
                        db.query(Document)
                        .filter(*_reconcilable_filters())
                        .filter(Document.organization_id == org_id)
                    )
                    if cursor is not None:
                        # Keyset cursor — must be filtered before LIMIT.
                        query = query.filter(Document.id > cursor)
                    batch = (
                        query.order_by(Document.id).limit(min(page_size, budget)).all()
                    )
                    if not batch:
                        break

                    for doc in batch:
                        cursor = doc.id
                        budget -= 1
                        summary["scanned"] += 1
                        cleanup_pending = bool(
                            doc.is_deleted and doc.do_kb_data_source_uuid
                        )
                        graph_cleanup_pending = bool(
                            doc.is_deleted and doc.neo4j_index_status == _FAILED
                        )
                        needs_kg = (
                            not doc.is_deleted and doc.neo4j_index_status == _FAILED
                        )
                        needs_kb = (
                            not doc.is_deleted and doc.do_kb_sync_status == _FAILED
                        )

                        if not apply_mode:
                            logger.info(
                                "reconciler (report-only): would reconcile "
                                "document %s (org %s): neo4j=%s do_kb=%s",
                                doc.id,
                                doc.organization_id,
                                doc.neo4j_index_status,
                                doc.do_kb_sync_status,
                            )
                            report.append(
                                {
                                    "document_id": str(doc.id),
                                    "organization_id": str(doc.organization_id),
                                    "neo4j_index_status": doc.neo4j_index_status,
                                    "do_kb_sync_status": doc.do_kb_sync_status,
                                    "do_kb_cleanup_pending": cleanup_pending,
                                    "kg_cleanup_pending": graph_cleanup_pending,
                                }
                            )
                            continue

                        if cleanup_pending:
                            if not do_kb_enabled:
                                summary["do_kb_skipped_disabled"] += 1
                            elif _cleanup_deleted_do_kb_document(doc):
                                summary["do_kb_cleanup_succeeded"] += 1
                            else:
                                summary["do_kb_cleanup_failed"] += 1

                        if graph_cleanup_pending:
                            if _cleanup_deleted_document_graph(doc):
                                summary["kg_cleanup_succeeded"] += 1
                            else:
                                summary["kg_cleanup_failed"] += 1

                        if needs_kg:
                            if _redrive_neo4j(doc, extraction):
                                summary["kg_repaired"] += 1
                            else:
                                summary["kg_still_failed"] += 1

                        if needs_kb:
                            if not do_kb_enabled:
                                summary["do_kb_skipped_disabled"] += 1
                                logger.info(
                                    "reconciler: DO KB disabled — leaving "
                                    "document %s (org %s) do_kb_sync_status="
                                    "failed",
                                    doc.id,
                                    doc.organization_id,
                                )
                            elif _redrive_do_kb(doc):
                                summary["do_kb_resynced"] += 1
                                orgs_to_kick.add(str(doc.organization_id))
                            else:
                                summary["do_kb_still_failed"] += 1

                        # Commit per document so a crash preserves progress
                        # and a re-driven doc immediately drops out of the
                        # failed set.
                        db.commit()
        except SoftTimeLimitExceeded:
            db.rollback()
            summary["soft_time_limit"] = True
            logger.warning(
                "reconcile_satellite_indexes: soft time limit hit after %s docs",
                summary["scanned"],
            )

        if not apply_mode:
            summary["report"] = report

        if orgs_to_kick:
            run_async(_kick_do_kb_indexing(orgs_to_kick))

        logger.info("reconcile_satellite_indexes: %s", summary)
        return summary
    except Exception:
        db.rollback()
        logger.exception("reconcile_satellite_indexes failed")
        raise
    finally:
        db.close()
