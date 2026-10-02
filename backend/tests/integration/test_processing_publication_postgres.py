"""GOO-355: real PostgreSQL publication/worker interleavings.

Set INGESTION_TEST_DATABASE_URL to a disposable PostgreSQL database, or use
the shared postgres_container fixture. Each test owns an isolated schema.
Only broker transport is mocked; reservation, worker claim and commits are real.

Verification receipt (2026-10-02; base 4e47ec0d66bf76b08a14da502049041c3cc8107c):
Before repair, worker COMPLETED/RUNNING/CANCELLED became QUEUED, duplicate
producers sent twice, and broker errors overwrote worker/new-attempt state.
Independent review also reproduced same-session MissingGreenlet on no-op
rollback; the caller-response regression pins its repair.

Mutation verification: 18 isolated processes recompiled this method's source
with one guard removed, each named regression failed for the intended defect,
then the original method was restored. Tracked source stayed byte-identical.
Source: backend/src/services/processing/processing_service.py (queue method).
Use the following exact focused command with each listed test selector after
temporarily disabling its listed guard; restore source, rerun, require PASS:
  PYTHONPATH=backend pytest -q backend/tests/integration/test_processing_publication_postgres.py::<test>
Unit selectors (U) instead use:
  PYTHONPATH=backend pytest -q backend/tests/services/processing/test_processing_service_async.py::<test>

Guard line(s) -> named selector (all 18 guard removals observed failing):
171 reservation commit -> U:test_queue_processing_job_sends_and_commits
154 fresh status -> U:test_queue_processing_job_rejects_dispatched_or_deleted
153 deleted reservation -> U:test_queue_processing_job_rejects_dispatched_or_deleted
155 existing pending task ID -> U:test_queue_processing_job_rejects_dispatched_or_deleted
144 reservation lock -> test_reservation_locks_job_before_commit
145 populate_existing -> test_cached_pending_job_is_refreshed_before_reserving
212 failure status -> test_broker_error_after_acceptance_preserves_worker_state
213 failure task ID -> test_broker_error_cannot_fail_superseding_attempt
211 failure deletion -> test_broker_error_preserves_deleted_job
192 document tenant -> test_broker_error_preserves_foreign_rows[Document]
203 job tenant -> test_broker_error_preserves_foreign_rows[ProcessingJob]
193 document deletion -> test_broker_failure_preserves_terminal_or_deleted_document
218 document active state -> test_broker_failure_preserves_terminal_or_deleted_document
242 competing attempt -> test_broker_failure_preserves_another_active_ingestion
195 document lock -> test_dispatch_failure_locks_document_and_job
205 failure job lock -> test_dispatch_failure_locks_document_and_job
157 read-only completion -> test_noop_publication_preserves_caller_response_fields[False]
215 read-only completion -> test_noop_publication_preserves_caller_response_fields[True]

Completion of a real broker/worker process lifecycle belongs to GOO-360;
this file proves PostgreSQL publication/claim transitions, not deployed health.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.processing.processing_service import ProcessingPipeline
from src.tasks.replay_guard import claim_job_for_processing

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


@pytest.fixture
async def publication_db(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    configured = os.getenv("INGESTION_TEST_DATABASE_URL")
    url = make_url(configured or request.getfixturevalue("postgres_container")["url"])
    assert url.get_backend_name() == "postgresql"
    schema = f"test_publication_{uuid4().hex}"
    admin = create_async_engine(url.set(drivername="postgresql+asyncpg"))
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema}},
    )
    sync_engine = create_engine(
        url.set(drivername="postgresql+psycopg2"),
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE organizations (id UUID PRIMARY KEY)"
            )
            await connection.exec_driver_sql("CREATE TABLE users (id UUID PRIMARY KEY)")
            await connection.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn, tables=[Document.__table__, ProcessingJob.__table__]
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
        async with factory() as db:
            await db.execute(
                text("INSERT INTO organizations VALUES (:id)"), {"id": org_id}
            )
            db.add(
                Document(
                    id=doc_id,
                    organization_id=org_id,
                    title="Publication fixture",
                    filename="fixture.txt",
                    file_path="/unused/fixture.txt",
                    file_size_bytes=1,
                    mime_type="text/plain",
                    document_type=DocumentType.TEXT,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            await db.flush()
            db.add(
                ProcessingJob(
                    id=job_id,
                    organization_id=org_id,
                    document_id=doc_id,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.PENDING,
                    total_steps=1,
                )
            )
            await db.commit()
        yield SimpleNamespace(
            factory=factory,
            sync_factory=sessionmaker(sync_engine, expire_on_commit=False),
            job_id=job_id,
            doc_id=doc_id,
        )
    finally:
        sync_engine.dispose()
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin.dispose()


def _pipeline(db: Any) -> ProcessingPipeline:
    pipeline = ProcessingPipeline.__new__(ProcessingPipeline)
    pipeline.db = db
    return pipeline


def _worker_transition(fixture: Any, status: JobStatus, task_id: str) -> None:
    with fixture.sync_factory() as worker:
        job = worker.get(ProcessingJob, fixture.job_id)
        claim = claim_job_for_processing(worker, job, celery_task_id=task_id)
        assert claim.proceed
        document = worker.get(Document, fixture.doc_id)
        if status == JobStatus.COMPLETED:
            job.complete_job()
            document.processing_status = ProcessingStatus.COMPLETED
        elif status == JobStatus.CANCELLED:
            job.cancel_job()
            document.processing_status = ProcessingStatus.FAILED
            document.processing_error = "Cancelled"
        elif status == JobStatus.FAILED:
            job.fail_job("Worker failure")
            document.processing_status = ProcessingStatus.FAILED
        else:
            document.processing_status = ProcessingStatus.PROCESSING
        worker.commit()


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.RUNNING, JobStatus.CANCELLED]
)
async def test_publish_preserves_worker_terminal_state(
    publication_db: Any, status: JobStatus
) -> None:
    def send(_name: str, **kwargs: Any) -> SimpleNamespace:
        task_id = kwargs.get("task_id", "old-producer-task")
        _worker_transition(publication_db, status, task_id)
        return SimpleNamespace(id=task_id)

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task", side_effect=send
        ) as broker:
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
            with publication_db.sync_factory() as observer:
                job = observer.get(ProcessingJob, publication_db.job_id)
                assert (
                    job.status == status
                ), "Producer regressed the worker's state after publication"
                replay = claim_job_for_processing(
                    observer, job, celery_task_id=job.celery_task_id
                )
                assert (
                    not replay.proceed
                ), "Redelivery must not redo running or terminal work"
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    assert broker.call_count == 1


@pytest.mark.parametrize(
    "status",
    [JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
async def test_broker_error_after_acceptance_preserves_worker_state(
    publication_db: Any, status: JobStatus
) -> None:
    def accepted_then_error(_name: str, **kwargs: Any) -> None:
        _worker_transition(
            publication_db, status, kwargs.get("task_id", "accepted-task")
        )
        raise RuntimeError("Broker response lost after acceptance")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task", side_effect=accepted_then_error
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        document = observer.get(Document, publication_db.doc_id)
        assert job.status == status, "Dispatch error overwrote a worker-owned state"
        expected = {
            JobStatus.RUNNING: ProcessingStatus.PROCESSING,
            JobStatus.COMPLETED: ProcessingStatus.COMPLETED,
            JobStatus.FAILED: ProcessingStatus.FAILED,
            JobStatus.CANCELLED: ProcessingStatus.FAILED,
        }[status]
        assert document.processing_status == expected
        if status == JobStatus.FAILED:
            assert job.error_message == "Worker failure"
        assert (
            not document.processing_error or "Broker" not in document.processing_error
        )


@pytest.mark.parametrize("accepted_then_error", [False, True])
async def test_noop_publication_preserves_caller_response_fields(
    publication_db: Any, accepted_then_error: bool
) -> None:
    def send(_name: str, **kwargs: Any) -> SimpleNamespace:
        if accepted_then_error:
            _worker_transition(publication_db, JobStatus.RUNNING, kwargs["task_id"])
            raise RuntimeError("Broker response lost after acceptance")
        return SimpleNamespace(id=kwargs["task_id"])

    async with publication_db.factory() as producer:
        job = await producer.get(ProcessingJob, publication_db.job_id)
        with patch("src.tasks.celery_app.celery_app.send_task", side_effect=send):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
            if not accepted_then_error:
                await _pipeline(producer).queue_processing_job(
                    str(publication_db.job_id)
                )
        # HTTP response shaping reads these synchronously in the same session.
        assert job.id == publication_db.job_id
        expected = JobStatus.RUNNING if accepted_then_error else JobStatus.QUEUED
        assert job.status == expected


async def test_duplicate_producers_publish_once_with_stale_identity_map(
    publication_db: Any,
) -> None:
    async with publication_db.factory() as first, publication_db.factory() as second:
        stale = await second.get(ProcessingJob, publication_db.job_id)
        await second.commit()
        with patch("src.tasks.celery_app.celery_app.send_task") as broker:
            broker.return_value = SimpleNamespace(id="duplicate-producer-task")
            results = await asyncio.wait_for(
                asyncio.gather(
                    _pipeline(first).queue_processing_job(str(publication_db.job_id)),
                    _pipeline(second).queue_processing_job(str(publication_db.job_id)),
                    return_exceptions=True,
                ),
                timeout=10,
            )
        assert results == [None, None]
        assert (
            broker.call_count == 1
        ), "Concurrent producers published the same attempt twice"
        await second.refresh(stale)
        assert stale.status == JobStatus.QUEUED


async def test_cached_pending_job_is_refreshed_before_reserving(
    publication_db: Any,
) -> None:
    async with publication_db.factory() as first, publication_db.factory() as second:
        stale = await second.get(ProcessingJob, publication_db.job_id)
        await second.commit()
        with patch("src.tasks.celery_app.celery_app.send_task") as broker:
            await _pipeline(first).queue_processing_job(str(publication_db.job_id))
            assert stale.status == JobStatus.PENDING
            await _pipeline(second).queue_processing_job(str(publication_db.job_id))
            assert broker.call_count == 1, "Cached PENDING bypassed the dispatch guard"


async def test_reservation_locks_job_before_commit(publication_db: Any) -> None:
    async with publication_db.factory() as producer:
        commit = producer.commit

        async def check_lock_then_commit() -> None:
            with publication_db.sync_factory() as competitor:
                with pytest.raises(OperationalError) as blocked:
                    competitor.execute(
                        select(ProcessingJob)
                        .where(ProcessingJob.id == publication_db.job_id)
                        .with_for_update(nowait=True)
                    )
                assert (
                    cast(Any, blocked.value.orig).pgcode == "55P03"
                ), "Expected PostgreSQL lock denial"
            await commit()

        with patch.object(producer, "commit", side_effect=check_lock_then_commit):
            with patch("src.tasks.celery_app.celery_app.send_task") as broker:
                await _pipeline(producer).queue_processing_job(
                    str(publication_db.job_id)
                )
                broker.assert_called_once()


async def test_broker_failure_fails_reserved_attempt_and_document(
    publication_db: Any,
) -> None:
    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=RuntimeError("Broker unavailable"),
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        document = observer.get(Document, publication_db.doc_id)
        assert job.status == JobStatus.FAILED
        assert job.celery_task_id, "Failure must retain the reserved attempt identifier"
        assert job.can_retry
        assert document.processing_status == ProcessingStatus.FAILED
        assert "Broker unavailable" in job.error_message


async def test_dispatch_failure_locks_document_and_job(publication_db: Any) -> None:
    async with publication_db.factory() as producer:
        commit = producer.commit
        commits = 0

        async def check_locks_then_commit() -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                for model, row_id in (
                    (Document, publication_db.doc_id),
                    (ProcessingJob, publication_db.job_id),
                ):
                    with publication_db.sync_factory() as competitor:
                        with pytest.raises(OperationalError) as blocked:
                            competitor.execute(
                                select(model)
                                .where(model.id == row_id)
                                .with_for_update(nowait=True)
                            )
                        assert cast(Any, blocked.value.orig).pgcode == "55P03"
            await commit()

        with patch.object(producer, "commit", side_effect=check_locks_then_commit):
            with patch(
                "src.tasks.celery_app.celery_app.send_task",
                side_effect=RuntimeError("Broker unavailable"),
            ):
                await _pipeline(producer).queue_processing_job(
                    str(publication_db.job_id)
                )
        assert commits == 2


async def test_broker_error_cannot_fail_superseding_attempt(
    publication_db: Any,
) -> None:
    def supersede_then_error(_name: str, **kwargs: Any) -> None:
        with publication_db.sync_factory() as retry:
            job = retry.get(ProcessingJob, publication_db.job_id)
            job.celery_task_id = "newer-attempt"
            job.queue_job("document_processing")
            retry.commit()
        raise RuntimeError("Old publication failed")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=supersede_then_error,
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        assert (
            job.status == JobStatus.QUEUED
        ), "Old producer failed the new queued attempt"
        assert job.celery_task_id == "newer-attempt"


async def test_broker_error_preserves_deleted_job(publication_db: Any) -> None:
    def delete_then_error(_name: str, **kwargs: Any) -> None:
        with publication_db.sync_factory() as session:
            session.get(ProcessingJob, publication_db.job_id).is_deleted = True
            session.get(Document, publication_db.doc_id).is_deleted = True
            session.commit()
        raise RuntimeError("Broker unavailable")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task", side_effect=delete_then_error
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        assert job.is_deleted
        assert job.status == JobStatus.QUEUED


@pytest.mark.parametrize("model", [Document, ProcessingJob])
async def test_broker_error_preserves_foreign_rows(
    publication_db: Any, model: Any
) -> None:
    def change_scope_then_error(_name: str, **kwargs: Any) -> None:
        with publication_db.sync_factory() as session:
            foreign_org = uuid4()
            session.execute(
                text("INSERT INTO organizations VALUES (:id)"), {"id": foreign_org}
            )
            row_id = (
                publication_db.doc_id if model is Document else publication_db.job_id
            )
            session.get(model, row_id).organization_id = foreign_org
            session.commit()
        raise RuntimeError("Broker unavailable")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=change_scope_then_error,
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        document = observer.get(Document, publication_db.doc_id)
        assert document.processing_status == ProcessingStatus.PENDING
        if model is ProcessingJob:
            assert (
                observer.get(ProcessingJob, publication_db.job_id).status
                == JobStatus.QUEUED
            )


@pytest.mark.parametrize(
    "deleted,status",
    [(False, ProcessingStatus.COMPLETED), (True, ProcessingStatus.PENDING)],
)
async def test_broker_failure_preserves_terminal_or_deleted_document(
    publication_db: Any, deleted: bool, status: ProcessingStatus
) -> None:
    def change_document_then_error(_name: str, **kwargs: Any) -> None:
        with publication_db.sync_factory() as session:
            document = session.get(Document, publication_db.doc_id)
            document.processing_status = status
            document.is_deleted = deleted
            session.commit()
        raise RuntimeError("Broker unavailable")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=change_document_then_error,
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        document = observer.get(Document, publication_db.doc_id)
        assert document.processing_status == status
        assert document.is_deleted == deleted
        assert document.processing_error is None


async def test_broker_failure_preserves_another_active_ingestion(
    publication_db: Any,
) -> None:
    def start_other_attempt_then_error(_name: str, **kwargs: Any) -> None:
        with publication_db.sync_factory() as session:
            current = session.get(ProcessingJob, publication_db.job_id)
            session.add(
                ProcessingJob(
                    organization_id=current.organization_id,
                    document_id=publication_db.doc_id,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.QUEUED,
                    celery_task_id="other-active-attempt",
                )
            )
            session.commit()
        raise RuntimeError("Broker unavailable")

    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=start_other_attempt_then_error,
        ):
            await _pipeline(producer).queue_processing_job(str(publication_db.job_id))
    with publication_db.sync_factory() as observer:
        assert (
            observer.get(ProcessingJob, publication_db.job_id).status
            == JobStatus.FAILED
        )
        assert (
            observer.get(Document, publication_db.doc_id).processing_status
            == ProcessingStatus.PENDING
        )


async def test_reservation_commit_failure_sends_nothing(publication_db: Any) -> None:
    async with publication_db.factory() as producer:
        with patch.object(
            producer, "commit", side_effect=RuntimeError("Commit failed")
        ):
            with patch("src.tasks.celery_app.celery_app.send_task") as broker:
                with pytest.raises(RuntimeError, match="Commit failed"):
                    await _pipeline(producer).queue_processing_job(
                        str(publication_db.job_id)
                    )
                broker.assert_not_called()
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        assert job.status == JobStatus.PENDING
        assert job.celery_task_id is None


async def test_reserved_but_unsent_attempt_is_recovered_by_sweeper(
    publication_db: Any,
) -> None:
    async with publication_db.factory() as producer:
        with patch(
            "src.tasks.celery_app.celery_app.send_task",
            side_effect=SystemExit("Producer stopped"),
        ):
            with pytest.raises(SystemExit):
                await _pipeline(producer).queue_processing_job(
                    str(publication_db.job_id)
                )
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        assert job.status == JobStatus.QUEUED
        assert job.celery_task_id
        job.updated_at = datetime.utcnow() - timedelta(hours=1)
        observer.commit()
    from src.tasks.processing_tasks import sweep_stuck_processing_jobs

    settings = SimpleNamespace(
        SWEEPERS_ENABLED=True, PROCESSING_JOB_STUCK_AFTER_SECONDS=1800
    )
    with patch("src.tasks.processing_tasks.SessionLocal", publication_db.sync_factory):
        with patch("src.core.config.get_settings", return_value=settings):
            assert sweep_stuck_processing_jobs.run() == {"swept": 1}
    with publication_db.sync_factory() as observer:
        job = observer.get(ProcessingJob, publication_db.job_id)
        assert job.status == JobStatus.FAILED
        assert job.error_type == "StuckJobSweep"
