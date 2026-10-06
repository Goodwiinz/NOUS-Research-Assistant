"""
Celery tasks for document processing
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import uuid4

from celery import Task, current_app
from sqlalchemy import select

from src.core.config import settings
from src.core.database import SessionLocal, get_db
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, ExtractionMethod
from src.models.graph import (
    BatchEntityRequest,
    CreateEntityRequest,
    CreateRelationshipRequest,
)
from src.models.graph import EntityType as GraphEntityType
from src.models.graph import ExtractionMethod as GraphExtractionMethod
from src.models.graph import RelationshipType as GraphRelationshipType
from src.models.processing import JobStatus, ProcessingJob
from src.services.documents.satellite_state import begin_write, finish_write
from src.services.knowledge_graph.knowledge_graph_service import knowledge_graph_service
from src.services.processing.llm_entity_extraction import LLMEntityExtractionService
from src.services.processing.processing_service import ProcessingPipeline
from src.services.search.fulltext_search_service import fulltext_search_service
from src.shared.enums import SatelliteSyncStatus
from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app
from src.tasks.processing_lifecycle import ProcessingStopped, require_active_ingestion
from src.tasks.replay_guard import claim_job_for_processing

logger = logging.getLogger(__name__)

# Extraction methods produced by the synchronous ingestion pipeline
# (ProcessingPipeline -> EntityExtractionService: spaCy NER + regex). Curated
# (MANUAL) and LLM (OPENAI) entities use other methods and must never be
# clobbered when this pipeline replays; scope the delete-before-insert to these.
_PIPELINE_EXTRACTION_METHODS = (ExtractionMethod.SPACY, ExtractionMethod.REGEX)


def _reset_pipeline_entities(db, document_id) -> int:
    """Delete this pipeline's own auto-extracted entities for a document.

    Makes the entity-extraction stage idempotent under acks_late redelivery: a
    crash mid-run can leave a partial spaCy/regex entity set from the previous
    attempt, so we drop those rows before re-inserting the fresh set
    (delete-before-insert, the same pattern the figure stage uses). Only rows
    written by this pipeline (SPACY/REGEX) are removed; MANUAL/OPENAI entities
    are preserved. Returns the number of rows deleted.
    """
    return (
        db.query(Entity)
        .filter(
            Entity.document_id == document_id,
            Entity.extraction_method.in_(_PIPELINE_EXTRACTION_METHODS),
        )
        .delete(synchronize_session=False)
    )


def _sync_document_to_kb_blocking(
    document, *, trigger_indexing: bool = True
) -> str | None:
    """Push a document to DO KB from a synchronous Celery task.

    DO KB (DigitalOcean Knowledge Base) is the retrieval backend after Qdrant
    was dropped. ``sync_document_to_kb`` is async and needs an async session,
    while these tasks hold a sync ``SessionLocal``, so it runs on a fresh
    ``AsyncSessionLocal`` against a freshly loaded, org-scoped copy of the row.
    It used to ``merge()`` the caller's object instead, and committing that
    wrote every stale loaded column back (e.g. reverting a sweep's FAILED
    status). The sync only owns the DO KB columns it sets (GOO-358).

    ``trigger_indexing=False`` registers the data source without kicking the
    org-level indexing job — the satellite reconciler uses it to batch one
    kick per org instead of one per re-driven document.

    Returns the data-source uuid, or ``None`` when DO KB is disabled or the
    sync fails. Never raises — ingestion must not fail on a KB outage.
    """

    async def _run() -> str | None:
        from src.core.database import AsyncSessionLocal
        from src.services.do_kb import sync_document_to_kb
        from src.services.do_kb.provisioner import ensure_kb_for_org

        data_source_uuid = None
        token = uuid4().hex
        async with AsyncSessionLocal() as kb_db:
            current = await kb_db.get(Document, document.id)
            if (
                current is None
                or current.organization_id != document.organization_id
                or current.is_deleted
                or not settings.DO_KB_ENABLED
            ):
                return None
            # Persist the KB mapping before dispatch, then a discoverable write
            # intent under the document lock. Neither provider call holds that
            # lock. A lost response/commit can be recovered by exact object key.
            await ensure_kb_for_org(kb_db, current.organization_id)
            await kb_db.commit()
            current = (
                await kb_db.execute(
                    select(Document)
                    .where(
                        Document.id == document.id,
                        Document.organization_id == document.organization_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True, autoflush=False)
                )
            ).scalar_one_or_none()
            if current is None or current.is_deleted:
                await kb_db.rollback()
                return None
            begin_write(current, "do_kb", token)
            await kb_db.commit()
            try:
                data_source_uuid = await sync_document_to_kb(
                    kb_db, current, trigger_indexing=trigger_indexing
                )
                indexed_at, index_status = (
                    current.do_kb_indexed_at,
                    current.do_kb_index_status,
                )
                # Drop the provider's ORM snapshot before writing just its
                # outcome to a fresh locked row; preserve concurrent deletion.
                await kb_db.rollback()
                current = (
                    await kb_db.execute(
                        select(Document)
                        .where(
                            Document.id == document.id,
                            Document.organization_id == document.organization_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True, autoflush=False)
                    )
                ).scalar_one_or_none()
                if current is not None and data_source_uuid:
                    current.do_kb_data_source_uuid = data_source_uuid
                    current.do_kb_indexed_at, current.do_kb_index_status = (
                        indexed_at,
                        index_status,
                    )
                    finish_write(current, "do_kb", token)
                await kb_db.commit()
            except Exception:
                await kb_db.rollback()
                logger.warning(
                    "DO KB sync or outcome persistence failed; durable cleanup intent retained",
                    exc_info=True,
                )
                # The remote identifier remains useful even when persistence
                # fails. The caller can compensate; reconciliation can discover
                # it independently if this process stops before compensation.
            return data_source_uuid

    try:
        return run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "DO KB sync failed for document %s: %s",
            getattr(document, "id", "?"),
            exc,
        )
        return None


def _index_entities_to_graph(document, entities) -> int:
    """Fan extracted entities out to the Neo4j knowledge graph, recording truth.

    Audit D1 (P2.1): this fan-out is best-effort by design — a Neo4j outage
    must never fail ingestion — but the outcome used to be a warn-log only, so
    a graph-drifted document was indistinguishable from a healthy one. This
    helper records the per-satellite outcome on the document instead:

    - ``neo4j_index_status='completed'`` (+ ``neo4j_indexed_at``) only when
      EVERY entity landed. ``create_entity_node`` swallows per-entity failures
      and returns ``None`` (it never raises on a Neo4j outage), so a partial
      or zero-indexed run is a real failure, not a success with a warn-log.
    - ``neo4j_index_status='failed'`` on any exception or any missed entity —
      the scheduled reconciler (``src.tasks.reconcile_tasks``) picks these up.

    Also fixes the fan-out itself: the pipeline hands over ORM ``Entity`` rows
    whose confidence column is ``confidence`` — the old inline code read
    ``entity.confidence_score`` (an attribute that does not exist), so the
    very first entity raised ``AttributeError`` and EVERY document's Neo4j
    fan-out failed into the warn-log. The new status column would have exposed
    that as 100% ``failed``; reading the real attribute makes success possible
    again.

    Never raises. Mutates ``document`` only; the caller owns the commit.
    Returns the number of entities actually indexed.
    """
    kg_indexed = 0
    try:
        for entity in entities:
            entity_id = knowledge_graph_service.create_entity_node(
                entity_text=entity.name,
                entity_type=(
                    entity.entity_type.value
                    if hasattr(entity.entity_type, "value")
                    else str(entity.entity_type)
                ),
                document_id=str(document.id),
                confidence=entity.confidence or 0.8,
                organization_id=(
                    str(document.organization_id) if document.organization_id else None
                ),
            )
            if entity_id:
                kg_indexed += 1
        logger.info(
            f"Indexed {kg_indexed}/{len(entities)} entities into Neo4j "
            f"for document {document.id}"
        )
        if kg_indexed == len(entities):
            document.neo4j_index_status = SatelliteSyncStatus.COMPLETED.value
            document.neo4j_indexed_at = datetime.now(timezone.utc)
        else:
            document.neo4j_index_status = SatelliteSyncStatus.FAILED.value
    except Exception as e:
        document.neo4j_index_status = SatelliteSyncStatus.FAILED.value
        logger.warning(
            f"Neo4j indexing failed for document {document.id}: {e}. "
            "Entities are still available in PostgreSQL; marked "
            "neo4j_index_status=failed for the reconciler."
        )
    return kg_indexed


def _record_do_kb_sync_outcome(document, ds_uuid) -> None:
    """Record the DO KB sync outcome on the document (audit D1 / P2.1).

    Only meaningful when DO KB is enabled: ``sync_document_to_kb`` returns
    ``None`` both when the feature is off (not an attempt — leave the column
    alone) and when an enabled sync failed (record ``failed`` so the
    reconciler can re-drive it). The ingestion call sites only invoke the sync
    for documents with ``content_text``, so an enabled ``None`` here is always
    a genuine failure (provisioning, canonical-text upload, data-source add,
    or persist), never a nothing-to-sync skip.

    Mutates ``document`` only; the caller owns the commit.
    """
    if ds_uuid:
        document.do_kb_sync_status = SatelliteSyncStatus.COMPLETED.value
    elif settings.DO_KB_ENABLED:
        document.do_kb_sync_status = SatelliteSyncStatus.FAILED.value


def _compensate_late_remote_writes(
    db, document_id, organization_id, writes: set[str]
) -> None:
    """Undo DO KB / Neo4j writes that finished after their document was deleted.

    The delete's own satellite cleanup ran before these writes existed, so
    nothing else would remove them. Cleanup is attempted now. If it fails, the
    retry marker stays durable on the deleted row, where the satellite
    reconciler picks it up: the data-source uuid for DO KB, or
    ``neo4j_index_status=failed`` for Neo4j. Never raises; the task is already
    stopping.
    """
    from src.tasks.reconcile_tasks import (
        _cleanup_deleted_do_kb_document,
        _cleanup_deleted_document_graph,
    )

    try:
        current = (
            db.query(Document)
            .filter(
                Document.id == document_id,
                Document.organization_id == organization_id,
            )
            .populate_existing()
            .first()
        )
        if current is None or not current.is_deleted:
            return
        if "graph" in writes:
            _cleanup_deleted_document_graph(current)
            # The graph outcome holds a short Document lock. Release it before
            # DO cleanup opens its independent session for the same document.
            db.commit()
        if "do_kb" in writes:
            _cleanup_deleted_do_kb_document(current)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "Cleanup of late satellite writes failed for deleted document %s",
            document_id,
            exc_info=True,
        )


class ProcessingTask(Task):
    """Base class for processing tasks"""

    def on_success(self, retval, task_id, args, kwargs):
        """Handle task success"""
        logger.info(f"Task {task_id} completed successfully")

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Handle task failure"""
        logger.error(f"Task {task_id} failed: {str(exc)}")

        # Update job status if this is a processing job
        if args and len(args) > 0:
            job_id = args[0]
            db = SessionLocal()
            try:
                job = (
                    db.query(ProcessingJob)
                    .filter(ProcessingJob.id == job_id)
                    .with_for_update()
                    .first()
                )

                # Never overwrite a finished job (cancelled, swept, already
                # failed by the task) or one a newer attempt has claimed.
                if (
                    job
                    and not job.is_finished
                    and job.celery_task_id in (None, task_id)
                ):
                    job.fail_job(str(exc))
                db.commit()
            except Exception as e:
                logger.error(f"Failed to update job status: {str(e)}")
            finally:
                db.close()


def _map_llm_entity_type_to_graph(llm_type: str) -> GraphEntityType:
    """Map LLM extraction type string to graph EntityType."""
    mapping = {
        "PERSON": GraphEntityType.PERSON,
        "ORGANIZATION": GraphEntityType.ORGANIZATION,
        "LOCATION": GraphEntityType.LOCATION,
        "CONCEPT": GraphEntityType.CONCEPT,
        "TECHNOLOGY": GraphEntityType.TECHNOLOGY,
        "RESEARCH": GraphEntityType.RESEARCH,
        "MODEL": GraphEntityType.PRODUCT,
        "DATASET": GraphEntityType.PRODUCT,
        "METHOD": GraphEntityType.CONCEPT,
        "METRIC": GraphEntityType.OTHER,
        "TOPIC": GraphEntityType.TOPIC,
        "EVENT": GraphEntityType.EVENT,
    }
    return mapping.get(llm_type.strip().upper(), GraphEntityType.OTHER)


def _safe_relationship_type(raw_type: str) -> GraphRelationshipType:
    if not raw_type:
        return GraphRelationshipType.RELATED_TO

    normalized = str(raw_type).strip().upper().replace("-", "_").replace(" ", "_")
    if not normalized:
        return GraphRelationshipType.RELATED_TO

    # Backward-compatible alias for extractor outputs.
    if normalized == "LEADS":
        normalized = "MANAGES"

    try:
        return GraphRelationshipType(normalized)
    except ValueError:
        return GraphRelationshipType.RELATED_TO


@current_app.task(base=ProcessingTask, bind=True, name="process_document_ingestion")
def process_document_ingestion(self, job_id: str):
    """Process complete document ingestion pipeline"""
    db = SessionLocal()
    # Remote satellites this attempt wrote to; cleaned up if the document turns
    # out to have been deleted while the write was in flight (GOO-358).
    remote_writes: set[str] = set()
    document_ids: tuple[Any, Any] | None = None
    try:
        # Get job and document
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()

        if not job:
            raise ValueError(f"Job {job_id} not found")

        document = (
            db.query(Document)
            .filter(Document.id == job.parameters["document_id"])
            .first()
        )

        if not document:
            raise ValueError(f"Document not found for job {job_id}")
        document_ids = (document.id, document.organization_id)

        # Atomic idempotency claim for acks_late redelivery. The Celery app sets
        # task_acks_late, so a worker killed mid-run never acks and the broker
        # redelivers this message. claim_job_for_processing serializes the
        # decision on a row lock: it short-circuits a job that already finished
        # (terminal) or is still running on another worker, and reclaims only one
        # whose worker died mid-run (stale RUNNING). A reclaim safely re-runs the
        # pipeline because every stage is idempotent — text/metadata are
        # overwritten, figure rows use delete-before-insert, DO KB sync reuses
        # the existing data source, the Neo4j path upserts, and the Postgres
        # entity writes below are replaced (not appended) per run.
        claim = claim_job_for_processing(
            db,
            job,
            worker_id=self.request.id,
            celery_task_id=self.request.id,
        )
        if not claim.proceed:
            logger.info(
                "Job %s not claimable (%s); skipping redelivered ingestion run",
                job_id,
                claim.reason,
            )
            return {
                "status": job.status.value,
                "document_id": str(document.id),
                "skipped": claim.reason,
            }

        # Every stage commit re-checks, under Document -> ProcessingJob row
        # locks, that this attempt is still the live one: the document can be
        # deleted, the job cancelled or swept, or a retry started while this
        # worker parses or calls models. A stale attempt rolls back instead of
        # writing (GOO-357). Slow work stays outside the locks.
        def commit_if_active() -> None:
            require_active_ingestion(
                db, job_id, expected_task_id=self.request.id, lock=True
            )
            db.commit()

        # Initialize processing service
        processing_service = ProcessingPipeline(db)

        # Update document status (claim already moved the job to RUNNING).
        document.update_processing_status(ProcessingStatus.PROCESSING)
        commit_if_active()

        # Step 1: Text Extraction
        job.update_progress("Extracting text content", 20)
        commit_if_active()

        # Run async function in a managed event loop
        text_extraction_result = asyncio.run(
            processing_service.process_text_extraction(document)
        )

        if text_extraction_result["text_content"]:
            document.content_text = text_extraction_result["text_content"]
            document.content_summary = text_extraction_result["summary"]

            # Add metadata
            document.add_metadata("word_count", text_extraction_result["word_count"])
            document.add_metadata(
                "character_count", text_extraction_result["character_count"]
            )
        commit_if_active()

        # Step 1b: Figure extraction (optional, flag-gated; never fails ingestion)
        if (
            settings.FIGURE_EXTRACTION_ENABLED
            and document.document_type == DocumentType.PDF
        ):
            try:
                from src.services.processing.figure_extraction_service import (
                    extract_figures_for_document,
                    merge_captions_into_text,
                )

                job.update_progress("Extracting figures", 35)
                commit_if_active()
                fig_result = extract_figures_for_document(db, document)
                if fig_result.get("captions_text"):
                    document.content_text = merge_captions_into_text(
                        document.content_text, fig_result["captions_text"]
                    )
                commit_if_active()
            except ProcessingStopped:
                raise
            except Exception as fig_err:  # optional step, mirrors Neo4j tolerance above
                db.rollback()
                logger.warning(
                    f"Figure extraction failed for document {document.id}: {fig_err}"
                )

        # Step 2: Entity Extraction
        job.update_progress("Extracting entities", 50)
        commit_if_active()

        if document.content_text:
            # Run entity extraction in event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                entities = loop.run_until_complete(
                    processing_service.process_entity_extraction(
                        document, document.content_text
                    )
                )
            finally:
                loop.close()

            # Replay-safe write: drop any spaCy/regex entities left by a prior
            # crashed run for this document before inserting the fresh set, so an
            # acks_late reclaim replaces rather than duplicates them. The delete
            # and the inserts commit in one transaction (all-or-nothing). The
            # DELETE runs immediately, so take the guard's Document -> Job locks
            # first: a delete cascading to these entities must not wait on this
            # worker while the worker waits on the delete's Document lock.
            require_active_ingestion(
                db, job_id, expected_task_id=self.request.id, lock=True
            )
            _reset_pipeline_entities(db, document.id)
            for entity in entities:
                db.add(entity)
            commit_if_active()

            # Index entities into Neo4j knowledge graph. Best-effort (a Neo4j
            # outage never fails ingestion) but no longer silent: the helper
            # records neo4j_index_status=completed/failed on the document
            # (audit D1) so the reconciler can re-drive drifted rows. Stamp
            # 'pending' first so a worker killed mid-fan-out is visible too.
            job.update_progress("Indexing knowledge graph", 60)
            document.neo4j_index_status = SatelliteSyncStatus.PENDING.value
            commit_if_active()

            require_active_ingestion(
                db, job_id, expected_task_id=self.request.id, lock=True
            )
            graph_token = uuid4().hex
            begin_write(document, "graph", graph_token)
            db.commit()
            remote_writes.add("graph")
            try:
                _index_entities_to_graph(document, entities)
            finally:
                # SIGKILL cannot reach this acknowledgement. Its token then
                # keeps successful earlier cleanup from hiding the late write.
                with SessionLocal() as outcome_db:
                    current = (
                        outcome_db.query(Document)
                        .filter(
                            Document.id == document.id,
                            Document.organization_id == document.organization_id,
                        )
                        .populate_existing()
                        .with_for_update()
                        .one_or_none()
                    )
                    if current is not None:
                        finish_write(current, "graph", graph_token)
                        if (
                            current.is_deleted
                            and (current.document_metadata or {}).get(
                                "graph_cleanup_requested"
                            )
                            is not False
                        ):
                            current.neo4j_index_status = (
                                SatelliteSyncStatus.PENDING.value
                            )
                        outcome_db.commit()
                db.refresh(document, attribute_names=["document_metadata"])
            commit_if_active()

        # Step 3: Embedding Generation — push the document to DO KB, the
        # retrieval backend (replaces the dead Qdrant write). Idempotent and
        # gated by DO_KB_ENABLED; a None result (KB disabled/outage) is a clean
        # no-op so ingestion still completes — but the outcome is recorded in
        # do_kb_sync_status (audit D1) so a failed sync is reconcilable.
        job.update_progress("Generating embeddings", 75)
        commit_if_active()

        if document.content_text:
            if settings.DO_KB_ENABLED:
                # Visible in-flight marker; overwritten by the outcome below.
                document.do_kb_sync_status = SatelliteSyncStatus.PENDING.value
                commit_if_active()
            ds_uuid = _sync_document_to_kb_blocking(document)
            if ds_uuid:
                remote_writes.add("do_kb")
                document.is_embedded = True
            _record_do_kb_sync_outcome(document, ds_uuid)
        commit_if_active()

        # Step 4: Generate Search Vector
        job.update_progress("Creating search index", 90)
        commit_if_active()

        # Update search vector for full-text search. The service commits its own
        # raw UPDATE of search_vector only (no stale ORM state); the guarded
        # finalize below still refuses to complete a lost attempt.
        search_vector_ok = False
        try:
            fulltext_search_service.update_document_search_vector(str(document.id), db)
            search_vector_ok = True
            logger.info(f"Updated search vector for document {document.id}")
        except Exception as e:
            logger.warning(
                f"Failed to update search vector for document {document.id}: {e}"
            )

        # Step 5: Finalize
        job.update_progress("Finalizing", 95)

        # Progress, document and job complete in one guarded commit. Check
        # before mutating status so the flush cannot mask a cancellation.
        require_active_ingestion(
            db, job_id, expected_task_id=self.request.id, lock=True
        )

        # Mark document as processed. is_indexed must reflect ACTUAL full-text
        # searchability: if the search-vector build above failed the document has
        # no tsvector and the primary full-text retrieval can never surface it —
        # claiming is_indexed=True there is a silent partial index (the doc looks
        # ready but is unreachable). Tie the flag to the real outcome so a
        # re-index can be triggered for the not-yet-searchable docs.
        document.update_processing_status(ProcessingStatus.COMPLETED)
        document.is_indexed = search_vector_ok
        job.complete_job(
            result={
                "text_extracted": bool(document.content_text),
                "entities_found": len(entities) if "entities" in locals() else 0,
                # is_embedded is the source of truth (set after DO KB sync).
                # embedding_id is the dead Qdrant vector-id column (always NULL
                # now), so bool(embedding_id) always reported False.
                "embedding_generated": bool(document.is_embedded),
                "search_indexed": search_vector_ok,
                "word_count": text_extraction_result.get("word_count", 0),
            }
        )
        db.commit()

        logger.info(f"Successfully processed document {document.id}")

        return {
            "status": "completed",
            "document_id": str(document.id),
            "processing_time": job.duration_seconds,
        }

    except ProcessingStopped as stop:
        # Cancelled, deleted, swept or superseded mid-run: whoever did that
        # already wrote the state, so drop this attempt's writes and stop.
        db.rollback()
        logger.info(
            "Ingestion job %s stopped mid-run (%s); discarding stale writes",
            job_id,
            stop.reason,
        )
        if stop.reason == "deleted" and remote_writes and document_ids:
            _compensate_late_remote_writes(db, *document_ids, remote_writes)
        return {"status": "stopped", "job_id": job_id, "skipped": stop.reason}

    except Exception as e:
        logger.error(f"Document ingestion failed for job {job_id}: {str(e)}")

        # Roll back first: if the failure came from a DB op the session is
        # poisoned (PendingRollbackError), so the queries/commit below to write
        # the FAILED status would themselves throw and get swallowed — leaving
        # the document stuck in PROCESSING (never FAILED), which then blocks
        # content-hash dedup from ever re-uploading it.
        db.rollback()

        # Record the failure only if this attempt still owns the job; never
        # overwrite a cancellation, deletion, sweep or newer attempt.
        try:
            try:
                job, document = require_active_ingestion(
                    db, job_id, expected_task_id=self.request.id, lock=True
                )
            except ProcessingStopped as stop:
                db.rollback()
                # A remote write may have landed before the document was
                # deleted and the stage then failed for an unrelated reason.
                if stop.reason == "deleted" and remote_writes and document_ids:
                    _compensate_late_remote_writes(db, *document_ids, remote_writes)
            else:
                document.update_processing_status(ProcessingStatus.FAILED, str(e))
                job.fail_job(str(e))
                db.commit()

        except Exception as update_error:
            logger.error(f"Failed to update failure status: {str(update_error)}")

        raise

    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="extract_text_content")
def extract_text_content(self, job_id: str):
    """Extract text content from document"""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()

        if not job:
            raise ValueError(f"Job {job_id} not found")

        document = (
            db.query(Document)
            .filter(Document.id == job.parameters["document_id"])
            .first()
        )

        if not document:
            raise ValueError(f"Document not found for job {job_id}")

        # Start job
        job.start_job(worker_id=self.request.id)
        db.commit()

        # Extract text. process_text_extraction is async — run it in a managed
        # event loop (mirrors the process_document task and extract_entities).
        # Calling it without asyncio.run returned a coroutine, so the next line
        # (result["text_content"]) raised "'coroutine' object is not
        # subscriptable" and this task failed on every document.
        processing_service = ProcessingPipeline(db)
        result = asyncio.run(processing_service.process_text_extraction(document))

        # Update document
        document.content_text = result["text_content"]
        document.content_summary = result["summary"]

        # Add metadata
        document.add_metadata("word_count", result["word_count"])
        document.add_metadata("character_count", result["character_count"])
        db.commit()

        # Complete job
        job.complete_job(result=result)
        db.commit()

        return result

    except Exception as e:
        logger.error(f"Text extraction failed for job {job_id}: {str(e)}")
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if job:
            job.fail_job(str(e))
            db.commit()
        raise

    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="extract_entities")
def extract_entities(self, job_id: str):
    """Extract entities from document text"""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()

        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Idempotency guard for acks_late redelivery (celery_app sets
        # task_acks_late=True): a worker killed after completion but before the
        # broker ack redelivers the SAME message. Without this the task re-runs
        # the full paid LLM extraction AND appends a SECOND copy of every entity
        # (OPENAI rows are deliberately never delete-before-inserted, so the
        # duplicates accumulate), while regressing the job COMPLETED -> RUNNING.
        # Same short-circuit as kg_extract_entities_job / kg_merge_entities_job.
        if job.status == JobStatus.COMPLETED:
            logger.info(
                f"Job {job_id} already completed; skipping redelivered "
                "entity extraction"
            )
            return {
                "status": "completed",
                "job_id": job_id,
                "skipped": "duplicate_delivery",
            }

        document = (
            db.query(Document)
            .filter(Document.id == job.parameters["document_id"])
            .first()
        )

        if not document:
            raise ValueError(f"Document not found for job {job_id}")

        if not document.content_text:
            raise ValueError("No text content available for entity extraction")

        # Start job
        job.start_job(worker_id=self.request.id)
        db.commit()

        # Extract entities. asyncio.run creates + manages a fresh loop;
        # get_event_loop().run_until_complete raises "no current event loop"
        # / deprecation on Python 3.10+ in a worker thread.
        service = LLMEntityExtractionService()
        extraction_result = asyncio.run(service.extract_entities(document.content_text))

        # A timeout that skips chunks (or all-chunks-failed) sets
        # ExtractionResult.error while still returning the entities found so far.
        # Surface it instead of reporting unqualified success.
        if extraction_result.error:
            logger.warning(
                "Entity extraction for document %s was partial: %s",
                document.id,
                extraction_result.error,
            )

        # Save entities
        from datetime import datetime

        from src.models.entity import Entity, ExtractionMethod
        from src.services.agent.llm_factory import get_lightweight_model_name

        # R2-M14: make the LLM write idempotent for ANY redelivery (not just
        # post-COMPLETED acks): a worker killed mid-run leaves the job RUNNING
        # and the COMPLETED short-circuit above never fires, so the retry
        # appended a second copy of every OPENAI entity. Delete-before-insert,
        # scoped to this document + OPENAI method only.
        db.query(Entity).filter(
            Entity.document_id == document.id,
            Entity.extraction_method == ExtractionMethod.OPENAI,
        ).delete(synchronize_session=False)
        db.commit()

        from src.services.processing.llm_entity_extraction import map_to_entity_type

        # Record the deployment that actually ran, not a hardcoded tier name —
        # the stored "gpt-5-nano" string went stale when tiering changed.
        extraction_model = get_lightweight_model_name()
        saved_entities = []
        for ent in extraction_result.entities:
            entity = Entity(
                entity_type=map_to_entity_type(ent.type),
                name=ent.name,
                canonical_name=ent.canonical_name,
                aliases=ent.aliases,
                description=ent.description,
                confidence=ent.confidence,
                extraction_method=ExtractionMethod.OPENAI,
                extracted_at=datetime.utcnow(),
                extraction_model=extraction_model,
                document_id=document.id,
                organization_id=document.organization_id,
            )
            db.add(entity)
            saved_entities.append(entity)
        db.commit()

        # Complete job
        job.complete_job(
            result={
                "entities_extracted": len(saved_entities),
                "entity_types": list(
                    set(ent.type for ent in extraction_result.entities)
                ),
                "extraction_error": extraction_result.error,
                "partial": bool(extraction_result.error),
            }
        )
        db.commit()

        return {
            "entities_extracted": len(saved_entities),
            "entities": [e.to_dict() for e in saved_entities],
        }

    except Exception as e:
        logger.error(f"Entity extraction failed for job {job_id}: {str(e)}")
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if job:
            job.fail_job(str(e))
            db.commit()
        raise

    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="generate_embeddings")
def generate_embeddings(self, job_id: str):
    """Generate embeddings for document"""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()

        if not job:
            raise ValueError(f"Job {job_id} not found")

        document = (
            db.query(Document)
            .filter(Document.id == job.parameters["document_id"])
            .first()
        )

        if not document:
            raise ValueError(f"Document not found for job {job_id}")

        if not document.content_text:
            raise ValueError("No text content available for embedding generation")

        # Start job
        job.start_job(worker_id=self.request.id)
        db.commit()

        # Push the document to DO KB (retrieval backend; replaces dead Qdrant).
        if settings.DO_KB_ENABLED:
            # Visible in-flight marker; overwritten by the outcome below.
            document.do_kb_sync_status = SatelliteSyncStatus.PENDING.value
            db.commit()
        ds_uuid = _sync_document_to_kb_blocking(document)

        # Record the per-satellite outcome (audit D1): completed on success,
        # failed when DO KB is enabled but the sync returned nothing, untouched
        # when the feature is disabled (never attempted).
        _record_do_kb_sync_outcome(document, ds_uuid)

        # Update document
        if ds_uuid:
            document.is_embedded = True
            db.commit()

            # Complete job
            job.complete_job(result={"do_kb_data_source_uuid": ds_uuid})
            db.commit()

            return {"do_kb_data_source_uuid": ds_uuid, "status": "success"}
        else:
            # DO KB disabled or returned nothing — not a task failure. Complete
            # the job cleanly rather than raising (there is no other embedding
            # backend now that Qdrant is gone); do_kb_sync_status carries the
            # truth for the reconciler when the KB is enabled.
            job.complete_job(
                result={"status": "skipped", "reason": "do_kb_unavailable"}
            )
            db.commit()
            return {"status": "skipped", "reason": "do_kb_unavailable"}

    except Exception as e:
        logger.error(f"Embedding generation failed for job {job_id}: {str(e)}")
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if job:
            job.fail_job(str(e))
            db.commit()
        raise

    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="index_in_graph")
def index_in_graph(self, job_id: str):
    """Index document and entities in knowledge graph"""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()

        if not job:
            raise ValueError(f"Job {job_id} not found")

        document = (
            db.query(Document)
            .filter(Document.id == job.parameters["document_id"])
            .first()
        )

        if not document:
            raise ValueError(f"Document not found for job {job_id}")

        # Start job
        job.start_job(worker_id=self.request.id)
        db.commit()

        # Get entities for this document
        entities = db.query(Entity).filter(Entity.document_id == document.id).all()

        # Index in Neo4j (this would be implemented with actual Neo4j client)
        # For now, simulate the process
        indexed_entities = []
        for entity in entities:
            # Simulate graph indexing
            entity.is_in_knowledge_graph = True
            entity.graph_id = f"neo4j_node_{entity.id}"
            indexed_entities.append(entity)

        db.commit()

        # Mark document as indexed
        document.is_indexed = True
        db.commit()

        # Complete job
        job.complete_job(
            result={"entities_indexed": len(indexed_entities), "document_indexed": True}
        )
        db.commit()

        return {
            "entities_indexed": len(indexed_entities),
            "document_indexed": True,
            "status": "success",
        }

    except Exception as e:
        logger.error(f"Graph indexing failed for job {job_id}: {str(e)}")
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if job:
            job.fail_job(str(e))
            db.commit()
        raise

    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="kg_extract_entities_job")
def kg_extract_entities_job(self, job_id: str):
    """Extract entities from a document and index them into knowledge graph."""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Idempotency guard for acks_late redelivery (same pattern as
        # process_document_ingestion above): a worker killed after completion
        # but before the broker ack redelivers the message, which would re-run
        # the full LLM extraction and regress the job COMPLETED -> RUNNING.
        if job.status == JobStatus.COMPLETED:
            logger.info(
                f"Job {job_id} already completed; skipping redelivered KG extraction"
            )
            return {
                "status": "completed",
                "job_id": job_id,
                "skipped": "duplicate_delivery",
            }

        job.start_job(worker_id=self.request.id, celery_task_id=self.request.id)
        db.commit()

        document_ids = []
        if job.parameters:
            if job.parameters.get("document_ids"):
                document_ids = [
                    str(doc_id) for doc_id in job.parameters.get("document_ids", [])
                ]
            elif job.parameters.get("document_id"):
                document_ids = [str(job.parameters.get("document_id"))]
        if not document_ids:
            raise ValueError("Missing document_id(s) in job parameters")

        entities_found_total = 0
        entities_created_total = 0
        relationships_found_total = 0
        relationships_created_total = 0
        errors_total = 0
        extraction_errors_total = 0

        total_docs = len(document_ids)
        for index, document_id in enumerate(document_ids):
            document = db.query(Document).filter(Document.id == document_id).first()
            if not document:
                errors_total += 1
                continue

            content = document.content_text or ""
            if not content.strip():
                errors_total += 1
                continue

            job.update_progress(
                f"Extracting entities ({index + 1}/{total_docs})",
                min(80.0, ((index + 1) / max(total_docs, 1)) * 70.0),
            )
            db.commit()

            service = LLMEntityExtractionService()
            extraction_result = asyncio.run(
                service.extract_entities(content, timeout_seconds=300.0)
            )
            if extraction_result.error:
                extraction_errors_total += 1
                logger.warning(
                    "Entity extraction partial for document %s in KG job: %s",
                    document.id,
                    extraction_result.error,
                )

            extracted_entities = extraction_result.entities
            extracted_relationships = extraction_result.relationships
            entities_found_total += len(extracted_entities)
            relationships_found_total += len(extracted_relationships)

            create_requests = []
            for ent in extracted_entities:
                create_requests.append(
                    CreateEntityRequest(
                        name=ent.name,
                        entity_type=_map_llm_entity_type_to_graph(ent.type),
                        confidence_score=min(1.0, max(0.0, ent.confidence)),
                        extraction_method=GraphExtractionMethod.LLM_EXTRACTION,
                        position=None,
                        context=ent.description,
                        metadata={
                            "source": "background_extraction_job",
                            "document_id": str(document.id),
                            "aliases": ent.aliases,
                        },
                        source_document_id=str(document.id),
                        organization_id=(
                            str(document.organization_id)
                            if document.organization_id
                            else None
                        ),
                    )
                )

            entity_result = knowledge_graph_service.create_entities_batch(
                BatchEntityRequest(
                    entities=create_requests,
                    relationships=[],
                    upsert=True,
                    document_id=str(document.id),
                )
            )

            entities_created_total += len(entity_result.created_entities)
            errors_total += len(entity_result.errors)

            # Map created graph node IDs by entity name. Relationships from the
            # LLM extractor reference entity names (already remapped to the kept
            # merged entity's name in _resolve_relationships), so a name lookup
            # is sufficient to resolve endpoints to the nodes just created.
            created_id_by_name = {}
            for created in entity_result.created_entities:
                created_id_by_name[created.name.strip().lower()] = created.id

            relationship_requests = []
            for relationship in extracted_relationships:
                source_entity_id = created_id_by_name.get(
                    relationship.source.strip().lower()
                )
                target_entity_id = created_id_by_name.get(
                    relationship.target.strip().lower()
                )
                if not source_entity_id or not target_entity_id:
                    continue

                confidence = min(1.0, max(0.0, float(relationship.confidence)))
                evidence = relationship.evidence
                relationship_requests.append(
                    CreateRelationshipRequest(
                        source_entity_id=source_entity_id,
                        target_entity_id=target_entity_id,
                        relationship_type=_safe_relationship_type(
                            relationship.relationship_type
                        ),
                        strength=confidence,
                        confidence_score=confidence,
                        context=evidence,
                        evidence=[evidence] if evidence else [],
                        metadata={
                            "source": "background_extraction_job",
                            "document_id": str(document.id),
                        },
                        source_document_id=str(document.id),
                        organization_id=(
                            str(document.organization_id)
                            if document.organization_id
                            else None
                        ),
                    )
                )

            if relationship_requests:
                relationship_result = knowledge_graph_service.create_entities_batch(
                    BatchEntityRequest(
                        entities=[],
                        relationships=relationship_requests,
                        upsert=True,
                        document_id=str(document.id),
                    )
                )
                relationships_created_total += len(
                    relationship_result.created_relationships
                )
                errors_total += len(relationship_result.errors)

        job.update_progress("Finalizing extraction job", 90)

        result = {
            "document_ids": document_ids,
            "entities_found": entities_found_total,
            "entities_created": entities_created_total,
            "relationships_found": relationships_found_total,
            "relationships_created": relationships_created_total,
            "errors": errors_total,
            "extraction_errors": extraction_errors_total,
        }

        # Honest outcome: if NOTHING was created and something errored (all
        # documents missing/empty, or every KG write failed), completing the job
        # reports a false success to anything polling job status. Fail it so the
        # total failure is visible; a legitimately-empty run (no creates, no
        # errors) still completes.
        total_created = entities_created_total + relationships_created_total
        total_errors = errors_total + extraction_errors_total
        if total_created == 0 and total_errors > 0:
            job.fail_job(
                f"KG extraction created no entities or relationships "
                f"({total_errors} errors across {total_docs} documents)",
                error_type="extraction_failed",
            )
            db.commit()
            return {"status": "failed", "job_id": job_id, "result": result}

        job.complete_job(result=result)
        db.commit()
        return {"status": "completed", "job_id": job_id}
    except Exception as e:
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if "job" in locals() and job:
            job.fail_job(str(e), error_type=type(e).__name__)
            db.commit()
        raise
    finally:
        db.close()


@current_app.task(base=ProcessingTask, bind=True, name="kg_merge_entities_job")
def kg_merge_entities_job(self, job_id: str):
    """Merge duplicate entity groups into suggested primary entities."""
    db = SessionLocal()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Idempotency guard for acks_late redelivery (same pattern as
        # kg_extract_entities_job): a worker killed after completion but before
        # the broker ack redelivers the message. Re-running would regress the
        # job COMPLETED -> RUNNING and re-process every group — re-deleting
        # already-merged duplicate nodes and duplicating re-pointed edges.
        if job.status == JobStatus.COMPLETED:
            logger.info(
                f"Job {job_id} already completed; skipping redelivered entity merge"
            )
            return {
                "status": "completed",
                "job_id": job_id,
                "skipped": "duplicate_delivery",
            }

        groups = (job.parameters or {}).get("groups", [])
        if not groups:
            raise ValueError("No groups provided for merge job")

        # Tenant scope for every KG read/write below. Entity ids are org-validated
        # at enqueue, but the KG service itself is only tenant-safe when the org
        # is passed through.
        job_org_id = str(job.organization_id) if job.organization_id else None

        job.start_job(worker_id=self.request.id, celery_task_id=self.request.id)
        db.commit()

        success_count = 0
        failure_count = 0

        total_groups = len(groups)
        for index, group in enumerate(groups):
            group_entities = group.get("entities", [])
            primary_id = group.get("suggested_primary")
            if not primary_id or not group_entities:
                failure_count += 1
                continue

            duplicate_ids = [
                e.get("id")
                for e in group_entities
                if e.get("id") and e.get("id") != primary_id
            ]
            step_message = f"Merging group {index + 1}/{total_groups}"
            job.update_progress(
                step_message, min(90.0, ((index + 1) / max(total_groups, 1)) * 85.0)
            )
            db.commit()

            try:
                merged_all_duplicates = True
                for duplicate_id in duplicate_ids:
                    # Scope every KG read/write to the job's org: get_relationships
                    # and delete_entity are otherwise unscoped and would
                    # read/DETACH DELETE any entity by id across tenants, and a
                    # re-pointed edge created without organization_id lands
                    # org-less, regressing edge-level tenancy.
                    relationships = knowledge_graph_service.get_relationships(
                        duplicate_id, organization_id=job_org_id
                    )
                    for rel in relationships:
                        create_request = CreateRelationshipRequest(
                            source_entity_id=(
                                primary_id
                                if rel.source_entity_id == duplicate_id
                                else rel.source_entity_id
                            ),
                            target_entity_id=(
                                primary_id
                                if rel.target_entity_id == duplicate_id
                                else rel.target_entity_id
                            ),
                            relationship_type=_safe_relationship_type(
                                getattr(
                                    rel.relationship_type,
                                    "value",
                                    rel.relationship_type,
                                )
                            ),
                            strength=rel.strength,
                            confidence_score=rel.confidence_score,
                            context=rel.context,
                            evidence=rel.evidence or [],
                            metadata=rel.metadata or {},
                            source_document_id=rel.source_document_id,
                            organization_id=job_org_id,
                        )
                        try:
                            knowledge_graph_service.create_relationship(create_request)
                        except Exception as rel_error:
                            # Duplicate-relationship inserts are expected while
                            # re-pointing edges onto the primary; log at debug so
                            # a genuine create failure isn't fully invisible.
                            logger.debug(
                                "Skipped relationship insert during entity merge: %s",
                                rel_error,
                                exc_info=True,
                            )

                    # delete_entity swallows Neo4j errors and returns False (and
                    # also returns False when the node is already gone). The
                    # relationships were already re-pointed onto the primary, so
                    # a failure here leaves the duplicate node alive alongside
                    # duplicated edges — NOT a merged group. Track it so the
                    # group is reported failed instead of a false success.
                    if not knowledge_graph_service.delete_entity(
                        duplicate_id, organization_id=job_org_id
                    ):
                        merged_all_duplicates = False
                        logger.warning(
                            "Entity merge: duplicate %s not deleted "
                            "(group %s/%s, primary %s) — group marked failed",
                            duplicate_id,
                            index + 1,
                            total_groups,
                            primary_id,
                        )

                if merged_all_duplicates:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as merge_error:
                # A group that fails to merge was previously counted but never
                # logged, so the cause was invisible while the job still reported
                # COMPLETED. Log which group failed and why.
                failure_count += 1
                logger.warning(
                    "Failed to merge entity group %s/%s (primary %s): %s",
                    index + 1,
                    total_groups,
                    primary_id,
                    merge_error,
                    exc_info=True,
                )

        job.update_progress("Finalizing merge job", 95)
        job.complete_job(
            result={
                "total_groups": total_groups,
                "merged_groups": success_count,
                "failed_groups": failure_count,
            }
        )
        db.commit()
        return {"status": "completed", "job_id": job_id}
    except Exception as e:
        # Roll back a possibly-poisoned session before writing fail state.
        db.rollback()
        if "job" in locals() and job:
            job.fail_job(str(e), error_type=type(e).__name__)
            db.commit()
        raise
    finally:
        db.close()


# Terminal ProcessingJob states (mirrors ProcessingJob.is_finished). Everything
# else — PENDING, QUEUED, RUNNING, RETRYING — is non-terminal and sweepable.
_TERMINAL_PROCESSING_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)
_NON_TERMINAL_PROCESSING_STATUSES = [
    status for status in JobStatus if status not in _TERMINAL_PROCESSING_STATUSES
]


@current_app.task(name="src.tasks.processing_tasks.sweep_stuck_processing_jobs")
def sweep_stuck_processing_jobs() -> dict:
    """Fail non-terminal processing_jobs rows that stopped making progress.

    Audit P1.4b / D7: ``cleanup_old_jobs`` below only ever deletes TERMINAL
    rows, so a job whose worker died (OOM-kill, pod eviction, crash between
    status writes) sat in pending/queued/running/retrying forever, invisibly
    "in progress" to the UI.

    ``updated_at`` is bumped by every progress write, and Celery's hard time
    limit is 600s — so a non-terminal row silent for
    ``PROCESSING_JOB_STUCK_AFTER_SECONDS`` (default 30 min, ≥3× the max legal
    task runtime) is definitively stuck and is marked FAILED with an explicit
    sweep message. Conservative by design: mark-failed only, never re-enqueue
    (the task may have partially executed). Known edge: with acks_late a
    QUEUED row swept during an extreme (>30 min) broker backlog can still be
    picked up later — its start_job/complete writes simply overwrite the
    sweep, so the job self-heals; the sweep is logged either way.

    Flag-gated by SWEEPERS_ENABLED (values-controllable kill switch).
    """
    from src.core.config import get_settings

    settings_local = get_settings()
    if not settings_local.SWEEPERS_ENABLED:
        logger.info("sweep_stuck_processing_jobs: skipped (SWEEPERS_ENABLED=false)")
        return {"skipped": "sweepers-disabled"}

    threshold_seconds = settings_local.PROCESSING_JOB_STUCK_AFTER_SECONDS
    # Naive-UTC cutoff matches this model's BaseModel timestamps
    # (default/onupdate=datetime.utcnow) and cleanup_old_jobs' convention.
    cutoff = datetime.utcnow() - timedelta(seconds=threshold_seconds)

    db = SessionLocal()
    try:
        stuck_jobs = (
            db.query(ProcessingJob)
            .filter(
                ProcessingJob.status.in_(_NON_TERMINAL_PROCESSING_STATUSES),
                ProcessingJob.updated_at < cutoff,
                ProcessingJob.is_deleted == False,  # noqa: E712
            )
            .limit(200)
            .all()
        )

        for job in stuck_jobs:
            prior_status = job.status.value if job.status else "unknown"
            last_update = job.updated_at.isoformat() if job.updated_at else "unknown"
            logger.warning(
                "sweep_stuck_processing_jobs: failing job %s (type=%s, was=%s, "
                "last update %s, threshold %ss)",
                job.id,
                job.job_type.value if job.job_type else "unknown",
                prior_status,
                last_update,
                threshold_seconds,
            )
            job.fail_job(
                f"Swept as stuck: status '{prior_status}' with no update since "
                f"{last_update} (threshold {threshold_seconds}s; Celery hard "
                "time limit is 600s)",
                error_type="StuckJobSweep",
            )

        db.commit()
        result = {"swept": len(stuck_jobs)}
        logger.info("sweep_stuck_processing_jobs: %s", result)
        return result
    except Exception as e:
        db.rollback()
        logger.error(f"Stuck-job sweep failed: {str(e)}")
        raise
    finally:
        db.close()


@current_app.task
def cleanup_old_jobs():
    """Cleanup old processing jobs"""
    db = SessionLocal()
    try:
        # Delete jobs older than 30 days
        cutoff_date = datetime.utcnow() - timedelta(days=30)

        old_jobs = (
            db.query(ProcessingJob)
            .filter(
                ProcessingJob.created_at < cutoff_date,
                ProcessingJob.status.in_(
                    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
                ),
            )
            .all()
        )

        for job in old_jobs:
            db.delete(job)

        db.commit()
        logger.info(f"Cleaned up {len(old_jobs)} old processing jobs")

        return {"cleaned_jobs": len(old_jobs)}

    except Exception as e:
        logger.error(f"Job cleanup failed: {str(e)}")
        raise

    finally:
        db.close()


# Periodic tasks
from celery.schedules import crontab

# Merge (not assign) — every task module shares one app.conf.beat_schedule, so a
# full `= {...}` here is clobbered by whichever module Celery imports last
# (include order in celery_app.py). .update() lets all modules' schedules coexist.
current_app.conf.beat_schedule.update(
    {
        "cleanup-old-jobs": {
            "task": "src.tasks.processing_tasks.cleanup_old_jobs",
            "schedule": crontab(hour=2, minute=0),  # Run daily at 2 AM
        },
    }
)
