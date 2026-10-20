"""Celery tasks for the pgvector embeddings (GOO-435 S5a).

- the enqueue helper is a no-op while the flag is off and never raises;
- the ingestion task queues the embedding only after the document is COMPLETED
  and committed, and an enqueue failure cannot fail the task;
- the backfill is flag-gated, pages by keyset, counts outcomes, and stops after
  a run of consecutive failures;
- the backfill has no beat entry (manual trigger only).

Ingestion harness: the in-memory SQLite one used by ``test_satellite_status.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest

from src.core.config import settings
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.embedding.document_embedding_service import EmbedOutcome
from src.tasks import document_embedding_tasks as tasks
from src.tasks import processing_tasks as pt
from src.tasks.celery_app import celery_app

pytestmark = pytest.mark.unit


# --- enqueue helper ----------------------------------------------------------------


class _Delay:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error = error

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)
        if self.error is not None:
            raise self.error


def test_enqueue_is_a_no_op_while_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", False)
    delay = _Delay()
    monkeypatch.setattr(tasks.embed_documents, "delay", delay)

    assert tasks.enqueue_document_embeddings(uuid4(), [str(uuid4())]) is False
    assert delay.calls == []


def test_enqueue_queues_string_ids_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    delay = _Delay()
    monkeypatch.setattr(tasks.embed_documents, "delay", delay)
    org, doc = uuid4(), uuid4()

    assert tasks.enqueue_document_embeddings(org, [doc]) is True  # type: ignore[list-item]
    assert delay.calls == [(str(org), [str(doc)])]
    assert tasks.enqueue_document_embeddings(org, []) is False


def test_enqueue_swallows_a_broker_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr(
        tasks.embed_documents, "delay", _Delay(ConnectionError("broker down"))
    )

    assert tasks.enqueue_document_embeddings(uuid4(), ["d"]) is False
    assert "could not queue document embeddings" in caplog.text


# --- registration ------------------------------------------------------------------


def test_tasks_are_registered_and_the_backfill_has_no_beat_entry() -> None:
    assert "src.tasks.document_embedding_tasks" in celery_app.conf.include
    for name in (
        "src.tasks.document_embedding_tasks.embed_documents",
        "src.tasks.document_embedding_tasks.backfill_document_embeddings",
    ):
        assert name in celery_app.tasks
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert not {t for t in scheduled if "document_embedding" in t}


# --- ingestion hook ----------------------------------------------------------------


@pytest.fixture
def session_factory() -> Iterator[Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import src.models  # noqa: F401  register every table on Base.metadata
    from src.models.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        engine.dispose()


class _FakePipeline:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def process_text_extraction(self, document: Any) -> dict[str, Any]:
        return {
            "text_content": document.content_text,
            "summary": "s",
            "word_count": 3,
            "character_count": 20,
        }

    async def process_entity_extraction(self, document: Any, text: str) -> list[Any]:
        return []


def _seed(Session: Any) -> tuple[UUID, UUID, UUID]:
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    db = Session()
    db.add(
        Document(
            id=doc_id,
            title="Doc",
            filename="doc.txt",
            file_path="local:///tmp/doc.txt",
            file_size_bytes=10,
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            organization_id=org_id,
            content_text="Some text.",
            processing_status=ProcessingStatus.PENDING,
        )
    )
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
    db.close()
    return org_id, doc_id, job_id


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, Session: Any) -> None:
    monkeypatch.setattr(pt, "SessionLocal", Session)
    monkeypatch.setattr(pt, "ProcessingPipeline", _FakePipeline)
    monkeypatch.setattr(pt, "_sync_document_to_kb_blocking", lambda d, **kw: None)
    monkeypatch.setattr(
        pt.fulltext_search_service,
        "update_document_search_vector",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(pt.settings, "DO_KB_ENABLED", False)


def test_ingestion_queues_the_embedding_after_the_document_is_committed(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, doc_id, job_id = _seed(session_factory)
    _stub_pipeline(monkeypatch, session_factory)
    seen: list[Any] = []

    def record(organization_id: Any, document_ids: list[str]) -> bool:
        # Read through a fresh session: only committed state is visible.
        db = session_factory()
        try:
            status = (
                db.query(Document.processing_status)
                .filter(Document.id == doc_id)
                .scalar()
            )
        finally:
            db.close()
        seen.append((organization_id, document_ids, status))
        return True

    monkeypatch.setattr(pt, "enqueue_document_embeddings", record)

    result = pt.process_document_ingestion.apply(args=(str(job_id),)).get()

    assert result["status"] == "completed"
    assert seen == [(org_id, [str(doc_id)], ProcessingStatus.COMPLETED)]


def test_a_broker_failure_cannot_fail_ingestion(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, doc_id, job_id = _seed(session_factory)
    _stub_pipeline(monkeypatch, session_factory)
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr(
        tasks.embed_documents, "delay", _Delay(ConnectionError("broker down"))
    )

    result = pt.process_document_ingestion.apply(args=(str(job_id),)).get()

    assert result["status"] == "completed"
    db = session_factory()
    try:
        document = db.query(Document).filter(Document.id == doc_id).one()
        assert document.processing_status == ProcessingStatus.COMPLETED
    finally:
        db.close()


def test_ingestion_queues_nothing_while_the_flag_is_off(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, job_id = _seed(session_factory)
    _stub_pipeline(monkeypatch, session_factory)
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", False)
    delay = _Delay()
    monkeypatch.setattr(tasks.embed_documents, "delay", delay)

    pt.process_document_ingestion.apply(args=(str(job_id),)).get()

    assert delay.calls == []


# --- backfill ----------------------------------------------------------------------


class _Result:
    def __init__(self, ids: list[UUID] | None = None, doc: Any = None) -> None:
        self._ids, self._doc = ids or [], doc

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[UUID]:
        return self._ids

    def scalar_one_or_none(self) -> Any:
        return self._doc


class _BackfillSession:
    """Scripted: candidate pages come from ``pages``; loads return a document."""

    def __init__(
        self, pages: list[list[UUID]], missing: set[UUID] | None = None
    ) -> None:
        self.pages = list(pages)
        self.candidate_statements: list[Any] = []
        self.missing = missing or set()
        self.expunged = 0

    async def __aenter__(self) -> "_BackfillSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, statement: Any) -> _Result:
        text = str(statement)
        if "FROM documents LEFT OUTER JOIN document_embeddings" in text:
            self.candidate_statements.append(statement)
            return _Result(ids=self.pages.pop(0) if self.pages else [])
        wanted = next(iter(statement.compile().params.values()))
        if wanted in self.missing:
            return _Result(doc=None)
        return _Result(doc=SimpleNamespace(id=wanted))

    def expunge_all(self) -> None:
        self.expunged += 1


class _Service:
    def __init__(self, outcomes: list[EmbedOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.seen: list[Any] = []

    async def embed_document(self, session: Any, document: Any) -> EmbedOutcome:
        self.seen.append(document.id)
        return self.outcomes.pop(0) if self.outcomes else EmbedOutcome.EMBEDDED


def _patch_backfill(
    monkeypatch: pytest.MonkeyPatch, session: _BackfillSession, service: _Service
) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", True)
    monkeypatch.setattr("src.core.database.AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        "src.services.embedding.document_embedding_service.document_embedding_service",
        service,
    )


async def test_backfill_is_a_no_op_while_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DOCUMENT_EMBEDDINGS_ENABLED", False)

    def explode() -> None:
        raise AssertionError("no session may open while the flag is off")

    monkeypatch.setattr("src.core.database.AsyncSessionLocal", explode)

    assert await tasks._backfill(100, None) == {
        "scanned": 0,
        "embedded": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert await tasks._embed_documents(str(uuid4()), [str(uuid4())]) == {
        "scanned": 0,
        "embedded": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }


async def test_backfill_pages_by_keyset_and_counts_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(uuid4() for _ in range(5))
    session = _BackfillSession(pages=[ids[:3], ids[3:], []], missing={ids[1]})
    service = _Service(
        [
            EmbedOutcome.EMBEDDED,
            EmbedOutcome.UNCHANGED,
            EmbedOutcome.SKIPPED,
            EmbedOutcome.FAILED,
        ]
    )
    _patch_backfill(monkeypatch, session, service)

    counts = await tasks._backfill(3, None)

    assert counts == {
        "scanned": 5,
        "embedded": 1,
        "unchanged": 1,
        "skipped": 2,  # one skipped by the service, one deleted since the page
        "failed": 1,
    }
    assert ids[1] not in service.seen
    # The second page continues after the last id of the first (keyset), so a
    # document that could not be embedded is not fetched again by this run.
    first, second = session.candidate_statements[:2]
    assert ids[2] in second.compile().params.values()
    assert ids[2] not in first.compile().params.values()
    assert session.expunged >= 4


async def test_backfill_selects_completed_live_documents_without_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = uuid4()
    session = _BackfillSession(pages=[[]])
    _patch_backfill(monkeypatch, session, _Service([]))

    await tasks._backfill(10, str(org))

    sql = str(session.candidate_statements[0].compile())
    assert "document_embeddings.document_id IS NULL" in sql
    assert "documents.is_deleted IS false" in sql
    assert "documents.processing_status" in sql and "documents.organization_id" in sql
    assert "ORDER BY documents.id" in sql
    assert org in session.candidate_statements[0].compile().params.values()


async def test_backfill_stops_after_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(uuid4() for _ in range(25))
    session = _BackfillSession(pages=[ids, []])
    service = _Service([EmbedOutcome.FAILED] * 25)
    _patch_backfill(monkeypatch, session, service)

    counts = await tasks._backfill(100, None)

    assert counts["failed"] == tasks.MAX_CONSECUTIVE_FAILURES
    assert len(service.seen) == tasks.MAX_CONSECUTIVE_FAILURES  # the rest untouched


async def test_a_success_resets_the_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = sorted(uuid4() for _ in range(12))
    session = _BackfillSession(pages=[ids, []])
    outcomes = (
        [EmbedOutcome.FAILED] * 9 + [EmbedOutcome.EMBEDDED] + [EmbedOutcome.FAILED] * 2
    )
    service = _Service(outcomes)
    _patch_backfill(monkeypatch, session, service)

    counts = await tasks._backfill(100, None)

    assert counts["scanned"] == 12 and counts["failed"] == 11


async def test_batch_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _BackfillSession(pages=[[]])
    _patch_backfill(monkeypatch, session, _Service([]))

    await tasks._backfill(10_000, None)

    assert session.candidate_statements[0]._limit == tasks._MAX_BATCH_SIZE
