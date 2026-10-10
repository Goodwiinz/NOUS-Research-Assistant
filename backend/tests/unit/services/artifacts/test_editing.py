"""Browser editing preserves immutable versions and durable retry receipts."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.artifact import Artifact, ArtifactUpload, ArtifactVersion
from src.models.collection import Collection
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import (
    ArtifactAccessDenied,
    ArtifactConflict,
    ArtifactDigestMismatch,
    ArtifactNotFound,
    ArtifactStorageUnavailable,
    ArtifactTooLarge,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts.editing import MAX_EDIT_BYTES, edit_version
from src.services.artifacts.service import read_version_content
from src.services.artifacts.storage import MemoryArtifactStorage
from tests.unit.services.artifacts.test_publication import (
    EDITOR_USER,
    ORG,
    OTHER_ORG,
    OTHER_USER,
    PROJECT,
    USER,
    VIEWER_USER,
    WORKSPACE,
)
from tests.unit.services.artifacts.test_publication import (
    _use_memory_storage as _use_memory_storage,
)
from tests.unit.services.artifacts.test_publication import context as context
from tests.unit.services.artifacts.test_publication import db as db
from tests.unit.services.artifacts.test_publication import storage as storage
from tests.utils.artifact_publication import publish_content


async def test_edit_retry_and_old_bytes(
    db: AsyncSession, context: IntegrationContext
) -> None:
    _, original = await publish_content(db, context)
    args: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        artifact_id=original.artifact_id,
        expected_parent_version_id=original.version_id,
        publication_id=uuid4(),
        text="changed\n",
    )
    edited = await edit_version(db, **args)
    retried = await edit_version(db, **args)
    assert retried.version_id == edited.version_id
    assert edited.parent_version_id == original.version_id
    assert edited.provenance.producer == "user"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 2
    content, _, _ = await read_version_content(
        db, user_id=USER, organization_id=ORG, version_id=original.version_id
    )
    assert content == b"report\n"
    with pytest.raises(ArtifactConflict):
        await edit_version(db, **{**args, "text": "different\n"})


async def test_stale_parent_preserves_current(
    db: AsyncSession, context: IntegrationContext
) -> None:
    _, original = await publish_content(db, context)
    args: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        artifact_id=original.artifact_id,
        expected_parent_version_id=original.version_id,
        publication_id=uuid4(),
        text="new version\n",
    )
    edited = await edit_version(db, **args)
    uploads = await db.scalar(select(func.count()).select_from(ArtifactUpload))
    with pytest.raises(ArtifactConflict) as conflict:
        await edit_version(db, **{**args, "publication_id": uuid4(), "text": "stale"})
    assert conflict.value.current_version_id == edited.version_id
    artifact = await db.get(Artifact, original.artifact_id, populate_existing=True)
    assert artifact is not None
    assert artifact.current_version_id == edited.version_id
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 2
    # A fresh stale edit is rejected before it reserves quota or stores bytes.
    assert await db.scalar(select(func.count()).select_from(ArtifactUpload)) == uploads


@pytest.mark.parametrize(
    ("user_id", "org_id", "allowed"),
    [
        (EDITOR_USER, ORG, True),
        (VIEWER_USER, ORG, False),
        (OTHER_USER, OTHER_ORG, False),
    ],
)
async def test_edit_requires_live_project_editor(
    db: AsyncSession,
    context: IntegrationContext,
    user_id: UUID,
    org_id: UUID,
    allowed: bool,
) -> None:
    _, original = await publish_content(db, context)
    args: dict[str, Any] = dict(
        user_id=user_id,
        organization_id=org_id,
        artifact_id=original.artifact_id,
        expected_parent_version_id=original.version_id,
        publication_id=uuid4(),
        text="editor change",
    )
    if allowed:
        assert (await edit_version(db, **args)).parent_version_id == original.version_id
    else:
        with pytest.raises((ArtifactAccessDenied, ArtifactNotFound)):
            await edit_version(db, **args)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("é" * (MAX_EDIT_BYTES // 2 + 1), ArtifactTooLarge),
        ("\ud800", ArtifactDigestMismatch),
    ],
    ids=["utf8-byte-limit", "invalid-utf8"],
)
async def test_invalid_edit(
    db: AsyncSession,
    context: IntegrationContext,
    text: str,
    error: type[Exception],
) -> None:
    _, original = await publish_content(db, context)
    with pytest.raises(error):
        await edit_version(
            db,
            user_id=USER,
            organization_id=ORG,
            artifact_id=original.artifact_id,
            expected_parent_version_id=original.version_id,
            publication_id=uuid4(),
            text=text,
        )
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


async def test_missing_blob_keeps_pointer(
    db: AsyncSession,
    context: IntegrationContext,
    storage: MemoryArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, original = await publish_content(db, context)

    async def missing(key: str) -> bool:
        return False

    monkeypatch.setattr(storage, "exists", missing)
    with pytest.raises(ArtifactStorageUnavailable):
        await edit_version(
            db,
            user_id=USER,
            organization_id=ORG,
            artifact_id=original.artifact_id,
            expected_parent_version_id=original.version_id,
            publication_id=uuid4(),
            text="new bytes",
        )
    current = await db.get(Artifact, original.artifact_id, populate_existing=True)
    assert current is not None and current.current_version_id == original.version_id


async def test_empty_text_is_a_valid_version(
    db: AsyncSession, context: IntegrationContext
) -> None:
    _, original = await publish_content(db, context)
    empty = await edit_version(
        db,
        user_id=USER,
        organization_id=ORG,
        artifact_id=original.artifact_id,
        expected_parent_version_id=original.version_id,
        publication_id=uuid4(),
        text="",
    )
    assert empty.byte_size == 0
    content, _, _ = await read_version_content(
        db, user_id=USER, organization_id=ORG, version_id=empty.version_id
    )
    assert content == b""


@pytest.mark.parametrize(
    "mime_type", ["image/png", "application/pdf", "application/octet-stream"]
)
async def test_binary_edit_is_rejected(
    db: AsyncSession, context: IntegrationContext, mime_type: str
) -> None:
    _, original = await publish_content(db, context, mime_type=mime_type)
    with pytest.raises(ArtifactDigestMismatch):
        await edit_version(
            db,
            user_id=USER,
            organization_id=ORG,
            artifact_id=original.artifact_id,
            expected_parent_version_id=original.version_id,
            publication_id=uuid4(),
            text="replacement",
        )
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


@pytest.mark.parametrize("revocation", ["project", "workspace", "membership"])
async def test_edit_rechecks_live_ancestors_and_membership(
    db: AsyncSession, context: IntegrationContext, revocation: str
) -> None:
    _, original = await publish_content(db, context)
    if revocation == "project":
        await db.execute(
            update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
        )
    elif revocation == "workspace":
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
    else:
        await db.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.user_id == EDITOR_USER)
            .values(is_deleted=True)
        )
    await db.commit()
    with pytest.raises((ArtifactAccessDenied, ArtifactNotFound)):
        await edit_version(
            db,
            user_id=EDITOR_USER,
            organization_id=ORG,
            artifact_id=original.artifact_id,
            expected_parent_version_id=original.version_id,
            publication_id=uuid4(),
            text="replacement",
        )
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 1


async def test_slow_identical_retry_preserves_published_bytes(
    db: AsyncSession,
    context: IntegrationContext,
    storage: MemoryArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, original = await publish_content(db, context)
    first_put_started = asyncio.Event()
    winner_finished = asyncio.Event()
    real_put = storage.put
    calls = 0

    async def paused_put(key: str, content: bytes, mime_type: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_put_started.set()
            await asyncio.wait_for(winner_finished.wait(), 5)
        await real_put(key, content, mime_type)

    monkeypatch.setattr(storage, "put", paused_put)
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    publication_id = uuid4()

    async def edit() -> Any:
        async with sessions() as session:
            return await edit_version(
                session,
                user_id=USER,
                organization_id=ORG,
                artifact_id=original.artifact_id,
                expected_parent_version_id=original.version_id,
                publication_id=publication_id,
                text="identical edit",
            )

    slow = asyncio.create_task(edit())
    await asyncio.wait_for(first_put_started.wait(), 5)
    try:
        winner = await asyncio.wait_for(edit(), 5)
    finally:
        winner_finished.set()
    replay = await asyncio.wait_for(slow, 5)
    assert replay.version_id == winner.version_id
    content, _, _ = await read_version_content(
        db, user_id=USER, organization_id=ORG, version_id=winner.version_id
    )
    assert content == b"identical edit"
    assert await db.scalar(select(func.count()).select_from(ArtifactVersion)) == 2
