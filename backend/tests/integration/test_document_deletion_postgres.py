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
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.models.user import User, UserRole
from src.services.documents.file_service import FileService
from src.tasks.replay_guard import claim_job_for_processing

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_QUOTA_BEFORE = 1000
_FILE_SIZE = 100


def _dsn(driver: str) -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
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
        Base.metadata.create_all(sync_engine)
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
