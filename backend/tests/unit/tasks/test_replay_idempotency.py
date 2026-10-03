"""Replay ownership + stage idempotency for the doc-processing Celery tasks.

``celery_app`` sets ``task_acks_late=True``, so a worker killed mid-task never
acks and the broker redelivers the message. Audit P1.5 (finding X4) requires the
two ingestion tasks to survive that redelivery without duplicating work or
corrupting job state:

* ``process_document_ingestion`` (canonical) reclaims a job whose worker died
  mid-run and re-runs the pipeline *idempotently* — its Postgres ``Entity``
  writes are replaced, not appended (closes the entity-duplication half of the
  hunt-9 #912 follow-up), while a finished job is skipped outright.

The claim decision itself lives in ``src.tasks.replay_guard``. These tests use a
real in-memory SQLite schema so "no duplicate entities" is a real row count.

They import the task modules, which pull spaCy / langchain at import time, so
they run in CI (where those deps are installed), not the lean local venv.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks import processing_tasks as pt
from src.tasks.replay_guard import claim_job_for_processing


@pytest.fixture
def session_factory():
    """A sessionmaker over a fresh in-memory SQLite schema (all tables).

    ``expire_on_commit=False`` so a tz-aware ``started_at`` set by ``start_job``
    survives commits in memory; SQLite silently drops tzinfo on round-trip,
    which would otherwise make ``complete_job``'s ``completed_at - started_at``
    mix naive/aware datetimes. Production uses Postgres ``timestamptz`` (always
    aware), so this only papers over a SQLite storage limitation.
    """
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


def _seed_document(
    db, *, doc_id, org_id, content_text, status=ProcessingStatus.PROCESSING
):
    doc = Document(
        id=doc_id,
        title="Doc",
        filename="doc.txt",
        file_path="local:///tmp/doc.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        organization_id=org_id,
        content_text=content_text,
        processing_status=status,
    )
    db.add(doc)
    return doc


def _seed_job(db, *, job_id, doc_id, org_id, status, started_at=None):
    job = ProcessingJob(
        id=job_id,
        job_type=JobType.DOCUMENT_INGESTION,
        status=status,
        organization_id=org_id,
        document_id=doc_id,
        parameters={"document_id": str(doc_id)},
        # complete_job copies total_steps -> completed_steps (NOT NULL); real
        # jobs always carry it, so seed it here too.
        total_steps=5,
        started_at=started_at,
    )
    db.add(job)
    return job


def _entity(name, method, doc_id, org_id, *, etype=EntityType.PERSON):
    return Entity(
        entity_type=etype,
        name=name,
        extraction_method=method,
        extracted_at=datetime.now(timezone.utc),
        confidence=0.9,
        document_id=doc_id,
        organization_id=org_id,
    )


# ---------------------------------------------------------------------------
# claim_job_for_processing decision matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,started_delta,expect_proceed,expect_reason",
    [
        (JobStatus.COMPLETED, None, False, "terminal"),
        (JobStatus.FAILED, None, False, "terminal"),
        (JobStatus.CANCELLED, None, False, "terminal"),
        # RUNNING and recent -> assumed live on another worker, skip.
        (JobStatus.RUNNING, timedelta(seconds=5), False, "running"),
        # RUNNING and old (or no started_at) -> worker died, reclaim.
        (JobStatus.RUNNING, timedelta(days=1), True, "reclaimed_stale"),
        (JobStatus.RUNNING, None, True, "reclaimed_stale"),
        (JobStatus.QUEUED, None, True, "claimed"),
        (JobStatus.PENDING, None, True, "claimed"),
        (JobStatus.RETRYING, None, True, "claimed"),
    ],
)
def test_claim_matrix(
    session_factory, status, started_delta, expect_proceed, expect_reason
):
    Session = session_factory
    db = Session()
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    started = (
        None if started_delta is None else datetime.now(timezone.utc) - started_delta
    )
    _seed_document(db, doc_id=doc_id, org_id=org_id, content_text=None)
    _seed_job(
        db,
        job_id=job_id,
        doc_id=doc_id,
        org_id=org_id,
        status=status,
        started_at=started,
    )
    db.commit()

    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    result = claim_job_for_processing(db, job, worker_id="w1", celery_task_id="t1")

    assert result.proceed is expect_proceed
    assert result.reason == expect_reason
    if expect_proceed:
        # A claim/reclaim moves the job to RUNNING and records the worker.
        assert job.status == JobStatus.RUNNING
        assert job.worker_id == "w1"
    else:
        # A skip leaves the persisted status untouched.
        assert job.status == status
    db.close()


def test_reclaim_moves_started_at_forward(session_factory):
    """A stale reclaim resets started_at so the new owner's clock starts now."""
    Session = session_factory
    db = Session()
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    stale = datetime.now(timezone.utc) - timedelta(hours=6)
    _seed_document(db, doc_id=doc_id, org_id=org_id, content_text=None)
    _seed_job(
        db,
        job_id=job_id,
        doc_id=doc_id,
        org_id=org_id,
        status=JobStatus.RUNNING,
        started_at=stale,
    )
    db.commit()

    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    result = claim_job_for_processing(db, job, worker_id="w2")

    assert result.proceed is True
    age = datetime.now(timezone.utc) - job.started_at
    assert age < timedelta(minutes=1)
    db.close()


def test_claim_reads_freshly_locked_row_not_stale_cache(session_factory):
    """The lock re-read must reflect the just-locked DB row, not the cached
    identity-map instance.

    Simulates the concurrency race the guard exists to prevent: worker B loads
    the job while it is still QUEUED, worker A commits it to RUNNING out-of-band
    (a second session), then worker B calls ``claim_job_for_processing``. Without
    ``populate_existing()`` on the FOR UPDATE query, SQLAlchemy returns worker
    B's cached (stale QUEUED) instance and B wrongly claims the already-running
    job → double-processing. With the fix, B sees RUNNING and skips.
    """
    Session = session_factory
    db = Session()
    other = Session()
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    _seed_document(db, doc_id=doc_id, org_id=org_id, content_text=None)
    _seed_job(
        db,
        job_id=job_id,
        doc_id=doc_id,
        org_id=org_id,
        status=JobStatus.QUEUED,
    )
    db.commit()

    # Worker B loads the job (now cached QUEUED in this Session's identity map).
    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    assert job.status == JobStatus.QUEUED

    # Worker A claims it out-of-band: mutate the row to RUNNING (recent) via a
    # second session and commit, so the DB row is now live-RUNNING.
    a_job = other.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    a_job.start_job(worker_id="worker-a")
    other.commit()
    other.close()

    # Worker B now tries to claim. It must observe the freshly-locked RUNNING row
    # (recent -> assumed live) and refuse, NOT act on its stale cached QUEUED.
    result = claim_job_for_processing(db, job, worker_id="worker-b")

    assert result.proceed is False
    assert result.reason == "running"
    db.close()


@pytest.mark.parametrize("deleted", ["job", "document"])
def test_claim_refuses_deleted_ingestion(session_factory, deleted):
    """A message delivered after its job or document was deleted must not start
    work, even while the job row itself still says QUEUED (GOO-356)."""
    db = session_factory()
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    doc = _seed_document(db, doc_id=doc_id, org_id=org_id, content_text=None)
    job = _seed_job(
        db, job_id=job_id, doc_id=doc_id, org_id=org_id, status=JobStatus.QUEUED
    )
    (job if deleted == "job" else doc).soft_delete()
    db.commit()

    result = claim_job_for_processing(db, job, worker_id="w1")

    assert result.proceed is False
    assert result.reason == "deleted"
    assert job.status == JobStatus.QUEUED
    db.close()


@pytest.mark.parametrize("document_state", ["missing", "foreign", "unlinked"])
def test_claim_refuses_missing_or_foreign_document(session_factory, document_state):
    """A queued ingestion cannot start without its own live document."""
    with session_factory() as db:
        org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
        if document_state == "foreign":
            _seed_document(db, doc_id=doc_id, org_id=uuid4(), content_text=None)
        job = _seed_job(
            db,
            job_id=job_id,
            doc_id=None if document_state == "unlinked" else doc_id,
            org_id=org_id,
            status=JobStatus.QUEUED,
        )
        db.commit()

        result = claim_job_for_processing(db, job, worker_id="w1")

        assert not result.proceed, "Ingestion started without an accessible document"
        assert job.status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# _reset_pipeline_entities scoping
# ---------------------------------------------------------------------------


def test_reset_pipeline_entities_only_touches_pipeline_methods(session_factory):
    Session = session_factory
    db = Session()
    org_id, doc_a, doc_b = uuid4(), uuid4(), uuid4()
    db.add_all(
        [
            _entity("Ada", ExtractionMethod.SPACY, doc_a, org_id),
            _entity(
                "a@b.com", ExtractionMethod.REGEX, doc_a, org_id, etype=EntityType.EMAIL
            ),
            _entity(
                "Curated",
                ExtractionMethod.MANUAL,
                doc_a,
                org_id,
                etype=EntityType.CONCEPT,
            ),
            _entity(
                "LLM", ExtractionMethod.OPENAI, doc_a, org_id, etype=EntityType.CONCEPT
            ),
            # A different document must be left completely alone.
            _entity("Other", ExtractionMethod.SPACY, doc_b, org_id),
        ]
    )
    db.commit()

    deleted = pt._reset_pipeline_entities(db, doc_a)
    db.commit()

    assert deleted == 2  # only the spaCy + regex rows for doc_a
    remaining = sorted(
        e.extraction_method.value
        for e in db.query(Entity).filter(Entity.document_id == doc_a).all()
    )
    assert remaining == ["manual", "openai"]  # curated + LLM preserved
    assert db.query(Entity).filter(Entity.document_id == doc_b).count() == 1
    db.close()


# ---------------------------------------------------------------------------
# process_document_ingestion — canonical task
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Stand-in for ProcessingPipeline: deterministic text + entities, no spaCy."""

    def __init__(self, db):
        self.db = db

    async def process_text_extraction(self, document):
        return {
            "text_content": document.content_text,
            "summary": "s",
            "word_count": 5,
            "character_count": 40,
        }

    async def process_entity_extraction(self, document, text):
        return [
            _entity(
                "Ada Lovelace",
                ExtractionMethod.SPACY,
                document.id,
                document.organization_id,
            ),
            _entity(
                "Charles Babbage",
                ExtractionMethod.SPACY,
                document.id,
                document.organization_id,
            ),
        ]


def _stub_ingestion_side_effects(monkeypatch, Session):
    monkeypatch.setattr(pt, "SessionLocal", Session)
    monkeypatch.setattr(pt, "ProcessingPipeline", _FakePipeline)
    # KB sync + search-vector are already idempotent; stub them out so the test
    # does not need DO Spaces / Postgres full-text.
    monkeypatch.setattr(pt, "_sync_document_to_kb_blocking", lambda document: None)
    monkeypatch.setattr(
        pt.fulltext_search_service,
        "update_document_search_vector",
        lambda *a, **k: None,
    )


def test_running_crash_replay_does_not_duplicate_entities(session_factory, monkeypatch):
    """Job left RUNNING by a crashed worker → redelivery reclaims it, replaces
    the partial entity set instead of appending, and completes."""
    Session = session_factory
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()

    setup = Session()
    _seed_document(setup, doc_id=doc_id, org_id=org_id, content_text="Ada and Charles.")
    _seed_job(
        setup,
        job_id=job_id,
        doc_id=doc_id,
        org_id=org_id,
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc) - timedelta(days=1),  # stale -> reclaim
    )
    # Partial output from the crashed run: two spaCy rows + one curated (MANUAL).
    setup.add_all(
        [
            _entity("Ada Lovelace", ExtractionMethod.SPACY, doc_id, org_id),
            _entity("Charles Babbage", ExtractionMethod.SPACY, doc_id, org_id),
            _entity(
                "Curated Topic",
                ExtractionMethod.MANUAL,
                doc_id,
                org_id,
                etype=EntityType.CONCEPT,
            ),
        ]
    )
    setup.commit()
    setup.close()

    _stub_ingestion_side_effects(monkeypatch, Session)

    result = pt.process_document_ingestion.apply(args=(str(job_id),)).get()
    assert result["status"] == "completed"

    check = Session()
    entities = check.query(Entity).filter(Entity.document_id == doc_id).all()
    # 2 spaCy (replaced, not appended) + 1 curated (preserved) — NOT 5.
    assert len(entities) == 3
    spacy_names = sorted(
        e.name for e in entities if e.extraction_method == ExtractionMethod.SPACY
    )
    assert spacy_names == ["Ada Lovelace", "Charles Babbage"]
    assert any(e.extraction_method == ExtractionMethod.MANUAL for e in entities)
    job = check.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
    assert job.status == JobStatus.COMPLETED

    # A second redelivery of the now-completed job is a terminal short-circuit.
    result2 = pt.process_document_ingestion.apply(args=(str(job_id),)).get()
    assert result2["skipped"] == "terminal"
    assert check.query(Entity).filter(Entity.document_id == doc_id).count() == 3
    check.close()


def test_ingestion_skips_completed_job_without_reprocessing(
    session_factory, monkeypatch
):
    Session = session_factory
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    setup = Session()
    _seed_document(
        setup,
        doc_id=doc_id,
        org_id=org_id,
        content_text="x",
        status=ProcessingStatus.COMPLETED,
    )
    _seed_job(
        setup, job_id=job_id, doc_id=doc_id, org_id=org_id, status=JobStatus.COMPLETED
    )
    setup.commit()
    setup.close()

    monkeypatch.setattr(pt, "SessionLocal", Session)

    # If the guard were bypassed the task would instantiate the pipeline; make
    # that explode so a regression fails loudly.
    def _boom(_db):
        raise AssertionError("pipeline must not run for a completed job")

    monkeypatch.setattr(pt, "ProcessingPipeline", _boom)

    result = pt.process_document_ingestion.apply(args=(str(job_id),)).get()
    assert result["skipped"] == "terminal"
    assert result["status"] == "completed"
