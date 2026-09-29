"""Scoped artifact publication, using a local SQLite database and memory storage."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.artifact import (
    Artifact,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactConflict,
    ArtifactDigestMismatch,
    ArtifactNotFound,
    ArtifactProvenance,
    ArtifactQuotaExceeded,
    ArtifactStorageUnavailable,
    ArtifactTooLarge,
    ArtifactVersionDTO,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import service
from src.services.artifacts.service import (
    MAX_ARTIFACT_BYTES,
    PROJECT_QUOTA_BYTES,
    authorize_artifact,
    list_thread_artifacts,
    publish_version,
    read_version_content,
    reserve_upload,
    store_upload,
)
from src.services.artifacts.storage import MemoryArtifactStorage

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, OTHER_ORG, PROJECT, OTHER_PROJECT, WORKSPACE, OTHER_WORKSPACE = (
    uuid4() for _ in range(8)
)
EDITOR_USER, VIEWER_USER, CONVERSATION, THREAD = (uuid4() for _ in range(4))
CONTENT = b"report\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def _user_sql(user_id: UUID, org: UUID, email: str) -> tuple[str, dict[str, str]]:
    return (
        "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)",
        {"id": str(user_id), "org": str(org), "email": email},
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    # File-backed so a second session can commit a competing row.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifacts.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        Artifact,
        ArtifactVersion,
        ArtifactUpload,
        ArtifactReference,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(
                [
                    dict(id=ORG, name="Test", storage_limit_bytes=1000000),
                    dict(id=OTHER_ORG, name="Other", storage_limit_bytes=1000000),
                ]
            )
        )
        for user_id, org, email in (
            (USER, ORG, "owner@example.test"),
            (OTHER_USER, OTHER_ORG, "foreign@example.test"),
            (EDITOR_USER, ORG, "editor@example.test"),
            (VIEWER_USER, ORG, "viewer@example.test"),
        ):
            await session.execute(
                text(_user_sql(user_id, org, email)[0]),
                _user_sql(user_id, org, email)[1],
            )
        await session.execute(
            insert(Workspace).values(
                [
                    dict(
                        id=WORKSPACE,
                        name="Workspace",
                        owner_id=USER,
                        organization_id=ORG,
                    ),
                    dict(
                        id=OTHER_WORKSPACE,
                        name="Foreign",
                        owner_id=OTHER_USER,
                        organization_id=OTHER_ORG,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=OTHER_WORKSPACE),
                ]
            )
        )
        await session.execute(
            insert(WorkspaceMember).values(
                [
                    dict(
                        workspace_id=WORKSPACE,
                        user_id=EDITOR_USER,
                        role=WorkspaceRole.EDITOR,
                    ),
                    dict(
                        workspace_id=WORKSPACE,
                        user_id=VIEWER_USER,
                        role=WorkspaceRole.VIEWER,
                    ),
                ]
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


@pytest.fixture
def context() -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        thread_id=THREAD,
        grant_id=uuid4(),
    )


def _reserve(
    publication_id: UUID | None = None, **overrides: Any
) -> ReserveArtifactUploadRequest:
    return ReserveArtifactUploadRequest(
        publication_id=publication_id or uuid4(),
        byte_size=overrides.pop("byte_size", len(CONTENT)),
        mime_type=overrides.pop("mime_type", "text/markdown"),
        sha256=overrides.pop("sha256", DIGEST),
    )


def _publish(
    reserve: ReserveArtifactUploadRequest, upload_id: UUID, **overrides: Any
) -> PublishVersionRequest:
    return PublishVersionRequest(
        publication_id=reserve.publication_id,
        upload_id=upload_id,
        title=overrides.pop("title", "report.md"),
        provenance=overrides.pop("provenance", ArtifactProvenance(producer="harness")),
        **overrides,
    )


async def _published(
    db: AsyncSession, context: IntegrationContext, **overrides: Any
) -> tuple[PublishVersionRequest, ArtifactVersionDTO]:
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    request = _publish(reserve, upload.upload_id, **overrides)
    return request, await publish_version(db, context, request)


async def test_finalize_is_idempotent_after_terminal(
    db: AsyncSession, context: IntegrationContext
) -> None:
    request, first = await _published(db, context)
    second = await publish_version(db, context, request)
    assert first.version_id == second.version_id
    assert first.sha256 == DIGEST
    listed = await list_thread_artifacts(
        db, user_id=USER, organization_id=ORG, thread_id=THREAD
    )
    assert [item.version.version_id for item in listed] == [first.version_id]
    assert listed[0].reference.thread_id == THREAD
    assert (await db.scalar(select(ArtifactVersion.id))) == first.version_id
    assert not hasattr(first, "storage_key")


async def test_changed_replay_conflicts(
    db: AsyncSession, context: IntegrationContext
) -> None:
    request, first = await _published(db, context)
    with pytest.raises(ArtifactConflict):
        await publish_version(
            db, context, request.model_copy(update={"title": "other.md"})
        )
    reserve = _reserve(request.publication_id)
    with pytest.raises(ArtifactConflict):
        await reserve_upload(db, context, reserve.model_copy(update={"byte_size": 99}))
    same = await reserve_upload(db, context, reserve)
    assert same.upload_id == request.upload_id


async def test_digest_and_size_mismatch_never_store(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    upload = await reserve_upload(db, context, _reserve())
    with pytest.raises(ArtifactDigestMismatch):
        await store_upload(db, context, upload.upload_id, b"tampered\n")
    with pytest.raises(ArtifactDigestMismatch):
        await store_upload(db, context, upload.upload_id, CONTENT + b"x")
    assert storage.objects == {}
    with pytest.raises(ArtifactNotFound):
        await publish_version(db, context, _publish(_reserve(), upload.upload_id))


@pytest.mark.parametrize(
    "case",
    [
        "oversize",
        "quota",
        "foreign_project",
        "deleted_project",
        "deleted_workspace",
        "foreign_actor",
    ],
)
async def test_publication_denials(
    db: AsyncSession,
    context: IntegrationContext,
    storage: MemoryArtifactStorage,
    case: str,
) -> None:
    request = _reserve()
    expected: type[Exception] = ArtifactAccessDenied
    if case == "oversize":
        request = _reserve(byte_size=MAX_ARTIFACT_BYTES + 1)
        expected = ArtifactTooLarge
    elif case == "quota":
        await db.execute(
            insert(ArtifactUpload).values(
                id=uuid4(),
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
        expected = ArtifactQuotaExceeded
    elif case == "foreign_project":
        context = context.model_copy(update={"project_id": OTHER_PROJECT})
    elif case == "deleted_project":
        await db.execute(
            update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
        )
        await db.commit()
    elif case == "deleted_workspace":
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
        await db.commit()
    elif case == "foreign_actor":
        context = context.model_copy(update={"user_id": OTHER_USER})
    with pytest.raises(expected):
        await reserve_upload(db, context, request)
    assert (
        await db.scalar(
            select(ArtifactUpload.id).where(ArtifactUpload.grant_id == context.grant_id)
        )
    ) is None


async def test_expired_reservation_cannot_be_stored(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    upload = await reserve_upload(db, context, _reserve())
    await db.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload.upload_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db.commit()
    with pytest.raises(ArtifactConflict):
        await store_upload(db, context, upload.upload_id, CONTENT)
    assert storage.objects == {}


async def test_missing_blob_keeps_current_version(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    request, first = await _published(db, context)
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    storage.objects.clear()
    second = _publish(
        reserve,
        upload.upload_id,
        artifact_id=first.artifact_id,
        expected_parent_version_id=first.version_id,
    )
    with pytest.raises(ArtifactStorageUnavailable):
        await publish_version(db, context, second)
    artifact = await db.get(Artifact, first.artifact_id)
    assert artifact is not None and artifact.current_version_id == first.version_id
    with pytest.raises(ArtifactStorageUnavailable):
        await read_version_content(
            db, user_id=USER, organization_id=ORG, version_id=first.version_id
        )


async def test_new_version_requires_current_parent(
    db: AsyncSession, context: IntegrationContext
) -> None:
    _, first = await _published(db, context)
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    stale = _publish(
        reserve,
        upload.upload_id,
        artifact_id=first.artifact_id,
        expected_parent_version_id=uuid4(),
    )
    with pytest.raises(ArtifactConflict):
        await publish_version(db, context, stale)
    fresh = _publish(
        reserve,
        upload.upload_id,
        artifact_id=first.artifact_id,
        expected_parent_version_id=first.version_id,
    )
    second = await publish_version(db, context, fresh)
    assert second.parent_version_id == first.version_id
    artifact = await db.get(Artifact, first.artifact_id)
    assert artifact is not None and artifact.current_version_id == second.version_id
    # A finalized upload replayed with a different payload is a conflict, not a retry.
    with pytest.raises(ArtifactConflict):
        await publish_version(
            db, context, _publish(reserve, upload.upload_id, artifact_id=uuid4())
        )
    another = _reserve()
    third = await reserve_upload(db, context, another)
    await store_upload(db, context, third.upload_id, CONTENT)
    with pytest.raises(ArtifactNotFound):
        await publish_version(
            db, context, _publish(another, third.upload_id, artifact_id=uuid4())
        )


async def test_reads_recheck_org_project_and_ancestors(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    _, version = await _published(db, context)
    content, mime, filename = await read_version_content(
        db, user_id=USER, organization_id=ORG, version_id=version.version_id
    )
    assert (content, mime, filename) == (CONTENT, "text/markdown", "report.md")
    await authorize_artifact(
        db,
        user_id=USER,
        organization_id=ORG,
        artifact_id=version.artifact_id,
        action="read",
    )
    with pytest.raises(ArtifactNotFound):
        await authorize_artifact(
            db,
            user_id=OTHER_USER,
            organization_id=OTHER_ORG,
            artifact_id=version.artifact_id,
            action="read",
        )
    with pytest.raises(ArtifactNotFound):
        await read_version_content(
            db,
            user_id=OTHER_USER,
            organization_id=OTHER_ORG,
            version_id=version.version_id,
        )
    assert (
        await list_thread_artifacts(
            db, user_id=OTHER_USER, organization_id=OTHER_ORG, thread_id=THREAD
        )
        == []
    )
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(ArtifactNotFound):
        await authorize_artifact(
            db,
            user_id=USER,
            organization_id=ORG,
            artifact_id=version.artifact_id,
            action="read",
        )
    assert (
        await list_thread_artifacts(
            db, user_id=USER, organization_id=ORG, thread_id=THREAD
        )
        == []
    )


async def test_unknown_mime_is_stored_as_octet_stream_and_dto_hides_keys(
    db: AsyncSession, context: IntegrationContext
) -> None:
    reserve = _reserve(mime_type="application/x-msdownload")
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    version = await publish_version(
        db, context, _publish(reserve, upload.upload_id, title="tool.exe")
    )
    assert version.mime_type == "application/octet-stream"
    assert "storage_key" not in version.model_dump()
    assert version.provenance.producer == "harness"


async def test_concurrent_finalize_returns_the_winner(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    request = _publish(reserve, upload.upload_id)
    winner_id = uuid4()
    real_exists = storage.exists

    async def commit_competitor(key: str) -> bool:
        # Another worker finalizes the same upload while this one checks the blob.
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:  # type: ignore[arg-type]
            artifact_id = uuid4()
            other.add(
                Artifact(
                    id=artifact_id,
                    organization_id=ORG,
                    project_id=PROJECT,
                    owner_id=USER,
                    title="report.md",
                    current_version_id=winner_id,
                )
            )
            other.add(
                ArtifactVersion(
                    id=winner_id,
                    artifact_id=artifact_id,
                    upload_id=upload.upload_id,
                    title="report.md",
                    mime_type="text/markdown",
                    byte_size=len(CONTENT),
                    sha256=DIGEST,
                    storage_key=f"artifacts/{ORG}/uploads/{upload.upload_id}",
                    producer="harness",
                    provenance={"producer": "harness", "source_ids": []},
                    grant_id=context.grant_id,
                )
            )
            await other.execute(
                update(ArtifactUpload)
                .where(ArtifactUpload.id == upload.upload_id)
                .values(
                    version_id=winner_id,
                    publish_hash=service._hash(request.model_dump(mode="json")),
                )
            )
            await other.commit()
        return await real_exists(key)

    storage.exists = commit_competitor  # type: ignore[method-assign]
    result = await publish_version(db, context, request)
    assert result.version_id == winner_id
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1
    storage.exists = real_exists  # type: ignore[method-assign]
    changed = request.model_copy(update={"title": "other.md"})
    with pytest.raises(ArtifactConflict):
        await publish_version(db, context, changed)


@pytest.mark.parametrize(
    ("user_id", "allowed"), [(EDITOR_USER, True), (VIEWER_USER, False)]
)
async def test_publication_requires_edit_role_but_reads_need_membership(
    db: AsyncSession, context: IntegrationContext, user_id: UUID, allowed: bool
) -> None:
    _, version = await _published(db, context)
    member = context.model_copy(update={"user_id": user_id, "grant_id": uuid4()})
    if allowed:
        await reserve_upload(db, member, _reserve())
    else:
        with pytest.raises(ArtifactAccessDenied):
            await reserve_upload(db, member, _reserve())
    # Any live member may still read.
    await authorize_artifact(
        db,
        user_id=user_id,
        organization_id=ORG,
        artifact_id=version.artifact_id,
        action="read",
    )


async def test_parent_pointer_swap_is_atomic(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    _, first = await _published(db, context)
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    request = _publish(
        reserve,
        upload.upload_id,
        artifact_id=first.artifact_id,
        expected_parent_version_id=first.version_id,
    )
    real_exists = storage.exists
    moved_to = uuid4()

    async def move_pointer(key: str) -> bool:
        # A sibling finalize with the same parent commits between our read and our write.
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:  # type: ignore[arg-type]
            await other.execute(
                update(Artifact)
                .where(Artifact.id == first.artifact_id)
                .values(current_version_id=moved_to)
            )
            await other.commit()
        return await real_exists(key)

    storage.exists = move_pointer  # type: ignore[method-assign]
    with pytest.raises(ArtifactConflict):
        await publish_version(db, context, request)
    storage.exists = real_exists  # type: ignore[method-assign]
    artifact = await db.get(Artifact, first.artifact_id, populate_existing=True)
    assert artifact is not None and artifact.current_version_id == moved_to
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


async def test_deleted_thread_or_conversation_hides_references(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await _published(db, context)
    assert (
        len(
            await list_thread_artifacts(
                db, user_id=USER, organization_id=ORG, thread_id=THREAD
            )
        )
        == 1
    )
    await db.execute(
        update(Conversation)
        .where(Conversation.id == CONVERSATION)
        .values(is_deleted=True)
    )
    await db.commit()
    assert (
        await list_thread_artifacts(
            db, user_id=USER, organization_id=ORG, thread_id=THREAD
        )
        == []
    )


async def test_identical_concurrent_reservations_return_one_upload(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve = _reserve()
    real_now = service._now
    competitor: dict[str, UUID] = {}

    def insert_competitor() -> datetime:
        # Runs after the lookup and before our insert, like a racing identical retry.
        if not competitor:
            competitor["id"] = uuid4()
            import sqlite3

            path = str(db.bind.url.database)  # type: ignore[union-attr]
            with sqlite3.connect(path) as raw:
                raw.execute(
                    "INSERT INTO artifact_uploads (id, organization_id, project_id, grant_id, publication_id, byte_size, mime_type, sha256, request_hash, expires_at, created_at, updated_at, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)",
                    (
                        str(competitor["id"]),
                        str(ORG),
                        str(PROJECT),
                        str(context.grant_id),
                        str(reserve.publication_id),
                        reserve.byte_size,
                        reserve.mime_type,
                        reserve.sha256,
                        service._hash(reserve.model_dump(mode="json")),
                        (real_now() + timedelta(minutes=15)).isoformat(sep=" "),
                    ),
                )
        return real_now()

    monkeypatch.setattr(service, "_now", insert_competitor)
    result = await reserve_upload(db, context, reserve)
    assert result.upload_id == competitor["id"]
    assert await db.scalar(select(func.count()).select_from(ArtifactUpload)) == 1


async def test_storage_probe_failure_is_unavailable_not_500(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)

    async def boom(key: str) -> bool:
        raise RuntimeError("s3 auth expired")

    storage.exists = boom  # type: ignore[method-assign]
    with pytest.raises(ArtifactStorageUnavailable):
        await publish_version(db, context, _publish(reserve, upload.upload_id))
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 0


async def test_soft_deleted_reservation_cannot_be_used(
    db: AsyncSession, context: IntegrationContext
) -> None:
    reserve = _reserve()
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, CONTENT)
    await db.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload.upload_id)
        .values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(ArtifactNotFound):
        await publish_version(db, context, _publish(reserve, upload.upload_id))
