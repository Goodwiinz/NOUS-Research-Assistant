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
from sqlalchemy.orm import Session, sessionmaker

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks import processing_tasks as pt
from src.tasks.processing_lifecycle import ProcessingStopped, require_active_ingestion

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Change = Callable[[ProcessingJob, Document], None]


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
        Base.metadata.create_all(engine)
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
        yield SimpleNamespace(Session=factory, doc_id=doc_id, job_id=job_id)
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


def _cancel(job: ProcessingJob, doc: Document) -> None:
    job.cancel_job()


def _delete(job: ProcessingJob, doc: Document) -> None:
    doc.soft_delete()


def _newer_retry(job: ProcessingJob, doc: Document) -> None:
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
