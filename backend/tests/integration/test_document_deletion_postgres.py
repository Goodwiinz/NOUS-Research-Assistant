"""Real-Postgres proof that deleting a document stops its ingestion (GOO-356).

Deletion used to soft-delete the document and mark its jobs ``is_deleted``
without cancelling them, and the worker claim never looked at deletion, so a
QUEUED job delivered after the delete still ran. Two concurrent deletes also
both decremented the organization's storage quota. These tests need the row
locks and ``greatest()`` of a real PostgreSQL; set
``ORCHESTRATION_TEST_DATABASE_URL`` to run them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.models.user import User, UserRole
from src.services.documents.file_service import FileService
from src.tasks.replay_guard import claim_job_for_processing

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_QUOTA_BEFORE = 1000
_FILE_SIZE = 100


def _dsn(driver: str) -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql://"):
        if dsn.startswith(prefix):
            return f"postgresql+{driver}://" + dsn[len(prefix) :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@asynccontextmanager
async def _schema() -> AsyncIterator[SimpleNamespace]:
    """An isolated schema seeded with one org, its owner, a PENDING document
    and that document's QUEUED ingestion job."""
    schema = "doc_delete_" + uuid.uuid4().hex
    admin = create_engine(_dsn("psycopg2"))
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    sync_engine = create_engine(
        _dsn("psycopg2"), connect_args={"options": f"-csearch_path={schema}"}
    )
    async_engine = create_async_engine(
        _dsn("asyncpg"), connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        Base.metadata.create_all(
            sync_engine,
            tables=[
                Organization.__table__,
                User.__table__,
                Document.__table__,
                ProcessingJob.__table__,
                Entity.__table__,
            ],
        )
        ids = SimpleNamespace(
            org=uuid.uuid4(), user=uuid.uuid4(), doc=uuid.uuid4(), job=uuid.uuid4()
        )
        SyncSession = sessionmaker(sync_engine, expire_on_commit=False)
        with SyncSession() as db:
            db.add(
                Organization(
                    id=ids.org,
                    name="Org",
                    storage_limit_bytes=10_000,
                    storage_used_bytes=_QUOTA_BEFORE,
                )
            )
            db.flush()
            db.add(
                User(
                    id=ids.user,
                    email=f"{ids.user}@example.com",
                    password_hash="x",
                    first_name="A",
                    last_name="B",
                    organization_id=ids.org,
                    role=UserRole.USER,
                )
            )
            db.flush()
            db.add(
                Document(
                    id=ids.doc,
                    title="Doc",
                    filename="doc.txt",
                    file_path="local:///nonexistent/doc.txt",
                    file_size_bytes=_FILE_SIZE,
                    mime_type="text/plain",
                    document_type=DocumentType.TEXT,
                    organization_id=ids.org,
                    uploaded_by_user_id=ids.user,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            db.flush()
            db.add(
                ProcessingJob(
                    id=ids.job,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.QUEUED,
                    organization_id=ids.org,
                    document_id=ids.doc,
                    created_by_user_id=ids.user,
                    parameters={"document_id": str(ids.doc)},
                    celery_task_id="task-1",
                    total_steps=5,
                )
            )
            db.commit()
        yield SimpleNamespace(
            ids=ids,
            sync=SyncSession,
            async_=async_sessionmaker(async_engine, expire_on_commit=False),
            sync_engine=sync_engine,
            async_engine=async_engine,
        )
    finally:
        await async_engine.dispose()
        sync_engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


async def _delete_as_owner(env: SimpleNamespace) -> None:
    async with env.async_() as db:
        document = (
            await db.execute(select(Document).where(Document.id == env.ids.doc))
        ).scalar_one()
        user = await db.get(User, env.ids.user)
        await FileService(db).delete_file(document, user)


def _quota(env: SimpleNamespace) -> int:
    with env.sync() as db:
        return int(db.get(Organization, env.ids.org).storage_used_bytes)


@pytest.fixture(autouse=True)
def _no_side_effects() -> Iterator[MagicMock]:
    """Storage objects, DO KB and Neo4j are out of scope; Celery is observed."""
    with (
        patch.object(FileService, "delete_physical_file"),
        patch.object(FileService, "_cleanup_satellites_on_delete", AsyncMock()),
        patch("src.tasks.processing_tasks.current_app") as app,
    ):
        yield app


async def test_deleted_ingestion_is_not_claimed(
    _no_side_effects: MagicMock,
) -> None:
    async with _schema() as env:
        await _delete_as_owner(env)

        # The broker still delivers the message after the delete committed.
        with env.sync() as db:
            job = db.get(ProcessingJob, env.ids.job)
            claim = claim_job_for_processing(db, job, worker_id="w", celery_task_id="t")
            assert claim.proceed is False

            job = db.get(ProcessingJob, env.ids.job)
            assert job.status == JobStatus.CANCELLED
            assert job.is_deleted is True
            assert db.get(Document, env.ids.doc).is_deleted is True

        assert _quota(env) == _QUOTA_BEFORE - _FILE_SIZE
        _no_side_effects.control.revoke.assert_called_once_with(
            "task-1", terminate=True
        )

        # Deleting again is refused before the quota moves a second time.
        with pytest.raises(HTTPException) as exc:
            await _delete_as_owner(env)
        assert exc.value.status_code == 404
        assert _quota(env) == _QUOTA_BEFORE - _FILE_SIZE


async def test_concurrent_deletes_release_quota_once() -> None:
    async with _schema() as env:
        results = await asyncio.gather(
            _delete_as_owner(env), _delete_as_owner(env), return_exceptions=True
        )

        refused = [r for r in results if isinstance(r, HTTPException)]
        assert len(refused) == 1, results
        assert refused[0].status_code == 404
        assert _quota(env) == _QUOTA_BEFORE - _FILE_SIZE


async def test_non_cascade_delete_still_cancels_ingestion() -> None:
    async with _schema() as env:
        async with env.async_() as db:
            deleted = await FileService(db).soft_delete_documents(
                env.ids.org, [env.ids.doc], cascade=False
            )
        assert [d.id for d in deleted] == [env.ids.doc]

        with env.sync() as db:
            job = db.get(ProcessingJob, env.ids.job)
            assert job.status == JobStatus.CANCELLED
            assert job.is_deleted is False  # cascade=False keeps the row live
            claim = claim_job_for_processing(db, job, worker_id="w", celery_task_id="t")
            assert claim.proceed is False


async def _task_id(env: SimpleNamespace) -> str:
    task_id = str(uuid.uuid4())
    async with env.async_() as db:
        job = await db.get(ProcessingJob, env.ids.job)
        job.celery_task_id = task_id
        await db.commit()
    return task_id


@pytest.mark.parametrize("revoke_failure", [False, True])
async def test_task_id_cancellation_fails_retained_document(
    _no_side_effects: MagicMock, revoke_failure: bool
) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        observed_states = []

        def revoke(*args: object, **kwargs: object) -> None:
            # The broker sees the transaction from a separate connection.
            with env.sync() as observer:
                observed_states.append(
                    (
                        observer.get(ProcessingJob, env.ids.job).status,
                        observer.get(Document, env.ids.doc).processing_status,
                    )
                )
            if revoke_failure:
                raise RuntimeError("Synthetic broker outage")

        _no_side_effects.control.revoke.side_effect = revoke
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            result = await cancel_upload(task_id, user, db, FileService(db))
            assert result["message"] == "Upload cancelled successfully"
            repeated = await cancel_upload(task_id, user, db, FileService(db))
            assert repeated["job_status"] == "cancelled"

        with env.sync() as observer:
            document = observer.get(Document, env.ids.doc)
            assert (
                document.processing_status == ProcessingStatus.FAILED
            ), "Cancellation left the retained document processing"
            assert not document.is_deleted
            job = observer.get(ProcessingJob, env.ids.job)
            assert job.status == JobStatus.CANCELLED
            cancellation_reason = document.processing_error
            assert not claim_job_for_processing(observer, job).proceed
        assert _quota(env) == _QUOTA_BEFORE
        assert observed_states == [
            (JobStatus.CANCELLED, ProcessingStatus.FAILED)
        ], "Broker revocation ran before cancellation was committed"
        assert "cancel" in cancellation_reason.lower()
        assert _no_side_effects.control.revoke.call_count == 1


@pytest.mark.parametrize("operation", ["task", "document"])
async def test_task_id_cancellation_preserves_concurrent_completion(
    operation: str,
) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        async with env.async_() as db:
            cached = await db.get(ProcessingJob, env.ids.job)
            user = await db.get(User, env.ids.user)
            document = await db.get(Document, env.ids.doc)
            cached.error_message = "Stale cached update"
            with env.sync() as worker:
                worker.get(ProcessingJob, env.ids.job).complete_job()
                worker.get(Document, env.ids.doc).processing_status = (
                    ProcessingStatus.COMPLETED
                )
                worker.commit()
            assert cached.status == JobStatus.QUEUED
            cached.status = JobStatus.PENDING  # An unflushed stale caller update.

            if operation == "task":
                result = await cancel_upload(task_id, user, db, FileService(db))
                assert (
                    result.get("job_status") == "completed"
                ), "A stale cancellation overwrote the worker's completion"
            else:
                assert await FileService(db).delete_file(document, user)
        with env.sync() as observer:
            assert (
                observer.get(ProcessingJob, env.ids.job).status == JobStatus.COMPLETED
            )
            assert (
                observer.get(Document, env.ids.doc).processing_status
                == ProcessingStatus.COMPLETED
            )


async def test_claim_locks_document_before_starting() -> None:
    """A held document lock must prevent the claim from becoming RUNNING."""
    async with _schema() as env:
        with env.sync() as deletion, env.sync() as worker:
            deletion.execute(
                select(Document).where(Document.id == env.ids.doc).with_for_update()
            )
            job = worker.get(ProcessingJob, env.ids.job)
            worker.execute(text("SET LOCAL lock_timeout = '150ms'"))
            with pytest.raises(OperationalError) as exc:
                claim_job_for_processing(worker, job, worker_id="w1")
            assert exc.value.orig.pgcode == "55P03"
            worker.rollback()
            assert worker.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED


async def test_task_id_cancellation_locks_document_before_job() -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        with env.sync() as worker:
            worker.execute(
                select(Document).where(Document.id == env.ids.doc).with_for_update()
            )
            async with env.async_() as db:
                user = await db.get(User, env.ids.user)
                await db.execute(text("SET LOCAL lock_timeout = '150ms'"))
                with pytest.raises(HTTPException) as exc:
                    await cancel_upload(task_id, user, db, FileService(db))
                assert exc.value.status_code == 500
            assert worker.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED


@pytest.mark.parametrize("id_form", ["task", "document", "task_foreign_document"])
async def test_cancellation_denies_foreign_organization(id_form: str) -> None:
    """Uploader identity is not permission to mutate a different tenant."""
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        with env.sync() as seed:
            foreign = Organization(
                name="Foreign", storage_limit_bytes=10_000, storage_used_bytes=1000
            )
            seed.add(foreign)
            seed.flush()
            if id_form == "task":
                seed.get(ProcessingJob, env.ids.job).organization_id = foreign.id
            else:
                seed.get(Document, env.ids.doc).organization_id = foreign.id
            seed.commit()
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            with pytest.raises(HTTPException) as exc:
                await cancel_upload(
                    str(env.ids.doc) if id_form == "document" else task_id,
                    user,
                    db,
                    FileService(db),
                )
            assert exc.value.status_code == 404
        with env.sync() as observer:
            assert observer.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED
            assert not observer.get(Document, env.ids.doc).is_deleted
        assert _quota(env) == _QUOTA_BEFORE


async def test_claim_does_not_lock_job_while_waiting_for_document() -> None:
    """A delete holding Document must still be able to lock its job."""
    async with _schema() as env:
        attempting_document = Event()

        def observe_document_lock(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if "FOR UPDATE" in statement and "FROM documents" in statement:
                attempting_document.set()

        def claim() -> bool:
            with env.sync() as worker:
                worker.execute(text("SET LOCAL lock_timeout = '3s'"))
                job = worker.get(ProcessingJob, env.ids.job)
                job.error_message = "Stale cached update"
                return claim_job_for_processing(worker, job).proceed

        with ThreadPoolExecutor(max_workers=1) as pool:
            with env.sync() as deletion:
                deletion.execute(
                    select(Document).where(Document.id == env.ids.doc).with_for_update()
                )
                event.listen(
                    env.sync_engine, "before_cursor_execute", observe_document_lock
                )
                future = pool.submit(claim)
                try:
                    assert attempting_document.wait(
                        3
                    ), "Claim never attempted its document lock"
                    # NOWAIT is deterministic: job-first locking would block
                    # this delete and form a Document <-> Job deadlock.
                    deletion.execute(
                        select(ProcessingJob)
                        .where(ProcessingJob.id == env.ids.job)
                        .with_for_update(nowait=True)
                    )
                finally:
                    deletion.rollback()
                    event.remove(
                        env.sync_engine, "before_cursor_execute", observe_document_lock
                    )
            assert future.result(timeout=5)


async def test_cancellation_commit_failure_rolls_back_before_revoke(
    _no_side_effects: MagicMock,
) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)

            async def fail_commit() -> None:
                await db.flush()
                raise RuntimeError("Synthetic commit failure")

            with (
                patch.object(db, "commit", side_effect=fail_commit),
                patch.object(db, "rollback", wraps=db.rollback) as rollback,
            ):
                with pytest.raises(HTTPException) as exc:
                    await cancel_upload(task_id, user, db, FileService(db))
                assert exc.value.status_code == 500
                assert rollback.await_count == 1, "Service must own the rollback"
        with env.sync() as observer:
            assert observer.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED
            assert (
                observer.get(Document, env.ids.doc).processing_status
                == ProcessingStatus.PENDING
            )
        _no_side_effects.control.revoke.assert_not_called()


@pytest.mark.parametrize("operation", ["task", "document"])
async def test_cancellation_does_not_lock_job_while_waiting_for_document(
    operation: str,
) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        attempting_document = asyncio.Event()

        def observe_document_lock(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if "FOR UPDATE" in statement and "FROM documents" in statement:
                attempting_document.set()

        async def cancel():
            async with env.async_() as db:
                user = await db.get(User, env.ids.user)
                document = await db.get(Document, env.ids.doc)
                job = await db.get(ProcessingJob, env.ids.job)
                await db.execute(text("SET LOCAL lock_timeout = '3s'"))
                # SQLAlchemy 2.1 autoflushes textual SQL too. Configure the
                # timeout before dirtying the job so setup cannot take its lock.
                job.error_message = "Stale cached update"
                if operation == "document":
                    return await FileService(db).delete_file(document, user)
                return await cancel_upload(task_id, user, db, FileService(db))

        with env.sync() as deletion:
            deletion.execute(
                select(Document).where(Document.id == env.ids.doc).with_for_update()
            )
            event.listen(
                env.async_engine.sync_engine,
                "before_cursor_execute",
                observe_document_lock,
            )
            pending = asyncio.create_task(cancel())
            try:
                await asyncio.wait_for(attempting_document.wait(), timeout=2)
                deletion.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.id == env.ids.job)
                    .with_for_update(nowait=True)
                )
            finally:
                deletion.rollback()
                event.remove(
                    env.async_engine.sync_engine,
                    "before_cursor_execute",
                    observe_document_lock,
                )
                result = await pending
        if operation == "document":
            assert result is True
        else:
            assert result["message"] == "Upload cancelled successfully"


async def test_cancellation_locks_job_before_checking_its_state() -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            execute = db.execute
            checked = False

            async def observe_locked_job(statement, *args, **kwargs):
                nonlocal checked
                result = await execute(statement, *args, **kwargs)
                columns = getattr(statement, "column_descriptions", [])
                if len(columns) == 1 and columns[0]["entity"] is ProcessingJob:
                    with env.sync() as publisher:
                        with pytest.raises(OperationalError) as exc:
                            publisher.execute(
                                select(ProcessingJob)
                                .where(ProcessingJob.id == env.ids.job)
                                .with_for_update(nowait=True)
                            )
                        assert exc.value.orig.pgcode == "55P03"
                    checked = True
                return result

            with patch.object(db, "execute", side_effect=observe_locked_job):
                await cancel_upload(task_id, user, db, FileService(db))
            assert checked, "Cancellation never read a locked job"


@pytest.mark.parametrize("change", ["task", "document", "deleted"])
async def test_task_id_cancellation_rechecks_dispatch_identity(
    _no_side_effects: MagicMock,
    change: str,
) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        newer_task = str(uuid.uuid4())
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            execute = db.execute

            async def supersede_after_lookup(statement, *args, **kwargs):
                result = await execute(statement, *args, **kwargs)
                columns = getattr(statement, "column_descriptions", [])
                if len(columns) == 2 and columns[0]["entity"] is ProcessingJob:
                    with env.sync() as publisher:
                        job = publisher.get(ProcessingJob, env.ids.job)
                        if change == "task":
                            job.celery_task_id = newer_task
                        elif change == "document":
                            job.document_id = None
                        else:
                            job.soft_delete()
                        publisher.commit()
                return result

            with patch.object(db, "execute", side_effect=supersede_after_lookup):
                with pytest.raises(HTTPException) as exc:
                    await cancel_upload(task_id, user, db, FileService(db))
                assert exc.value.status_code == 404
        with env.sync() as observer:
            job = observer.get(ProcessingJob, env.ids.job)
            assert job.celery_task_id == (newer_task if change == "task" else task_id)
            assert job.status == JobStatus.QUEUED
            assert (
                observer.get(Document, env.ids.doc).processing_status
                == ProcessingStatus.PENDING
            )
        _no_side_effects.control.revoke.assert_not_called()


@pytest.mark.parametrize("document_state", ["deleted", "completed"])
async def test_task_id_cancellation_refreshes_document(document_state: str) -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        async with env.async_() as db:
            document = await db.get(Document, env.ids.doc)
            user = await db.get(User, env.ids.user)
            with env.sync() as writer:
                current = writer.get(Document, env.ids.doc)
                if document_state == "deleted":
                    current.soft_delete()
                else:
                    current.processing_status = ProcessingStatus.COMPLETED
                writer.commit()
            assert document.processing_status == ProcessingStatus.PENDING
            assert not document.is_deleted
            if document_state == "deleted":
                with pytest.raises(HTTPException) as exc:
                    await cancel_upload(task_id, user, db, FileService(db))
                assert exc.value.status_code == 404
            else:
                await cancel_upload(task_id, user, db, FileService(db))
        with env.sync() as observer:
            document = observer.get(Document, env.ids.doc)
            job = observer.get(ProcessingJob, env.ids.job)
            if document_state == "deleted":
                assert document.is_deleted
                assert job.status == JobStatus.QUEUED
            else:
                assert document.processing_status == ProcessingStatus.COMPLETED
                assert job.status == JobStatus.CANCELLED


async def test_claim_refreshes_deleted_document() -> None:
    async with _schema() as env:
        with env.sync() as worker:
            document = worker.get(Document, env.ids.doc)
            job = worker.get(ProcessingJob, env.ids.job)
            with env.sync() as deletion:
                deletion.get(Document, env.ids.doc).soft_delete()
                deletion.commit()
            assert not document.is_deleted
            result = claim_job_for_processing(worker, job)
            assert not result.proceed, "Claim used a stale live document"
            assert result.reason == "deleted"
            assert worker.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED


async def test_claim_rechecks_document_association() -> None:
    async with _schema() as env:
        with env.sync() as worker:
            job = worker.get(ProcessingJob, env.ids.job)
            execute = worker.execute

            def unlink_after_lookup(statement, *args, **kwargs):
                result = execute(statement, *args, **kwargs)
                columns = getattr(statement, "column_descriptions", [])
                if len(columns) == 2 and columns[0]["entity"] is ProcessingJob:
                    with env.sync() as writer:
                        writer.get(ProcessingJob, env.ids.job).document_id = None
                        writer.commit()
                return result

            with patch.object(worker, "execute", side_effect=unlink_after_lookup):
                result = claim_job_for_processing(worker, job)
            assert not result.proceed, "Claim used the previously associated document"
            assert result.reason == "missing"
            assert worker.get(ProcessingJob, env.ids.job).status == JobStatus.QUEUED


async def test_cancelled_old_job_preserves_another_active_ingestion() -> None:
    from src.api.documents.files import cancel_upload

    async with _schema() as env:
        task_id = await _task_id(env)
        with env.sync() as seed:
            seed.add(
                ProcessingJob(
                    id=uuid.uuid4(),
                    organization_id=env.ids.org,
                    document_id=env.ids.doc,
                    job_type=JobType.DOCUMENT_INGESTION,
                    status=JobStatus.RUNNING,
                )
            )
            seed.commit()
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            result = await cancel_upload(task_id, user, db, FileService(db))
            assert result["message"] == "Upload cancelled successfully"
        with env.sync() as observer:
            assert (
                observer.get(ProcessingJob, env.ids.job).status == JobStatus.CANCELLED
            )
            assert (
                observer.get(Document, env.ids.doc).processing_status
                == ProcessingStatus.PENDING
            )


@pytest.mark.parametrize("cascade", [True, False])
async def test_document_id_cancellation_preserves_cascade_and_tenant_scope(
    cascade: bool,
) -> None:
    from src.api.documents.documents import delete_document

    async with _schema() as env:
        with env.sync() as seed:
            foreign = Organization(
                name="Foreign", storage_limit_bytes=10_000, storage_used_bytes=1000
            )
            seed.add(foreign)
            seed.flush()
            foreign_job = ProcessingJob(
                id=uuid.uuid4(),
                organization_id=foreign.id,
                document_id=env.ids.doc,
                job_type=JobType.DOCUMENT_INGESTION,
                status=JobStatus.QUEUED,
            )
            foreign_entity = Entity(
                id=uuid.uuid4(),
                organization_id=foreign.id,
                document_id=env.ids.doc,
                name="Foreign",
                entity_type=EntityType.PERSON,
                extraction_method=ExtractionMethod.MANUAL,
                confidence=1.0,
                extracted_at=datetime.now(timezone.utc),
            )
            own_entity = Entity(
                id=uuid.uuid4(),
                organization_id=env.ids.org,
                document_id=env.ids.doc,
                name="Owned",
                entity_type=EntityType.PERSON,
                extraction_method=ExtractionMethod.MANUAL,
                confidence=1.0,
                extracted_at=datetime.now(timezone.utc),
            )
            seed.add_all([foreign_job, foreign_entity, own_entity])
            seed.commit()
        async with env.async_() as db:
            user = await db.get(User, env.ids.user)
            org = await db.get(Organization, env.ids.org)
            with patch(
                "src.api.documents.documents._cleanup_document_graph", AsyncMock()
            ):
                result = await delete_document(
                    str(env.ids.doc), cascade, user, org, db, FileService(db)
                )
            assert result["cascade_deleted"] == cascade
        with env.sync() as observer:
            assert (
                observer.get(ProcessingJob, env.ids.job).status == JobStatus.CANCELLED
            )
            assert observer.get(ProcessingJob, env.ids.job).is_deleted is cascade
            assert observer.get(Entity, own_entity.id).is_deleted is cascade
            assert (
                observer.get(ProcessingJob, foreign_job.id).status == JobStatus.QUEUED
            )
            assert not observer.get(ProcessingJob, foreign_job.id).is_deleted
            assert not observer.get(Entity, foreign_entity.id).is_deleted
        assert _quota(env) == _QUOTA_BEFORE - _FILE_SIZE


async def test_delete_rechecks_owner_under_lock() -> None:
    async with _schema() as env:
        async with env.async_() as db:
            document = await db.get(Document, env.ids.doc)
            user = await db.get(User, env.ids.user)
            with env.sync() as owner_change:
                document_row = owner_change.get(Document, env.ids.doc)
                document_row.uploaded_by_user_id = None
                owner_change.commit()
            assert document.uploaded_by_user_id == env.ids.user
            with pytest.raises(HTTPException) as exc:
                await FileService(db).delete_file(document, user)
            assert exc.value.status_code == 403
        assert _quota(env) == _QUOTA_BEFORE


@pytest.mark.parametrize("outcome", ["success", "provider-error", "non-cascade"])
async def test_graph_cleanup_preserves_caller_objects_and_pending_writes(
    outcome: str,
) -> None:
    """A post-delete cleanup must not expire or commit its caller's objects."""
    async with _schema() as env:
        with env.sync() as seed:
            document = seed.get(Document, env.ids.doc)
            document.is_deleted = True
            document.document_metadata = {
                "graph_cleanup_requested": outcome != "non-cascade"
            }
            seed.commit()
            original_name = seed.get(User, env.ids.user).first_name
        kg = MagicMock()
        if outcome == "provider-error":
            kg.delete_document_graph.side_effect = OSError("synthetic outage")
        async with env.async_() as db:
            document = await db.get(Document, env.ids.doc)
            user = await db.get(User, env.ids.user)
            user.first_name = "Uncommitted caller change"
            with patch(
                "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
                return_value=kg,
            ):
                assert await FileService(db).cleanup_deleted_document_graph(
                    str(env.ids.doc), str(env.ids.org)
                ) is (outcome != "provider-error")
            assert document.id == env.ids.doc
            assert user.id == env.ids.user
            assert user.first_name == "Uncommitted caller change"
            with env.sync() as observer:
                assert observer.get(User, env.ids.user).first_name == original_name


@pytest.mark.parametrize("cleanup_ok", [False, True])
def test_normal_delete_graph_cleanup_is_durable(cleanup_ok):
    """Delete commits retry intent; an outage remains selected until recovery."""
    from src.tasks.reconcile_tasks import _reconcilable_filters

    async def scenario():
        async with _schema() as env:
            with env.sync() as db:
                doc = db.get(Document, env.ids.doc)
                doc.neo4j_index_status = "completed"
                db.commit()
            kg = MagicMock()
            if not cleanup_ok:
                kg.delete_document_graph.side_effect = RuntimeError("neo4j down")
            async with env.async_() as session:
                service = FileService(session)
                with patch.object(service, "_revoke_tasks"):
                    await service.soft_delete_documents(env.ids.org, [env.ids.doc])
                with env.sync() as check:
                    assert (
                        check.query(Document.id)
                        .filter(*_reconcilable_filters(), Document.id == env.ids.doc)
                        .count()
                        == 1
                    ), "retry intent must be committed before remote cleanup"
                with patch(
                    "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
                    return_value=kg,
                ):
                    await service.cleanup_deleted_document_graph(
                        str(env.ids.doc), str(env.ids.org)
                    )
            with env.sync() as check:
                doc = check.get(Document, env.ids.doc)
                assert doc.is_deleted
                assert doc.neo4j_index_status == (
                    "completed" if cleanup_ok else "failed"
                )
                assert check.query(Document.id).filter(
                    *_reconcilable_filters(), Document.id == env.ids.doc
                ).count() == (0 if cleanup_ok else 1)
            kg.delete_document_graph.assert_called_once_with(
                str(env.ids.doc), str(env.ids.org)
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("denial", ["foreign", "live", "non-cascade"])
def test_graph_cleanup_rechecks_scope_and_delete_intent(denial):
    async def scenario():
        async with _schema() as env:
            org_id = str(env.ids.org)
            with env.sync() as db:
                doc = db.get(Document, env.ids.doc)
                doc.is_deleted = denial != "live"
                doc.neo4j_index_status = "pending"
                doc.document_metadata = {
                    "graph_cleanup_requested": denial != "non-cascade"
                }
                db.commit()
            if denial == "foreign":
                org_id = str(uuid.uuid4())
            with patch(
                "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService"
            ) as kg:
                async with env.async_() as session:
                    assert await FileService(session).cleanup_deleted_document_graph(
                        str(env.ids.doc), org_id
                    )
                kg.return_value.delete_document_graph.assert_not_called()
            with env.sync() as db:
                assert db.get(Document, env.ids.doc).neo4j_index_status == "pending"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["foreign", "live", "non-cascade"])
def test_graph_cleanup_outcome_does_not_overwrite_changed_row(change):
    async def scenario():
        async with _schema() as env:
            with env.sync() as db:
                db.get(Document, env.ids.doc).is_deleted = True
                db.get(Document, env.ids.doc).neo4j_index_status = "pending"
                db.commit()

            def delete_graph(*args):
                with env.sync() as other:
                    doc = other.get(Document, env.ids.doc)
                    if change == "foreign":
                        org_id = uuid.uuid4()
                        other.add(
                            Organization(
                                id=org_id, name="Other", storage_limit_bytes=10_000
                            )
                        )
                        other.flush()
                        doc.organization_id = org_id
                    elif change == "live":
                        doc.is_deleted = False
                    else:
                        doc.document_metadata = {"graph_cleanup_requested": False}
                    other.commit()

            kg = MagicMock()
            kg.delete_document_graph.side_effect = delete_graph
            with patch(
                "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService",
                return_value=kg,
            ):
                async with env.async_() as session:
                    await FileService(session).cleanup_deleted_document_graph(
                        str(env.ids.doc), str(env.ids.org)
                    )
            with env.sync() as db:
                assert db.get(Document, env.ids.doc).neo4j_index_status == "pending"

    asyncio.run(scenario())
