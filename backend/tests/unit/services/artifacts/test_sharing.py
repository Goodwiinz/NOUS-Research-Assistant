"""Shares expose only pinned bytes to live, explicit same-organization users.

Mutation proof: removing sharing.py:168 (revoked_at) / :169 (expires_at) from
sharing._authorized_share makes test_revoked_share_denies_metadata_and_bytes /
test_expired_share_denies_metadata_and_bytes fail respectively. Focused command:
/tmp/nous-qa-fix-venv/bin/python -m pytest -q backend/tests/unit/services/artifacts/test_sharing.py -k 'revoked_share or expired_share'
Observed both fail with DID NOT RAISE ArtifactNotFound; source restored exactly.
Storage revalidation (sharing.py:216) was separately observed red/green using
test_revocation_while_storage_loads_prevents_byte_release.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.core.config import settings
from src.models.artifact import Artifact, ArtifactVersion
from src.models.artifact_share import ArtifactShare, ArtifactShareAudience
from src.models.collection import Collection
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.artifact import (
    ArtifactInvalidShareRequest,
    ArtifactNotFound,
    ArtifactShareDTO,
    ArtifactSharingDisabled,
    ArtifactStorageUnavailable,
    ArtifactVersionDTO,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import sharing
from src.services.artifacts.editing import edit_version
from src.services.artifacts.service import list_versions, read_version_content
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
from tests.unit.services.artifacts.test_publication import _user_sql
from tests.unit.services.artifacts.test_publication import context as context
from tests.unit.services.artifacts.test_publication import db as publication_db
from tests.unit.services.artifacts.test_publication import storage as storage
from tests.utils.artifact_publication import publish_content

pytestmark = pytest.mark.unit
RECIPIENT = uuid4()


@pytest.fixture
async def db(publication_db: AsyncSession) -> AsyncIterator[AsyncSession]:
    # The original publication fixture keeps persistence and storage real.
    assert publication_db.bind is not None
    engine = cast(AsyncEngine, publication_db.bind)
    async with engine.begin() as connection:
        for model in (ArtifactShare, ArtifactShareAudience):
            await connection.run_sync(model.__table__.create, checkfirst=True)
    sql, values = _user_sql(RECIPIENT, ORG, "recipient@example.test")
    await publication_db.execute(text(sql), values)
    await publication_db.commit()
    yield publication_db


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)
    monkeypatch.setattr(settings, "ARTIFACT_SHARING_ENABLED", True)


async def _share(
    db: AsyncSession, context: IntegrationContext
) -> tuple[ArtifactShareDTO, ArtifactVersionDTO]:
    _, version = await publish_content(db, context)
    share = await sharing.create_share(
        db,
        user_id=context.user_id,
        organization_id=ORG,
        version_id=version.version_id,
        audience_user_ids=[RECIPIENT],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    return share, version


async def _deny_both(
    db: AsyncSession, share_id: UUID, user_id: UUID = RECIPIENT
) -> None:
    for read in (sharing.read_share, sharing.read_share_content):
        with pytest.raises(ArtifactNotFound):
            await read(db, user_id=user_id, share_id=share_id)


async def test_share_stays_pinned_without_granting_project_access(
    db: AsyncSession, context: IntegrationContext
) -> None:
    share, version = await _share(db, context)
    edited = await edit_version(
        db,
        user_id=USER,
        organization_id=ORG,
        artifact_id=version.artifact_id,
        expected_parent_version_id=version.version_id,
        publication_id=uuid4(),
        text="new bytes",
    )
    shared = await sharing.read_share(db, user_id=RECIPIENT, share_id=share.id)
    assert shared.model_dump() == {
        "artifact_id": version.artifact_id,
        "version_id": version.version_id,
        "title": "report.md",
        "mime_type": "text/markdown",
        "byte_size": 7,
        "sha256": "331d26d6d8f862e46ba900811be8a7a1e4dbaa229b14c99becfd5e5151490d95",
    }
    assert edited.version_id != shared.version_id
    assert await sharing.read_share_content(
        db, user_id=RECIPIENT, share_id=share.id
    ) == (
        b"report\n",
        "text/markdown",
        "report.md",
    )
    with pytest.raises(ArtifactNotFound):
        await list_versions(
            db, user_id=RECIPIENT, organization_id=ORG, artifact_id=version.artifact_id
        )
    with pytest.raises(ArtifactNotFound):
        await read_version_content(
            db, user_id=RECIPIENT, organization_id=ORG, version_id=version.version_id
        )
    await _deny_both(db, share.id, VIEWER_USER)
    await _deny_both(db, share.id, OTHER_USER)
    # The creator is not silently added to an explicit audience.
    await _deny_both(db, share.id, USER)


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "too_many",
        "duplicate",
        "unknown",
        "foreign",
        "inactive",
        "deleted",
        "naive",
        "expired",
        "too_long",
    ],
)
async def test_create_rejects_invalid_audience_and_expiry(
    db: AsyncSession, context: IntegrationContext, case: str
) -> None:
    _, version = await publish_content(db, context)
    recipients = [RECIPIENT]
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    if case == "empty":
        recipients = []
    elif case == "too_many":
        recipients = [uuid4() for _ in range(101)]
    elif case == "duplicate":
        recipients = [RECIPIENT, RECIPIENT]
    elif case == "unknown":
        recipients = [uuid4()]
    elif case == "foreign":
        recipients = [OTHER_USER]
    elif case in ("inactive", "deleted"):
        await db.execute(
            update(User)
            .where(User.id == RECIPIENT)
            .values(
                **{
                    "is_active" if case == "inactive" else "is_deleted": case
                    != "inactive"
                }
            )
        )
        await db.commit()
    elif case == "naive":
        expiry = datetime.now() + timedelta(days=1)
    elif case == "expired":
        expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        expiry = datetime.now(timezone.utc) + timedelta(days=7, seconds=1)

    with pytest.raises(ArtifactInvalidShareRequest):
        await sharing.create_share(
            db,
            user_id=USER,
            organization_id=ORG,
            version_id=version.version_id,
            audience_user_ids=recipients,
            expires_at=expiry,
        )
    assert await db.scalar(select(func.count()).select_from(ArtifactShare)) == 0


@pytest.mark.parametrize("actor", [EDITOR_USER, VIEWER_USER, OTHER_USER])
async def test_create_requires_artifact_owner(
    db: AsyncSession, context: IntegrationContext, actor: UUID
) -> None:
    _, version = await publish_content(db, context)
    with pytest.raises(ArtifactNotFound):
        await sharing.create_share(
            db,
            user_id=actor,
            organization_id=ORG,
            version_id=version.version_id,
            audience_user_ids=[RECIPIENT],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )


async def test_revoked_share_denies_metadata_and_bytes(
    db: AsyncSession, context: IntegrationContext
) -> None:
    share, _ = await _share(db, context)
    assert await sharing.read_share(db, user_id=RECIPIENT, share_id=share.id)
    await sharing.revoke_share(db, user_id=USER, share_id=share.id)
    await sharing.revoke_share(db, user_id=USER, share_id=share.id)
    await _deny_both(db, share.id)

    current = await db.get(ArtifactShare, share.id, populate_existing=True)
    assert current is not None and current.revoked_at is not None


async def test_expired_share_denies_metadata_and_bytes(
    db: AsyncSession, context: IntegrationContext
) -> None:
    share, _ = await _share(db, context)

    await db.execute(
        update(ArtifactShare)
        .where(ArtifactShare.id == share.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db.commit()
    await _deny_both(db, share.id)


@pytest.mark.parametrize(
    "loss",
    [
        "version",
        "artifact",
        "project",
        "workspace",
        "organization",
        "recipient_org",
        "recipient_inactive",
        "recipient_deleted",
        "creator_org",
        "creator_inactive",
        "creator_deleted",
        "creator_edit",
        "audience",
    ],
)
async def test_every_fetch_rechecks_live_authority(
    db: AsyncSession, context: IntegrationContext, loss: str
) -> None:
    share, version = await _share(db, context)
    assert await sharing.read_share_content(db, user_id=RECIPIENT, share_id=share.id)
    # Use a second session, preserving the reader identity map: checks must reload.
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
        losses: dict[str, tuple[Any, UUID, dict[str, Any]]] = {
            "version": (ArtifactVersion, version.version_id, {"is_deleted": True}),
            "artifact": (Artifact, version.artifact_id, {"is_deleted": True}),
            "project": (Collection, PROJECT, {"is_deleted": True}),
            "workspace": (Workspace, WORKSPACE, {"is_deleted": True}),
            "organization": (Organization, ORG, {"is_deleted": True}),
            "recipient_org": (User, RECIPIENT, {"organization_id": OTHER_ORG}),
            "recipient_inactive": (User, RECIPIENT, {"is_active": False}),
            "recipient_deleted": (User, RECIPIENT, {"is_deleted": True}),
            "creator_org": (User, USER, {"organization_id": OTHER_ORG}),
            "creator_inactive": (User, USER, {"is_active": False}),
            "creator_deleted": (User, USER, {"is_deleted": True}),
            "creator_edit": (
                Workspace,
                WORKSPACE,
                {"owner_id": EDITOR_USER, "is_public": True},
            ),
        }
        model, identity, changes = losses.get(loss, (User, RECIPIENT, {}))
        if loss == "audience":

            await writer.execute(
                update(ArtifactShareAudience)
                .where(ArtifactShareAudience.share_id == share.id)
                .values(is_deleted=True)
            )
        else:
            await writer.execute(
                update(model).where(model.id == identity).values(**changes)
            )
        await writer.commit()
    await _deny_both(db, share.id)


async def test_creator_requires_live_edit_role_even_after_ownership(
    db: AsyncSession, context: IntegrationContext
) -> None:
    context = context.model_copy(update={"user_id": EDITOR_USER})
    share, _ = await _share(db, context)
    await db.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.user_id == EDITOR_USER)
        .values(role=WorkspaceRole.VIEWER)
    )
    await db.commit()
    await _deny_both(db, share.id)


async def test_reads_and_owner_management_survive_disabled_write_flags(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    share, version = await _share(db, context)
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    monkeypatch.setattr(settings, "ARTIFACT_SHARING_ENABLED", False)
    assert await sharing.read_share(db, user_id=RECIPIENT, share_id=share.id)
    assert (await sharing.read_share_content(db, user_id=RECIPIENT, share_id=share.id))[
        0
    ] == b"report\n"
    assert [
        item.id
        for item in await sharing.list_artifact_shares(
            db, user_id=USER, organization_id=ORG, artifact_id=version.artifact_id
        )
    ] == [share.id]

    with pytest.raises(ArtifactNotFound):
        await sharing.revoke_share(db, user_id=RECIPIENT, share_id=share.id)
    await sharing.revoke_share(db, user_id=USER, share_id=share.id)
    await _deny_both(db, share.id)
    with pytest.raises(ArtifactSharingDisabled):
        await sharing.create_share(
            db,
            user_id=USER,
            organization_id=ORG,
            version_id=version.version_id,
            audience_user_ids=[RECIPIENT],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )


async def test_recipients_cannot_revoke_or_list_others_shares(
    db: AsyncSession, context: IntegrationContext
) -> None:
    share, version = await _share(db, context)
    with pytest.raises(ArtifactNotFound):
        await sharing.revoke_share(db, user_id=RECIPIENT, share_id=share.id)
    with pytest.raises(ArtifactNotFound):
        await sharing.list_artifact_shares(
            db,
            user_id=EDITOR_USER,
            organization_id=ORG,
            artifact_id=version.artifact_id,
        )
    assert await sharing.read_share(db, user_id=RECIPIENT, share_id=share.id)


async def test_missing_or_corrupted_blob_is_not_returned(
    db: AsyncSession, context: IntegrationContext, storage: MemoryArtifactStorage
) -> None:
    share, _ = await _share(db, context)
    for key in storage.objects:
        storage.objects[key] = b"changed behind immutable version"
    with pytest.raises(ArtifactStorageUnavailable):
        await sharing.read_share_content(db, user_id=RECIPIENT, share_id=share.id)
    storage.objects.clear()
    with pytest.raises(ArtifactStorageUnavailable):
        await sharing.read_share_content(db, user_id=RECIPIENT, share_id=share.id)


async def test_revocation_while_storage_loads_prevents_byte_release(
    db: AsyncSession,
    context: IntegrationContext,
    storage: MemoryArtifactStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    share, _ = await _share(db, context)
    original_get = storage.get

    async def revoke_during_load(key: str) -> bytes | None:
        content = await original_get(key)
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
            await sharing.revoke_share(writer, user_id=USER, share_id=share.id)
        return content

    monkeypatch.setattr(storage, "get", revoke_during_load)
    with pytest.raises(ArtifactNotFound):
        await sharing.read_share_content(db, user_id=RECIPIENT, share_id=share.id)
