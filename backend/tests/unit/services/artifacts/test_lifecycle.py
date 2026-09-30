"""Artifact lifecycle: outbox announcements to open runs and reservation sweeps."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.artifact import (
    Artifact,
    ArtifactLifecycleOutbox,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import (
    ArtifactProvenance,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.artifacts import lifecycle, service
from src.services.artifacts.lifecycle import (
    drain_artifact_outbox,
    sweep_artifact_uploads,
)
from src.services.artifacts.service import (
    PROJECT_QUOTA_BYTES,
    publish_version,
    reserve_upload,
    store_upload,
)
from src.services.artifacts.storage import MemoryArtifactStorage
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, WORKSPACE, CONVERSATION, THREAD = (uuid4() for _ in range(6))
RUN_OPEN, RUN_DONE = str(uuid4()), str(uuid4())
CONTENT = b"report\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        AgentRun,
        AgentRunEvent,
        Artifact,
        ArtifactVersion,
        ArtifactUpload,
        ArtifactReference,
        ArtifactLifecycleOutbox,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(
                id=ORG, name="Test", storage_limit_bytes=1000000
            )
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'owner@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(USER), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="Workspace", owner_id=USER, organization_id=ORG
            )
        )
        await session.execute(
            insert(Collection).values(
                id=PROJECT, name="Project", workspace_id=WORKSPACE
            )
        )
        await session.execute(
            insert(Conversation).values(
                id=CONVERSATION,
                workspace_id=WORKSPACE,
                title="Conversation",
                created_by_id=USER,
            )
        )
        await session.execute(
            insert(Thread).values(
                id=THREAD, conversation_id=CONVERSATION, created_by_id=USER
            )
        )
        for job_id, status in (
            (RUN_OPEN, JobStatus.RUNNING),
            (RUN_DONE, JobStatus.COMPLETED),
        ):
            await session.execute(
                insert(AgentRun).values(
                    job_id=job_id,
                    organization_id=ORG,
                    user_id=USER,
                    thread_id=THREAD,
                    project_id=PROJECT,
                    status=status.value,
                )
            )
        await session.commit()
        await append_event(
            session,
            run_id=RUN_DONE,
            event_type=RunEventType.RUN_COMPLETED,
            payload={},
            organization_id=ORG,
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def storage() -> MemoryArtifactStorage:
    return MemoryArtifactStorage()


@pytest.fixture(autouse=True)
def _use_memory_storage(
    monkeypatch: pytest.MonkeyPatch, storage: MemoryArtifactStorage
) -> None:
    monkeypatch.setattr(service, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(lifecycle, "get_artifact_storage", lambda: storage)


def _context(run_id: str | None) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        thread_id=THREAD,
        run_id=UUID(run_id) if run_id else None,
        grant_id=uuid4(),
    )


async def _publish(db: AsyncSession, context: IntegrationContext) -> Any:
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(CONTENT),
        mime_type="text/markdown",
        sha256=DIGEST,
    )
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    return await publish_version(
        db,
        context,
        PublishVersionRequest(
            publication_id=reserve.publication_id,
            upload_id=upload.upload_id,
            title="report.md",
            provenance=ArtifactProvenance(producer="harness"),
        ),
    )


async def _events(db: AsyncSession, run_id: str) -> list[AgentRunEvent]:
    return list(
        (
            await db.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.seq)
            )
        ).all()
    )


async def test_publish_enqueues_one_lifecycle_row_in_the_same_commit(
    db: AsyncSession,
) -> None:
    version = await _publish(db, _context(RUN_OPEN))
    rows = list((await db.scalars(select(ArtifactLifecycleOutbox))).all())
    assert [(r.version_id, r.kind, r.status, r.run_id) for r in rows] == [
        (version.version_id, "artifact.version_created", "pending", RUN_OPEN)
    ]
    # Nothing announced until the drain runs.
    assert await _events(db, RUN_OPEN) == []


async def test_drain_announces_once_to_an_open_run(db: AsyncSession) -> None:
    version = await _publish(db, _context(RUN_OPEN))
    assert await drain_artifact_outbox(db) == 1
    events = await _events(db, RUN_OPEN)
    assert [e.event_type for e in events] == ["artifact.version_created"]
    assert events[0].payload == {
        "artifact_id": str(version.artifact_id),
        "version_id": str(version.version_id),
    }
    assert await drain_artifact_outbox(db) == 0
    assert len(await _events(db, RUN_OPEN)) == 1
    row = await db.scalar(select(ArtifactLifecycleOutbox))
    assert (
        row is not None and row.status == "delivered" and row.delivered_at is not None
    )


@pytest.mark.parametrize("run_id", [RUN_DONE, None], ids=["closed-run", "no-run"])
async def test_closed_or_unbound_runs_keep_the_row_but_announce_nothing(
    db: AsyncSession, run_id: str | None
) -> None:
    version = await _publish(db, _context(run_id))
    assert await drain_artifact_outbox(db) == 0
    assert [e.event_type for e in await _events(db, RUN_DONE)] == ["run.completed"]
    row = await db.scalar(select(ArtifactLifecycleOutbox))
    assert (
        row is not None
        and row.status == "skipped"
        and row.version_id == version.version_id
    )
    # Late outputs stay discoverable through the durable reference, not the ledger.
    assert await db.scalar(select(func.count()).select_from(ArtifactReference)) == 1


async def test_sweep_removes_expired_unfinalized_reservations_and_their_bytes(
    db: AsyncSession, storage: MemoryArtifactStorage
) -> None:
    context = _context(RUN_OPEN)
    published = await _publish(db, context)
    finalized_upload_id = (await db.get(ArtifactVersion, published.version_id)).upload_id  # type: ignore[union-attr]
    stale = await reserve_upload(
        db,
        context,
        ReserveArtifactUploadRequest(
            publication_id=uuid4(),
            byte_size=len(CONTENT),
            mime_type="text/plain",
            sha256=DIGEST,
        ),
    )
    await store_upload(db, context, stale.upload_id, CONTENT)
    fresh = await reserve_upload(
        db,
        context,
        ReserveArtifactUploadRequest(
            publication_id=uuid4(), byte_size=1, mime_type="text/plain", sha256="0" * 64
        ),
    )
    # Expire the abandoned reservation AND the already-finalized one: only the
    # unfinalized reservation may be swept.
    await db.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id.in_([stale.upload_id, finalized_upload_id]))
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()
    assert len(storage.objects) == 2
    assert await sweep_artifact_uploads(db) == 1
    stale_row = await db.get(ArtifactUpload, stale.upload_id, populate_existing=True)
    fresh_row = await db.get(ArtifactUpload, fresh.upload_id, populate_existing=True)
    assert stale_row is not None and stale_row.is_deleted is True
    assert fresh_row is not None and fresh_row.is_deleted is False
    # The committed version's bytes survive; only the abandoned blob is gone.
    assert f"artifacts/{ORG}/uploads/{stale.upload_id}" not in storage.objects
    assert len(storage.objects) == 1  # the committed version keeps its bytes
    assert published.version_id
    assert await sweep_artifact_uploads(db) == 0


async def test_sweep_releases_quota(db: AsyncSession) -> None:
    context = _context(None)
    big = uuid4()
    await db.execute(
        insert(ArtifactUpload).values(
            id=big,
            organization_id=ORG,
            project_id=PROJECT,
            grant_id=uuid4(),
            publication_id=uuid4(),
            byte_size=PROJECT_QUOTA_BYTES,
            mime_type="text/plain",
            sha256="0" * 64,
            request_hash="0" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db.commit()
    small = ReserveArtifactUploadRequest(
        publication_id=uuid4(), byte_size=1, mime_type="text/plain", sha256="1" * 64
    )
    with pytest.raises(service.ArtifactQuotaExceeded):
        await reserve_upload(db, context, small)
    await db.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == big)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()
    assert await sweep_artifact_uploads(db) == 1
    assert (await reserve_upload(db, context, small)).upload_id


async def test_finalize_loses_to_a_concurrent_sweep(
    db: AsyncSession, storage: MemoryArtifactStorage
) -> None:
    context = _context(None)
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(CONTENT),
        mime_type="text/markdown",
        sha256=DIGEST,
    )
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    real_exists, real_delete = storage.exists, storage.delete

    async def keep_bytes(key: str) -> None:
        return None  # bytes survive, so only the row claim can stop the finalize

    async def sweep_meanwhile(key: str) -> bool:
        # The reservation expires and a sweeper claims it between our read and our write.
        storage.delete = keep_bytes  # type: ignore[method-assign]
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:  # type: ignore[arg-type]
            await other.execute(
                update(ArtifactUpload)
                .where(ArtifactUpload.id == upload.upload_id)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await other.commit()
            assert await sweep_artifact_uploads(other) == 1
        storage.delete = real_delete  # type: ignore[method-assign]
        return await real_exists(key)

    storage.exists = sweep_meanwhile  # type: ignore[method-assign]
    with pytest.raises(service.ArtifactConflict):
        await publish_version(
            db,
            context,
            PublishVersionRequest(
                publication_id=reserve.publication_id,
                upload_id=upload.upload_id,
                title="report.md",
                provenance=ArtifactProvenance(producer="harness"),
            ),
        )
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(ArtifactLifecycleOutbox)) == 0
    )


async def test_run_bound_to_another_org_is_refused(db: AsyncSession) -> None:
    foreign_run = str(uuid4())
    await db.execute(
        insert(AgentRun).values(
            job_id=foreign_run, organization_id=uuid4(), status=JobStatus.RUNNING.value
        )
    )
    await db.commit()
    with pytest.raises(service.ArtifactAccessDenied):
        await _publish(db, _context(foreign_run))
    assert (
        await db.scalar(select(func.count()).select_from(ArtifactLifecycleOutbox)) == 0
    )


async def test_drain_claims_rows_so_a_second_worker_cannot_redeliver(
    db: AsyncSession,
) -> None:
    version = await _publish(db, _context(RUN_OPEN))
    # Worker A claimed the row moments ago and is still delivering it.
    await db.execute(
        update(ArtifactLifecycleOutbox)
        .where(ArtifactLifecycleOutbox.version_id == version.version_id)
        .values(status="processing", updated_at=datetime.now(timezone.utc))
    )
    await db.commit()
    assert await drain_artifact_outbox(db) == 0
    assert await _events(db, RUN_OPEN) == []
    # Worker A died: a stale claim becomes eligible again, exactly once.
    await db.execute(
        update(ArtifactLifecycleOutbox)
        .where(ArtifactLifecycleOutbox.version_id == version.version_id)
        .values(
            updated_at=datetime.now(timezone.utc)
            - lifecycle.STALE_CLAIM
            - timedelta(seconds=1)
        )
    )
    await db.commit()
    assert await drain_artifact_outbox(db) == 1
    assert await drain_artifact_outbox(db) == 0
    assert len(await _events(db, RUN_OPEN)) == 1
    row = await db.scalar(select(ArtifactLifecycleOutbox))
    assert row is not None and row.status == "delivered" and row.attempts == 1


async def test_transient_delivery_failure_returns_the_row_to_pending(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _publish(db, _context(RUN_OPEN))
    calls = {"n": 0}
    real_append = lifecycle.append_event

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ledger hiccup")
        return await real_append(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "append_event", flaky)
    assert await drain_artifact_outbox(db) == 0
    row = await db.scalar(select(ArtifactLifecycleOutbox))
    assert (
        row is not None
        and row.status == "pending"
        and row.last_error == "ledger hiccup"
    )
    assert await drain_artifact_outbox(db) == 1
    row = await db.scalar(select(ArtifactLifecycleOutbox))
    assert row is not None and row.status == "delivered" and row.last_error is None


async def test_store_loses_to_a_sweep_that_expired_it_mid_flight(
    db: AsyncSession, storage: MemoryArtifactStorage
) -> None:
    context = _context(None)
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(CONTENT),
        mime_type="text/markdown",
        sha256=DIGEST,
    )
    upload = await reserve_upload(db, context, reserve)
    real_put = storage.put

    async def sweep_during_write(key: str, content: bytes, mime_type: str) -> None:
        await real_put(key, content, mime_type)
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:  # type: ignore[arg-type]
            await other.execute(
                update(ArtifactUpload)
                .where(ArtifactUpload.id == upload.upload_id)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await other.commit()
            assert await sweep_artifact_uploads(other) == 1

    storage.put = sweep_during_write  # type: ignore[method-assign]
    with pytest.raises(service.ArtifactConflict):
        await store_upload(db, context, upload.upload_id, CONTENT)
    assert storage.objects == {}  # the losing write cleaned up its blob
    row = await db.get(ArtifactUpload, upload.upload_id, populate_existing=True)
    assert row is not None and row.is_deleted is True and row.storage_key is None


async def test_failed_object_delete_keeps_the_key_for_retry(
    db: AsyncSession, storage: MemoryArtifactStorage
) -> None:
    from src.services.artifacts.storage import S3ArtifactStorage

    class FalseHelper:
        def delete_file(self, key: str) -> bool:
            return False  # the S3 helper swallows errors into False

    s3 = S3ArtifactStorage.__new__(S3ArtifactStorage)
    s3.helper = FalseHelper()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await s3.delete("artifacts/x")
    # And the sweeper leaves storage_key in place when delete raises.
    context = _context(None)
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(CONTENT),
        mime_type="text/plain",
        sha256=DIGEST,
    )
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    await db.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload.upload_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()

    async def boom(key: str) -> None:
        raise RuntimeError("object delete failed")

    storage.delete = boom  # type: ignore[method-assign]
    assert await sweep_artifact_uploads(db) == 1
    row = await db.get(ArtifactUpload, upload.upload_id, populate_existing=True)
    assert row is not None and row.is_deleted is True and row.storage_key is not None


def test_sweeper_task_honors_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import settings
    from src.tasks import artifact_tasks

    monkeypatch.setattr(settings, "SWEEPERS_ENABLED", False)
    monkeypatch.setattr(artifact_tasks, "run_async", lambda coro: (coro.close(), 99)[1])
    assert artifact_tasks.sweep_artifact_uploads() == 0
    monkeypatch.setattr(settings, "SWEEPERS_ENABLED", True)
    assert artifact_tasks.sweep_artifact_uploads() == 99
