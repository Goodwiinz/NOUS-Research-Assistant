"""Real-Postgres proof that a running ingestion cannot write after it lost the
job (GOO-357).

The worker loads the job and document once and keeps them in its session. If
the document is deleted, the job cancelled, or a retry claims the job while
the worker is parsing or calling a model, the next stage commit used to write
the stale objects back: live entities for a deleted document, or a cancelled
job flipped to COMPLETED/FAILED. Each test commits that change from a second
session mid-run, the way the API or sweeper would.

Needs ``ORCHESTRATION_TEST_DATABASE_URL``; imports the task module, so it
needs the full backend dependency set (spaCy).
"""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.models.user import User
from src.tasks import processing_tasks as pt
from src.tasks.processing_lifecycle import ProcessingStopped, require_active_ingestion

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Change = Callable[[Any, Any], None]


def _dsn() -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@contextmanager
def _ingestion() -> Iterator[SimpleNamespace]:
    """Isolated schema with one PENDING document and its QUEUED ingestion job."""
    schema = "stage_guard_" + uuid.uuid4().hex
    admin = create_engine(_dsn())
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_dsn(), connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Organization.__table__,
                User.__table__,
                Document.__table__,
                ProcessingJob.__table__,
                Entity.__table__,
            ],
        )
        factory = sessionmaker(engine, autocommit=False, autoflush=False)
        org_id, doc_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with factory() as db:
            db.add(Organization(id=org_id, name="Org", storage_limit_bytes=10_000))
            db.flush()
            db.add(
                Document(
                    id=doc_id,
                    title="Doc",
                    filename="doc.txt",
                    file_path="local:///nonexistent/doc.txt",
                    file_size_bytes=10,
                    mime_type="text/plain",
                    document_type=DocumentType.TEXT,
                    organization_id=org_id,
                    content_text="Ada and Charles.",
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            db.flush()
            db.add(
                ProcessingJob(
                    id=job_id,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.QUEUED,
                    organization_id=org_id,
                    document_id=doc_id,
                    parameters={"document_id": str(doc_id)},
                    total_steps=5,
                )
            )
            db.commit()
        # NullPool: the DO KB bridge runs each call on its own event loop.
        async_engine = create_async_engine(
            _dsn().replace("+psycopg2", "+asyncpg"),
            connect_args={"server_settings": {"search_path": schema}},
            poolclass=NullPool,
        )
        yield SimpleNamespace(
            Session=factory,
            AsyncSession=async_sessionmaker(async_engine, expire_on_commit=False),
            doc_id=doc_id,
            job_id=job_id,
            org_id=org_id,
        )
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def _interfere(env: SimpleNamespace, change: Change) -> None:
    with env.Session() as other:
        job = other.get(ProcessingJob, env.job_id)
        doc = other.get(Document, env.doc_id)
        change(job, doc)
        other.commit()


def _cancel(job: Any, doc: Any) -> None:
    job.cancel_job()


def _delete(job: Any, doc: Any) -> None:
    doc.soft_delete()


def _newer_retry(job: Any, doc: Any) -> None:
    job.celery_task_id = "newer-attempt"


class _FakePipeline:
    """Deterministic text and entities, no spaCy or model calls."""

    def __init__(self, db: Session) -> None:
        self.db = db

    async def process_text_extraction(self, document: Document) -> dict:
        return {
            "text_content": document.content_text,
            "summary": "s",
            "word_count": 3,
            "character_count": 16,
        }

    async def process_entity_extraction(
        self, document: Document, text: str
    ) -> list[Entity]:
        from datetime import datetime, timezone

        return [
            Entity(
                entity_type=EntityType.PERSON,
                name=name,
                extraction_method=ExtractionMethod.SPACY,
                extracted_at=datetime.now(timezone.utc),
                confidence=0.9,
                document_id=document.id,
                organization_id=document.organization_id,
            )
            for name in ("Ada Lovelace", "Charles Babbage")
        ]


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(pt, "ProcessingPipeline", _FakePipeline)
    monkeypatch.setattr(pt, "_sync_document_to_kb_blocking", lambda document: None)
    monkeypatch.setattr(pt, "_index_entities_to_graph", lambda document, ents: 0)
    monkeypatch.setattr(
        pt.fulltext_search_service,
        "update_document_search_vector",
        lambda *a, **k: None,
    )
    return monkeypatch


def _run(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(pt, "SessionLocal", env.Session)
    return pt.process_document_ingestion.apply(args=(str(env.job_id),))


def _state(env: SimpleNamespace) -> tuple[JobStatus, ProcessingStatus, int]:
    with env.Session() as db:
        job = db.get(ProcessingJob, env.job_id)
        doc = db.get(Document, env.doc_id)
        entities = db.query(Entity).filter(Entity.document_id == env.doc_id).count()
        return job.status, doc.processing_status, entities


@pytest.mark.parametrize(
    "change,reason,job_status",
    [
        (_cancel, "cancelled", JobStatus.CANCELLED),
        (_delete, "deleted", JobStatus.RUNNING),
        (_newer_retry, "superseded", JobStatus.RUNNING),
    ],
)
def test_cancel_after_extraction_blocks_stage_commit(
    stubbed: pytest.MonkeyPatch,
    change: Change,
    reason: str,
    job_status: JobStatus,
) -> None:
    with _ingestion() as env:
        extract = _FakePipeline.process_entity_extraction

        async def extract_then_interfere(
            self: _FakePipeline, document: Document, text: str
        ) -> list[Entity]:
            entities = await extract(self, document, text)
            _interfere(env, change)
            return entities

        stubbed.setattr(
            _FakePipeline, "process_entity_extraction", extract_then_interfere
        )

        result = _run(env, stubbed).get()

        assert result == {
            "status": "stopped",
            "job_id": str(env.job_id),
            "skipped": reason,
        }
        status, doc_status, entities = _state(env)
        assert entities == 0
        assert status == job_status
        assert doc_status != ProcessingStatus.COMPLETED


def test_cancel_before_final_completion_keeps_cancelled(
    stubbed: pytest.MonkeyPatch,
) -> None:
    with _ingestion() as env:
        # The search-vector build is the last step before the finalize commits.
        stubbed.setattr(
            pt.fulltext_search_service,
            "update_document_search_vector",
            lambda *a, **k: None,
        )
        progress = ProcessingJob.update_progress

        def cancel_at_finalize(job: ProcessingJob, step: str, *a: Any) -> None:
            progress(job, step, *a)
            if step == "Finalizing":
                _interfere(env, _cancel)

        stubbed.setattr(ProcessingJob, "update_progress", cancel_at_finalize)

        result = _run(env, stubbed).get()

        assert result["skipped"] == "cancelled"
        status, doc_status, _ = _state(env)
        assert status == JobStatus.CANCELLED
        assert doc_status != ProcessingStatus.COMPLETED


def test_failure_after_cancellation_does_not_overwrite_cancelled(
    stubbed: pytest.MonkeyPatch,
) -> None:
    """Neither the task's failure path nor ProcessingTask.on_failure may write
    FAILED over a cancellation."""
    with _ingestion() as env:

        async def cancel_then_crash(
            self: _FakePipeline, document: Document, text: str
        ) -> list[Entity]:
            _interfere(env, _cancel)
            raise RuntimeError("model timeout")

        stubbed.setattr(_FakePipeline, "process_entity_extraction", cancel_then_crash)

        _run(env, stubbed)

        status, doc_status, _ = _state(env)
        assert status == JobStatus.CANCELLED
        assert doc_status != ProcessingStatus.FAILED


def test_failure_of_live_attempt_still_fails_job_and_document(
    stubbed: pytest.MonkeyPatch,
) -> None:
    with _ingestion() as env:

        async def crash(
            self: _FakePipeline, document: Document, text: str
        ) -> list[Entity]:
            raise RuntimeError("model timeout")

        stubbed.setattr(_FakePipeline, "process_entity_extraction", crash)

        _run(env, stubbed)

        status, doc_status, _ = _state(env)
        assert status == JobStatus.FAILED
        assert doc_status == ProcessingStatus.FAILED


def test_happy_path_still_completes(stubbed: pytest.MonkeyPatch) -> None:
    with _ingestion() as env:
        result = _run(env, stubbed).get()

        assert result["status"] == "completed"
        assert _state(env) == (JobStatus.COMPLETED, ProcessingStatus.COMPLETED, 2)


def test_locked_check_serializes_against_cancellation() -> None:
    """While the worker holds the guard's locks, a cancellation waits; once the
    worker commits, the cancellation lands. Neither write is lost."""
    with _ingestion() as env:
        with env.Session() as setup:
            job = setup.get(ProcessingJob, env.job_id)
            job.start_job(worker_id="w", celery_task_id="t")
            setup.commit()

        worker = env.Session()
        require_active_ingestion(
            worker, str(env.job_id), expected_task_id="t", lock=True
        )
        worker.get(ProcessingJob, env.job_id).update_progress("Extracting", 50)

        cancelled = threading.Event()

        def cancel() -> None:
            _interfere(env, _cancel)
            cancelled.set()

        thread = threading.Thread(target=cancel)
        thread.start()
        assert not cancelled.wait(0.5), "cancellation must wait for the locks"

        worker.commit()
        thread.join(timeout=10)
        assert cancelled.is_set()
        worker.close()

        with env.Session() as check:
            job = check.get(ProcessingJob, env.job_id)
            assert job.status == JobStatus.CANCELLED
            assert job.current_step == "Extracting"

        # The worker's next stage check now refuses.
        with env.Session() as worker2:
            with pytest.raises(ProcessingStopped) as exc:
                require_active_ingestion(
                    worker2, str(env.job_id), expected_task_id="t", lock=True
                )
            assert exc.value.reason == "cancelled"


def _producer_late_queued_write(job: Any, doc: Any) -> None:
    # processing_service.queue_processing_job sends the task, then writes
    # QUEUED + the same task id; a fast worker may already have claimed it.
    job.status = JobStatus.QUEUED


def test_producer_late_queued_write_does_not_stop_live_run(
    stubbed: pytest.MonkeyPatch,
) -> None:
    with _ingestion() as env:
        extract = _FakePipeline.process_text_extraction

        async def extract_after_producer_commit(
            self: _FakePipeline, document: Document
        ) -> dict:
            with env.Session() as other:
                job = other.get(ProcessingJob, env.job_id)
                # keep the claimed task id, as the producer writes task.id
                _producer_late_queued_write(job, None)
                other.commit()
            return await extract(self, document)

        stubbed.setattr(
            _FakePipeline, "process_text_extraction", extract_after_producer_commit
        )

        result = _run(env, stubbed).get()

        assert result["status"] == "completed"
        assert _state(env) == (JobStatus.COMPLETED, ProcessingStatus.COMPLETED, 2)


def _wait_for_lock_waiter(env: SimpleNamespace, timeout: float = 5.0) -> None:
    import time

    from sqlalchemy import text

    deadline = time.monotonic() + timeout
    with env.Session() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar()
            if waiting:
                return
            probe.rollback()
            time.sleep(0.05)
    raise AssertionError("deleter never blocked on a row lock")


def test_guard_does_not_deadlock_with_a_concurrent_delete() -> None:
    """A stage that dirtied only the job must not take the job lock (via flush)
    before the Document lock: a delete holding Document and waiting on the job
    would deadlock with it. The guard locks Document -> Job first, like the
    delete, so the delete simply waits for the worker's commit."""
    with _ingestion() as env:
        with env.Session() as setup:
            setup.get(ProcessingJob, env.job_id).start_job(
                worker_id="w", celery_task_id="t"
            )
            setup.commit()

        errors: list[BaseException] = []
        deleted = threading.Event()

        def delete() -> None:
            # The same lock order as FileService.soft_delete_documents.
            try:
                with env.Session() as d:
                    doc = (
                        d.query(Document)
                        .filter(Document.id == env.doc_id)
                        .with_for_update()
                        .one()
                    )
                    job = (
                        d.query(ProcessingJob)
                        .filter(ProcessingJob.id == env.job_id)
                        .with_for_update()
                        .one()
                    )
                    job.cancel_job()
                    doc.soft_delete()
                    d.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                deleted.set()

        worker = env.Session()
        worker.get(ProcessingJob, env.job_id).update_progress("Extracting", 50)
        flush = worker.flush

        def flush_then_race(*args: Any, **kwargs: Any) -> None:
            flush(*args, **kwargs)
            worker.flush = flush  # type: ignore[method-assign]
            threading.Thread(target=delete).start()
            _wait_for_lock_waiter(env)

        worker.flush = flush_then_race  # type: ignore[method-assign]
        try:
            require_active_ingestion(
                worker, str(env.job_id), expected_task_id="t", lock=True
            )
            worker.commit()
        except BaseException as exc:  # noqa: BLE001
            worker.rollback()
            errors.append(exc)
        finally:
            assert deleted.wait(15)
            worker.close()

        assert errors == []
        with env.Session() as check:
            assert check.get(ProcessingJob, env.job_id).status == JobStatus.CANCELLED
            assert check.get(Document, env.doc_id).is_deleted is True


def test_entity_reset_does_not_deadlock_with_a_cascading_delete(
    stubbed: pytest.MonkeyPatch,
) -> None:
    """The entity reset DELETE locks this document's old entity rows at once. A
    delete that holds Document + job and cascades to those entities would wait
    on the worker while the worker waits on its Document lock, unless the
    worker took Document -> Job before the reset."""
    from datetime import datetime, timezone

    with _ingestion() as env:
        with env.Session() as setup:  # a previous crashed run's entities
            doc = setup.get(Document, env.doc_id)
            setup.add(
                Entity(
                    entity_type=EntityType.PERSON,
                    name="Ada Lovelace",
                    extraction_method=ExtractionMethod.SPACY,
                    extracted_at=datetime.now(timezone.utc),
                    confidence=0.9,
                    document_id=env.doc_id,
                    organization_id=doc.organization_id,
                )
            )
            setup.commit()

        errors: list[BaseException] = []
        deleted = threading.Event()

        def delete() -> None:
            # FileService.soft_delete_documents order: Document, job, entities.
            try:
                with env.Session() as d:
                    doc = (
                        d.query(Document)
                        .filter(Document.id == env.doc_id)
                        .with_for_update()
                        .one()
                    )
                    job = (
                        d.query(ProcessingJob)
                        .filter(ProcessingJob.id == env.job_id)
                        .with_for_update()
                        .one()
                    )
                    job.cancel_job()
                    d.query(Entity).filter(Entity.document_id == env.doc_id).update(
                        {"is_deleted": True}, synchronize_session=False
                    )
                    doc.soft_delete()
                    d.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                deleted.set()

        reset = pt._reset_pipeline_entities

        def reset_then_race(db: Session, document_id: Any) -> int:
            count = reset(db, document_id)
            threading.Thread(target=delete).start()
            _wait_for_lock_waiter(env)
            return count

        stubbed.setattr(pt, "_reset_pipeline_entities", reset_then_race)

        result = _run(env, stubbed).get()
        assert deleted.wait(15)

        assert errors == []
        assert result["status"] == "stopped"
        with env.Session() as check:
            live = (
                check.query(Entity)
                .filter(Entity.document_id == env.doc_id, Entity.is_deleted == False)
                .count()
            )
            assert live == 0
            assert check.get(ProcessingJob, env.job_id).status == JobStatus.CANCELLED


# ---------------------------------------------------------------------------
# Remote (DO KB / Neo4j) writes that finish after deletion (GOO-358)
# ---------------------------------------------------------------------------


def _write_late_data_source(env: SimpleNamespace, ds_uuid: str) -> None:
    """What sync_document_to_kb's own commit does once the remote call returns:
    store the uuid on the row, which the delete has already soft-deleted."""
    with env.Session() as other:
        other.get(Document, env.doc_id).do_kb_data_source_uuid = ds_uuid
        other.commit()


def _fake_unsync(ok: bool) -> Any:
    calls: list[str] = []

    async def unsync(session: Any, document: Any) -> bool:
        calls.append(document.do_kb_data_source_uuid)
        if ok:
            document.do_kb_data_source_uuid = None
            await session.commit()
        return ok

    unsync.calls = calls  # type: ignore[attr-defined]
    return unsync


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_late_do_kb_success_is_compensated(
    stubbed: pytest.MonkeyPatch, cleanup_ok: bool
) -> None:
    """The document is deleted while the DO KB call is in flight; the call then
    succeeds. The data source is removed at once, or left on the deleted row
    for the reconciler. The row is never resurrected."""
    from src.tasks.reconcile_tasks import _reconcilable_filters

    with _ingestion() as env:

        def sync_racing_delete(document: Document, **_: Any) -> str:
            _interfere(env, _delete)
            _write_late_data_source(env, "ds-late")
            return "ds-late"

        unsync = _fake_unsync(cleanup_ok)
        stubbed.setattr(pt, "_sync_document_to_kb_blocking", sync_racing_delete)
        stubbed.setattr("src.core.database.AsyncSessionLocal", env.AsyncSession)
        stubbed.setattr("src.services.do_kb.unsync_document_from_kb", unsync)

        result = _run(env, stubbed).get()

        assert result["skipped"] == "deleted"
        assert unsync.calls == ["ds-late"]
        with env.Session() as check:
            doc = check.get(Document, env.doc_id)
            assert doc.is_deleted is True
            if cleanup_ok:
                assert doc.do_kb_data_source_uuid is None
            else:
                assert doc.do_kb_data_source_uuid == "ds-late"
                assert (
                    check.query(Document.id)
                    .filter(*_reconcilable_filters(), Document.id == env.doc_id)
                    .count()
                    == 1
                )


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_late_graph_write_is_compensated(
    stubbed: pytest.MonkeyPatch, cleanup_ok: bool
) -> None:
    from unittest.mock import MagicMock

    from src.tasks.reconcile_tasks import _reconcilable_filters

    with _ingestion() as env:

        def index_racing_delete(document: Document, entities: Any) -> int:
            _interfere(env, _delete)
            return len(entities)

        kg = MagicMock()
        if not cleanup_ok:
            kg.delete_document_graph.side_effect = RuntimeError("neo4j down")
        stubbed.setattr(pt, "_index_entities_to_graph", index_racing_delete)
        stubbed.setattr(
            "src.services.knowledge_graph.knowledge_graph_service."
            "KnowledgeGraphService",
            lambda: kg,
        )

        result = _run(env, stubbed).get()

        assert result["skipped"] == "deleted"
        kg.delete_document_graph.assert_called_once_with(
            str(env.doc_id), str(env.org_id)
        )
        with env.Session() as check:
            doc = check.get(Document, env.doc_id)
            assert doc.is_deleted is True
            assert doc.neo4j_index_status == ("completed" if cleanup_ok else "failed")
            selected = (
                check.query(Document.id)
                .filter(*_reconcilable_filters(), Document.id == env.doc_id)
                .count()
            )
            assert selected == (0 if cleanup_ok else 1)


def test_stale_snapshot_does_not_revert_state_through_do_kb_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge used to merge() the caller's stale Document and commit it,
    writing every loaded column back over concurrent changes."""
    with _ingestion() as env:
        worker = env.Session()
        stale = worker.get(Document, env.doc_id)  # PENDING, kept in memory
        with env.Session() as other:  # e.g. the sweeper fails it meanwhile
            other.get(Document, env.doc_id).processing_status = ProcessingStatus.FAILED
            other.commit()

        async def fake_sync(session: Any, document: Any, **_: Any) -> str:
            document.do_kb_data_source_uuid = "ds-1"
            return "ds-1"

        monkeypatch.setattr("src.core.database.AsyncSessionLocal", env.AsyncSession)
        monkeypatch.setattr("src.services.do_kb.sync_document_to_kb", fake_sync)

        assert pt._sync_document_to_kb_blocking(stale) == "ds-1"
        worker.close()

        with env.Session() as check:
            doc = check.get(Document, env.doc_id)
            assert doc.processing_status == ProcessingStatus.FAILED
            assert doc.do_kb_data_source_uuid == "ds-1"


def test_late_graph_write_is_compensated_when_the_stage_then_crashes(
    stubbed: pytest.MonkeyPatch,
) -> None:
    """The graph write lands, the document is deleted, and the stage then fails
    with an ordinary error (deadlock, timeout, dropped connection) rather than
    ProcessingStopped. The failure path must still clean up the graph."""
    from unittest.mock import MagicMock

    with _ingestion() as env:

        def index_delete_then_crash(document: Document, entities: Any) -> int:
            _interfere(env, _delete)
            raise RuntimeError("connection dropped")

        kg = MagicMock()
        stubbed.setattr(pt, "_index_entities_to_graph", index_delete_then_crash)
        stubbed.setattr(
            "src.services.knowledge_graph.knowledge_graph_service."
            "KnowledgeGraphService",
            lambda: kg,
        )

        _run(env, stubbed)

        kg.delete_document_graph.assert_called_once_with(
            str(env.doc_id), str(env.org_id)
        )
        with env.Session() as check:
            assert check.get(Document, env.doc_id).neo4j_index_status == "completed"


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_non_cascade_late_graph_write_is_retained(stubbed, cleanup_ok):
    """Retaining entities must also retain late graph writes and exclude retries."""
    import asyncio
    from unittest.mock import MagicMock

    from src.services.documents.file_service import FileService
    from src.tasks.reconcile_tasks import _reconcilable_filters

    with _ingestion() as env:

        def index_racing_delete(document, entities):
            async def delete():
                async with env.AsyncSession() as session:
                    await FileService(session).soft_delete_documents(
                        env.org_id, [env.doc_id], cascade=False
                    )

            asyncio.run(delete())
            return len(entities)

        kg = MagicMock()
        if not cleanup_ok:
            kg.delete_document_graph.side_effect = RuntimeError("neo4j down")
        stubbed.setattr(pt, "_index_entities_to_graph", index_racing_delete)
        stubbed.setattr(
            "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
            lambda: kg,
        )
        result = _run(env, stubbed).get()
        assert result["skipped"] == "deleted"
        kg.delete_document_graph.assert_not_called()
        with env.Session() as check:
            doc = check.get(Document, env.doc_id)
            assert doc.is_deleted
            assert check.query(Entity).filter(Entity.is_deleted == False).count() == 2
            assert (
                check.query(Document.id)
                .filter(*_reconcilable_filters(), Document.id == env.doc_id)
                .count()
                == 0
            )
