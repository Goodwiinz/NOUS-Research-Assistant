"""Project-scoped artifact discovery, using a local SQLite database."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
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
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.artifact import (
    ArtifactNotFound,
    ArtifactProvenance,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import IntegrationContext
from src.services.artifacts import service
from src.services.artifacts.service import (
    list_project_artifacts,
    publish_version,
    read_version_content,
    reserve_upload,
    store_upload,
)
from src.services.artifacts.storage import MemoryArtifactStorage

pytestmark = pytest.mark.unit
USER, MEMBER, SAME_ORG_OUTSIDER, FOREIGN_USER = (uuid4() for _ in range(4))
ORG, OTHER_ORG, WORKSPACE, OTHER_WORKSPACE = (uuid4() for _ in range(4))
PROJECT, SIBLING_PROJECT, OTHER_PROJECT = (uuid4() for _ in range(3))
CONVERSATION, THREAD = uuid4(), uuid4()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _user_sql(user_id: UUID, org: UUID, email: str) -> tuple[str, dict[str, str]]:
    return (
        "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)",
        {"id": str(user_id), "org": str(org), "email": email},
    )


async def _artifact(
    db: AsyncSession,
    *,
    title: str,
    versions: list[int],
    project: UUID = PROJECT,
    org: UUID = ORG,
    thread: UUID | None = None,
    current: int | None = -1,
    deleted: bool = False,
    deleted_current: bool = False,
) -> tuple[UUID, list[UUID]]:
    """Insert an artifact with one version per offset (minutes after T0)."""
    artifact_id = uuid4()
    version_ids = [uuid4() for _ in versions]
    await db.execute(
        insert(ArtifactVersion).values(
            [
                dict(
                    id=version_id,
                    artifact_id=artifact_id,
                    upload_id=uuid4(),
                    title=f"{title} v{index}",
                    mime_type="text/markdown",
                    byte_size=7,
                    sha256="a" * 64,
                    storage_key=f"k/{version_id}",
                    producer="harness",
                    provenance={"producer": "harness"},
                    thread_id=thread,
                    created_at=T0 + timedelta(minutes=minutes),
                    is_deleted=deleted_current and index == len(versions) - 1,
                )
                for index, (version_id, minutes) in enumerate(
                    zip(version_ids, versions)
                )
            ]
        )
    )
    await db.execute(
        insert(Artifact).values(
            id=artifact_id,
            organization_id=org,
            project_id=project,
            owner_id=USER,
            title=title,
            current_version_id=None if current is None else version_ids[current],
            is_deleted=deleted,
        )
    )
    await db.commit()
    return artifact_id, version_ids


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'project.db'}")
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
        ArtifactLifecycleOutbox,
        AgentRun,
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
            (MEMBER, ORG, "member@example.test"),
            (SAME_ORG_OUTSIDER, ORG, "outsider@example.test"),
            (FOREIGN_USER, OTHER_ORG, "foreign@example.test"),
        ):
            sql, params = _user_sql(user_id, org, email)
            await session.execute(text(sql), params)
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
                        owner_id=FOREIGN_USER,
                        organization_id=OTHER_ORG,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=SIBLING_PROJECT, name="Sibling", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=OTHER_WORKSPACE),
                ]
            )
        )
        await session.execute(
            insert(WorkspaceMember).values(
                workspace_id=WORKSPACE, user_id=MEMBER, role=WorkspaceRole.VIEWER
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


async def _list(
    db: AsyncSession, user: UUID = MEMBER, org: UUID = ORG, **kwargs: Any
) -> list[Any]:
    return await list_project_artifacts(
        db, user_id=user, organization_id=org, project_id=PROJECT, **kwargs
    )


async def test_lists_latest_version_per_artifact_for_member(db: AsyncSession) -> None:
    a1, (_a1_v1,) = await _artifact(db, title="old", versions=[10])
    a2, (_a2_v1, a2_v2) = await _artifact(
        db, title="new", versions=[5, 30], thread=THREAD
    )
    # Current pointer may lag the newest row; the pointer wins.
    a3, (a3_v1, _a3_v2) = await _artifact(
        db, title="pinned", versions=[20, 40], current=0
    )
    rows = await _list(db)
    assert [r.artifact_id for r in rows] == [a2, a3, a1]  # newest current version first
    assert rows[0].current_version.version_id == a2_v2
    assert rows[0].thread_id == THREAD
    assert rows[0].updated_at == T0 + timedelta(minutes=30)
    assert rows[1].current_version.version_id == a3_v1
    assert rows[2].thread_id is None  # standalone artifact still listed
    assert not hasattr(rows[0].current_version, "storage_key")


async def test_hides_dead_and_foreign_rows(db: AsyncSession) -> None:
    live, _ = await _artifact(db, title="live", versions=[1])
    await _artifact(db, title="unfinalized", versions=[2], current=None)
    await _artifact(db, title="deleted", versions=[3], deleted=True)
    await _artifact(db, title="dead version", versions=[4], deleted_current=True)
    await _artifact(db, title="sibling", versions=[5], project=SIBLING_PROJECT)
    await _artifact(db, title="foreign org", versions=[6], org=OTHER_ORG)
    assert [r.artifact_id for r in await _list(db)] == [live]


async def test_limit_caps_rows(db: AsyncSession) -> None:
    for minute in range(3):
        await _artifact(db, title=f"a{minute}", versions=[minute])
    assert len(await _list(db, limit=2)) == 2


@pytest.mark.parametrize("case", ["owner", "member"])
async def test_owner_and_member_can_list(db: AsyncSession, case: str) -> None:
    await _artifact(db, title="a", versions=[1])
    assert len(await _list(db, user=USER if case == "owner" else MEMBER)) == 1


@pytest.mark.parametrize(
    "case", ["foreign_org", "same_org_non_member", "deleted_project", "deleted_ws"]
)
async def test_outsiders_and_deleted_ancestors_are_not_found(
    db: AsyncSession, case: str
) -> None:
    await _artifact(db, title="a", versions=[1])
    user, org = MEMBER, ORG
    if case == "foreign_org":
        user, org = FOREIGN_USER, OTHER_ORG
    elif case == "same_org_non_member":
        user = SAME_ORG_OUTSIDER
    elif case == "deleted_project":
        await db.execute(
            update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
        )
        await db.commit()
    else:
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
        await db.commit()
    with pytest.raises(ArtifactNotFound):
        await _list(db, user=user, org=org)


async def test_unscoped_publication_is_listed_and_hash_matches_bytes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = MemoryArtifactStorage()
    monkeypatch.setattr(service, "get_artifact_storage", lambda: storage)
    content = b"standalone report\n"
    digest = hashlib.sha256(content).hexdigest()
    context = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )  # thread_id=None: no chat bound
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(content),
        mime_type="text/markdown",
        sha256=digest,
    )
    upload = await reserve_upload(db, context, reserve)
    await store_upload(db, context, upload.upload_id, content)
    published = await publish_version(
        db,
        context,
        PublishVersionRequest(
            publication_id=reserve.publication_id,
            upload_id=upload.upload_id,
            title="standalone.md",
            provenance=ArtifactProvenance(producer="harness"),
        ),
    )
    (row,) = await _list(db)
    assert row.thread_id is None
    assert row.current_version.version_id == published.version_id
    body, _mime, _title = await read_version_content(
        db,
        user_id=MEMBER,
        organization_id=ORG,
        version_id=row.current_version.version_id,
    )
    assert hashlib.sha256(body).hexdigest() == row.current_version.sha256
