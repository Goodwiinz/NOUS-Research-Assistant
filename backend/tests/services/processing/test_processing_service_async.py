"""ProcessingPipeline async-session regression tests.

``get_processing_service`` injects ``get_db`` (the ASYNC session dependency),
but the class's db-touching methods used sync-only APIs on it:
``self.db.execute(stmt).scalar_one_or_none()`` (unawaited coroutine),
``self.db.query(...)`` (doesn't exist on AsyncSession), and unawaited
``commit()``/``refresh()``. Every FastAPI-wired call path
(process_document, queue_processing_job, get_processing_status,
retry_failed_jobs) crashed at runtime. These tests pin the async conversion.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import uuid
from collections.abc import Iterable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.document import ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.processing.processing_service import ProcessingPipeline

pytestmark = pytest.mark.unit


def _async_db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _result(scalar: object = None, scalars: "Iterable[object]" = ()) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar
    r.scalars.return_value.all.return_value = list(scalars)
    return r


def _pipeline(db: MagicMock) -> ProcessingPipeline:
    # Bypass __init__ side services (FileService touches the filesystem).
    p = ProcessingPipeline.__new__(ProcessingPipeline)
    p.db = db
    return p


# --- the regression itself: every db-touching method must be async ---------


@pytest.mark.parametrize(
    "method",
    [
        "process_document",
        "queue_processing_job",
        "get_processing_status",
        "retry_failed_jobs",
    ],
)
def test_db_touching_methods_are_coroutines(method: str) -> None:
    assert inspect.iscoroutinefunction(getattr(ProcessingPipeline, method))


def test_get_processing_service_wired_to_async_get_db() -> None:
    from src.core.database import get_db
    from src.services.processing.processing_service import get_processing_service

    sig = inspect.signature(get_processing_service)
    assert sig.parameters["db"].default.dependency is get_db


# --- process_document -------------------------------------------------------


def _pending_document() -> MagicMock:
    doc = MagicMock()
    doc.processing_status = ProcessingStatus.PENDING
    doc.document_type.value = "text"
    doc.file_path = "/tmp/x.txt"
    doc.organization_id = uuid.uuid4()
    return doc


def test_process_document_awaits_session_and_queues() -> None:
    db = _async_db()
    db.execute.return_value = _result(scalar=_pending_document())
    pipeline = _pipeline(db)
    pipeline.queue_processing_job = AsyncMock()  # type: ignore[method-assign]

    job = asyncio.run(pipeline.process_document("doc-1", "user-1", "org-1"))

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(job)
    pipeline.queue_processing_job.assert_awaited_once_with(job.id)


def test_process_document_not_found_raises() -> None:
    db = _async_db()
    db.execute.return_value = _result(scalar=None)
    pipeline = _pipeline(db)

    with pytest.raises(ValueError, match="not found"):
        asyncio.run(pipeline.process_document("doc-1", "user-1", "org-1"))


# --- queue_processing_job ---------------------------------------------------


def test_queue_processing_job_missing_job_no_send() -> None:
    db = _async_db()
    db.execute.return_value = _result(scalar=None)
    pipeline = _pipeline(db)

    celery_mod = MagicMock()
    with patch.dict(sys.modules, {"src.tasks.celery_app": celery_mod}):
        asyncio.run(pipeline.queue_processing_job("job-1"))

    celery_mod.celery_app.send_task.assert_not_called()
    db.commit.assert_awaited_once()


def test_queue_processing_job_sends_and_commits() -> None:
    job = _pending_job()
    db = _async_db()
    db.execute.return_value = _result(scalar=job)
    pipeline = _pipeline(db)

    celery_mod = MagicMock()
    events = []
    db.commit.side_effect = lambda: events.append("commit")

    def send(*args: object, **kwargs: object) -> SimpleNamespace:
        events.append("send")
        return SimpleNamespace(id="task-1")

    celery_mod.celery_app.send_task.side_effect = send
    with patch.dict(sys.modules, {"src.tasks.celery_app": celery_mod}):
        asyncio.run(pipeline.queue_processing_job("job-1"))

    assert events == ["commit", "send"]
    assert uuid.UUID(job.celery_task_id)
    assert job.status == JobStatus.QUEUED
    assert job.queue_name == "document_processing"
    celery_mod.celery_app.send_task.assert_called_once_with(
        "process_document_ingestion",
        args=["job-1"],
        queue="document_processing",
        task_id=job.celery_task_id,
    )
    db.commit.assert_awaited_once()


def _pending_job(**kwargs: object) -> ProcessingJob:
    values = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "job_type": JobType.DOCUMENT_INGESTION,
        "status": JobStatus.PENDING,
        "is_deleted": False,
        "retry_count": 0,
    }
    values.update(kwargs)
    return ProcessingJob(**values)


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RETRYING])
def test_queue_processing_job_reserves_fresh_attempt(status: JobStatus) -> None:
    job = _pending_job(status=status)
    if status == JobStatus.RETRYING:
        job.celery_task_id = "previous-attempt"
        job.retry_count = 1
    db = _async_db()
    db.execute.return_value = _result(scalar=job)
    celery_mod = MagicMock()
    with patch.dict(sys.modules, {"src.tasks.celery_app": celery_mod}):
        asyncio.run(_pipeline(db).queue_processing_job(str(job.id)))
    assert job.status == JobStatus.QUEUED
    assert job.celery_task_id != "previous-attempt"
    assert db.execute.call_args.args[0]._for_update_arg is not None
    assert db.execute.call_args.args[0].get_execution_options()["populate_existing"]


@pytest.mark.parametrize(
    "values",
    [
        {"status": status}
        for status in (
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )
    ]
    + [{"is_deleted": True}, {"celery_task_id": "already-dispatched"}],
)
def test_queue_processing_job_rejects_dispatched_or_deleted(values: dict) -> None:
    job = _pending_job(**values)
    db = _async_db()
    db.execute.return_value = _result(scalar=job)
    celery_mod = MagicMock()
    with patch.dict(sys.modules, {"src.tasks.celery_app": celery_mod}):
        asyncio.run(_pipeline(db).queue_processing_job(str(job.id)))
    celery_mod.celery_app.send_task.assert_not_called()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_reservation_commit_failure_dispatches_nothing() -> None:
    db = _async_db()
    db.execute.return_value = _result(scalar=_pending_job())
    db.commit.side_effect = RuntimeError("reservation commit failed")
    celery_mod = MagicMock()
    with patch.dict(sys.modules, {"src.tasks.celery_app": celery_mod}):
        with pytest.raises(RuntimeError, match="reservation commit failed"):
            asyncio.run(_pipeline(db).queue_processing_job("job-1"))
    celery_mod.celery_app.send_task.assert_not_called()
    db.rollback.assert_awaited_once()


# --- get_processing_status --------------------------------------------------


def test_get_processing_status_not_found() -> None:
    db = _async_db()
    db.execute.return_value = _result(scalar=None)
    pipeline = _pipeline(db)

    assert asyncio.run(pipeline.get_processing_status("doc-1")) == {
        "error": "Document not found"
    }


def test_get_processing_status_returns_jobs() -> None:
    doc = MagicMock()
    doc.processing_status.value = "completed"
    doc.is_embedded = True
    doc.is_indexed = True
    doc.processing_error = None
    job = MagicMock()
    job.to_dict.return_value = {"id": "job-1"}

    db = _async_db()
    db.execute.side_effect = [_result(scalar=doc), _result(scalars=[job])]
    pipeline = _pipeline(db)

    status = asyncio.run(pipeline.get_processing_status("doc-1"))
    assert status["processing_status"] == "completed"
    assert status["jobs"] == [{"id": "job-1"}]


# --- retry_failed_jobs ------------------------------------------------------


def test_retry_failed_jobs_retries_eligible() -> None:
    retryable = MagicMock(can_retry=True)
    stuck = MagicMock(can_retry=False)
    db = _async_db()
    db.execute.return_value = _result(scalars=[retryable, stuck])
    pipeline = _pipeline(db)
    pipeline.queue_processing_job = AsyncMock()  # type: ignore[method-assign]

    count = asyncio.run(pipeline.retry_failed_jobs(organization_id="org-1"))

    assert count == 1
    retryable.retry_job.assert_called_once()
    stuck.retry_job.assert_not_called()
    pipeline.queue_processing_job.assert_awaited_once_with(retryable.id)
    db.commit.assert_awaited_once()
