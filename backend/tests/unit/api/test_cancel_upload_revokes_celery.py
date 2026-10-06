"""Cancellation persists before best-effort broker revocation (GOO-356).

SQLite verifies route/service behavior and durable state here. PostgreSQL
interleavings and lock proof live in test_document_deletion_postgres.py.
Only storage, satellite and broker transports are replaced.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.models.user import User, UserRole
from src.services.documents.file_service import FileService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
async def cancel_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def sqlite_functions(connection, _record):
        connection.create_function("greatest", 2, max)

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    Organization.__table__,
                    User.__table__,
                    Document.__table__,
                    ProcessingJob.__table__,
                    Entity.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = SimpleNamespace(org=uuid4(), user=uuid4(), doc=uuid4(), job=uuid4())
    task_id = str(uuid4())
    async with factory() as db:
        db.add(
            Organization(
                id=ids.org,
                name="Test",
                storage_limit_bytes=1000,
                storage_used_bytes=100,
            )
        )
        db.add(
            User(
                id=ids.user,
                email="cancel@example.test",
                password_hash="unused",
                first_name="Test",
                last_name="Owner",
                organization_id=ids.org,
                role=UserRole.USER,
            )
        )
        await db.flush()
        db.add(
            Document(
                id=ids.doc,
                organization_id=ids.org,
                uploaded_by_user_id=ids.user,
                title="Test",
                filename="test.txt",
                file_path="/unused/test.txt",
                file_size_bytes=100,
                mime_type="text/plain",
                document_type=DocumentType.TEXT,
                processing_status=ProcessingStatus.PROCESSING,
            )
        )
        await db.flush()
        db.add(
            ProcessingJob(
                id=ids.job,
                organization_id=ids.org,
                document_id=ids.doc,
                created_by_user_id=ids.user,
                job_type=JobType.DOCUMENT_INGESTION,
                status=JobStatus.QUEUED,
                celery_task_id=task_id,
            )
        )
        await db.commit()
    try:
        yield SimpleNamespace(ids=ids, factory=factory, task_id=task_id)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("broker_failure", [False, True])
async def test_cancel_upload_revokes_celery_task_when_present(
    cancel_db, broker_failure
):
    from src.api.documents.files import cancel_upload

    with patch("src.tasks.processing_tasks.current_app") as app:
        if broker_failure:
            app.control.revoke.side_effect = RuntimeError("Synthetic broker outage")
        async with cancel_db.factory() as db:
            user = await db.get(User, cancel_db.ids.user)
            result = await cancel_upload(cancel_db.task_id, user, db, FileService(db))
        app.control.revoke.assert_called_once_with(cancel_db.task_id, terminate=True)
    assert result["message"] == "Upload cancelled successfully"
    async with cancel_db.factory() as observer:
        job = await observer.get(ProcessingJob, cancel_db.ids.job)
        document = await observer.get(Document, cancel_db.ids.doc)
        assert job.status == JobStatus.CANCELLED
        assert document.processing_status == ProcessingStatus.FAILED
        assert "cancel" in document.processing_error.lower()
        assert not document.is_deleted


async def test_cancel_upload_skips_revoke_when_no_celery_task_id(cancel_db):
    from src.api.documents.files import cancel_upload

    async with cancel_db.factory() as db:
        job = await db.get(ProcessingJob, cancel_db.ids.job)
        job.celery_task_id = None
        await db.commit()
        user = await db.get(User, cancel_db.ids.user)
        with (
            patch("src.tasks.processing_tasks.current_app") as app,
            patch.object(FileService, "delete_physical_file"),
            patch.object(FileService, "_cleanup_satellites_on_delete", AsyncMock()),
        ):
            result = await cancel_upload(
                str(cancel_db.ids.doc), user, db, FileService(db)
            )
        app.control.revoke.assert_not_called()
    assert result["message"] == "Document upload cancelled successfully"
    async with cancel_db.factory() as observer:
        assert (
            await observer.get(ProcessingJob, cancel_db.ids.job)
        ).status == JobStatus.CANCELLED
        assert (await observer.get(Document, cancel_db.ids.doc)).is_deleted
        assert (
            await observer.get(Organization, cancel_db.ids.org)
        ).storage_used_bytes == 0
