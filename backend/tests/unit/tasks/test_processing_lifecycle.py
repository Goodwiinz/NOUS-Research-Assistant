"""Decision matrix for ``require_active_ingestion`` (GOO-357).

SQLite is enough for the decision itself; the lock and task-level interleaving
proofs live in ``test_ingestion_stage_guard_postgres.py``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.tasks.processing_lifecycle import (
    ACTIVE_DOCUMENT_STATUSES,
    ACTIVE_JOB_STATUSES,
    NON_TERMINAL_JOB_STATUSES,
    ProcessingStopped,
    end_processing_job,
    require_active_ingestion,
)

pytestmark = pytest.mark.unit

TASK = "task-1"


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Same flags as the worker's SessionLocal: autoflush off is what makes the
    # guard's explicit flush necessary.
    session = sessionmaker(engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db: Session) -> tuple[UUID, Any, Any]:
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    doc = Document(
        id=doc_id,
        title="Doc",
        filename="doc.txt",
        file_path="local:///tmp/doc.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        organization_id=org_id,
    )
    job = ProcessingJob(
        id=job_id,
        job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.RUNNING,
        organization_id=org_id,
        document_id=doc_id,
        parameters={"document_id": str(doc_id)},
        celery_task_id=TASK,
        total_steps=5,
    )
    db.add_all([doc, job])
    db.commit()
    return job_id, job, doc


def test_live_attempt_passes_and_keeps_pending_stage_changes(db: Session) -> None:
    job_id, job, doc = _seed(db)
    doc.content_text = "extracted"  # pending, not flushed

    fresh_job, fresh_doc = require_active_ingestion(
        db, str(job_id), expected_task_id=TASK, lock=True
    )

    assert fresh_job is job and fresh_doc is doc
    assert doc.content_text == "extracted"  # the refresh did not discard it


def _cancel(job: Any, doc: Any) -> None:
    job.cancel_job()


def _sweep(job: Any, doc: Any) -> None:
    job.fail_job("stuck")


def _delete_job(job: Any, doc: Any) -> None:
    job.soft_delete()


def _delete_doc(job: Any, doc: Any) -> None:
    doc.soft_delete()


def _supersede(job: Any, doc: Any) -> None:
    job.celery_task_id = "newer"


def _foreign_org(job: Any, doc: Any) -> None:
    doc.organization_id = uuid4()


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize(
    "change,reason",
    [
        (_cancel, "cancelled"),
        (_sweep, "failed"),
        (_delete_job, "deleted"),
        (_delete_doc, "deleted"),
        (_supersede, "superseded"),
        (_foreign_org, "deleted"),
    ],
)
def test_lost_attempt_is_stopped(
    db: Session,
    change: Callable[[Any, Any], None],
    reason: str,
    lock: bool,
) -> None:
    job_id, job, doc = _seed(db)
    # Committed by someone else; this session's objects are now stale.
    other = Session(bind=db.get_bind())
    change(
        other.get(ProcessingJob, job_id),
        other.get(Document, doc.id),
    )
    other.commit()
    other.close()

    with pytest.raises(ProcessingStopped) as exc:
        require_active_ingestion(db, str(job_id), expected_task_id=TASK, lock=lock)
    assert exc.value.reason == reason


def test_missing_job_is_stopped(db: Session) -> None:
    with pytest.raises(ProcessingStopped) as exc:
        require_active_ingestion(db, str(uuid4()), expected_task_id=TASK)
    assert exc.value.reason == "missing"


@pytest.mark.parametrize(
    "cancel,ended", [(False, JobStatus.FAILED), (True, JobStatus.CANCELLED)]
)
def test_end_processing_job_ends_the_document_with_its_job(
    db: Session, cancel: bool, ended: JobStatus
) -> None:
    """IN-1: every writer that ends an ingestion job ends its document too."""
    job_id, job, doc = _seed(db)
    doc.processing_status = ProcessingStatus.PROCESSING
    db.commit()

    assert end_processing_job(db, job_id, message="worker lost", cancel=cancel)
    db.commit()

    assert (job.status, job.error_message) == (ended, "worker lost")
    assert doc.processing_status == ProcessingStatus.FAILED
    assert doc.processing_error == "worker lost"


@pytest.mark.parametrize("change", ["finished", "newer_attempt", "deleted"])
def test_end_processing_job_refuses_a_job_someone_else_moved(
    db: Session, change: str
) -> None:
    job_id, job, doc = _seed(db)
    doc.processing_status = ProcessingStatus.PROCESSING
    db.commit()
    # This session now holds the live attempt in memory...
    assert (job.status, job.celery_task_id, job.is_deleted) == (
        JobStatus.RUNNING,
        TASK,
        False,
    )
    # ...and someone else moves it and commits, leaving that snapshot stale.
    other = Session(bind=db.get_bind())
    moved: Any = other.get(ProcessingJob, job_id)
    if change == "finished":
        moved.status = JobStatus.COMPLETED
    elif change == "newer_attempt":
        moved.celery_task_id = "task-2"
    else:
        moved.soft_delete()
    other.commit()
    after = (moved.status, moved.celery_task_id, moved.is_deleted)
    other.close()

    assert not end_processing_job(
        db, job_id, message="late", expected_task_ids=(None, TASK)
    )
    db.rollback()

    assert (job.status, job.celery_task_id, job.is_deleted) == after
    assert doc.processing_status == ProcessingStatus.PROCESSING


@pytest.mark.parametrize("owner", ["active_sibling", "not_ingestion"])
def test_end_processing_job_leaves_documents_it_does_not_own(
    db: Session, owner: str
) -> None:
    job_id, job, doc = _seed(db)
    doc.processing_status = ProcessingStatus.PROCESSING
    if owner == "active_sibling":
        db.add(
            ProcessingJob(
                job_type=JobType.DOCUMENT_INGESTION,
                status=JobStatus.QUEUED,
                organization_id=job.organization_id,
                document_id=doc.id,
                celery_task_id="task-2",
                total_steps=5,
            )
        )
    else:
        job.job_type = JobType.GRAPH_INDEXING
    db.commit()

    assert end_processing_job(db, job_id, message="ended")
    db.commit()

    assert job.status == JobStatus.FAILED
    assert doc.processing_status == ProcessingStatus.PROCESSING


def test_status_sets_match_the_model_predicates() -> None:
    """The helper's status sets stay in step with the models' own predicates."""
    assert NON_TERMINAL_JOB_STATUSES == {
        status for status in JobStatus if not ProcessingJob(status=status).is_finished
    }
    assert ACTIVE_JOB_STATUSES == {
        status for status in JobStatus if ProcessingJob(status=status).is_active
    }
    assert set(ACTIVE_DOCUMENT_STATUSES) == {
        status
        for status in ProcessingStatus
        if not Document(processing_status=status).is_processing_complete
    }
