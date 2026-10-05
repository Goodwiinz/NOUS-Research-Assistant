"""A browser consent request may bind its grant lineage to one chat (Plan 06)."""

import hashlib
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.artifact import (
    Artifact,
    ArtifactLifecycleOutbox,
    ArtifactReference,
    ArtifactUpload,
    ArtifactVersion,
)
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.artifact import (
    ArtifactProvenance,
    PublishVersionRequest,
    ReserveArtifactUploadRequest,
)
from src.schemas.integration_context import (
    DeviceCreate,
    GrantRequestCreate,
    GrantRequestDTO,
)
from src.services.artifacts import service as artifacts
from src.services.artifacts.storage import MemoryArtifactStorage
from src.services.integrations.context import (
    IntegrationAccessDenied,
    create_request,
    decide_request,
    exchange_request,
    register_device,
    renew_grant,
    request_dto,
    resolve_integration_context,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG = uuid4(), uuid4(), uuid4()
WORKSPACE, OTHER_WORKSPACE, PROJECT, OTHER_PROJECT = (uuid4() for _ in range(4))
CONVERSATION, OTHER_CONVERSATION = uuid4(), uuid4()
THREAD, UNTITLED_THREAD = uuid4(), uuid4()
FOREIGN_THREADS = {
    "other_project_thread": uuid4(),
    "other_owner_thread": uuid4(),
    "deleted_thread": uuid4(),
}
SCOPES = {"tools:read", "artifacts:publish"}
CONTENT = b"handoff\n"
OWNER = SimpleNamespace(id=USER, organization_id=ORG)


def _user(user_id: UUID, email: str) -> dict[str, str]:
    return {"id": str(user_id), "org": str(ORG), "email": email}


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        AgentRun,
        BridgeDevice,
        WorkspaceBinding,
        IntegrationGrantRequest,
        IntegrationGrant,
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
            insert(Organization).values(id=ORG, name="Org", storage_limit_bytes=10**6)
        )
        for params in (_user(USER, "o@example.test"), _user(OTHER_USER, "x@e.test")):
            await session.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', 'U', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
                ),
                params,
            )
        await session.execute(
            insert(Workspace).values(
                [
                    dict(id=WORKSPACE, name="W", owner_id=USER, organization_id=ORG),
                    dict(
                        id=OTHER_WORKSPACE,
                        name="X",
                        owner_id=OTHER_USER,
                        organization_id=ORG,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=WORKSPACE),
                ]
            )
        )
        await session.execute(
            insert(Conversation).values(
                [
                    dict(
                        id=CONVERSATION,
                        workspace_id=WORKSPACE,
                        title="C",
                        created_by_id=USER,
                    ),
                    dict(
                        id=OTHER_CONVERSATION,
                        workspace_id=OTHER_WORKSPACE,
                        title="X",
                        created_by_id=OTHER_USER,
                    ),
                ]
            )
        )

        def thread(thread_id: UUID, **values: Any) -> dict[str, Any]:
            return dict(
                id=thread_id,
                conversation_id=values.pop("conversation_id", CONVERSATION),
                created_by_id=values.pop("created_by_id", USER),
                source_project_id=values.pop("source_project_id", PROJECT),
                title=values.pop("title", None),
                is_deleted=values.pop("is_deleted", False),
            )

        await session.execute(
            insert(Thread).values(
                [
                    thread(THREAD, title="Literature review"),
                    thread(UNTITLED_THREAD),
                    thread(
                        FOREIGN_THREADS["other_project_thread"],
                        source_project_id=OTHER_PROJECT,
                    ),
                    thread(
                        FOREIGN_THREADS["other_owner_thread"],
                        conversation_id=OTHER_CONVERSATION,
                        created_by_id=OTHER_USER,
                    ),
                    thread(FOREIGN_THREADS["deleted_thread"], is_deleted=True),
                ]
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _request(db: AsyncSession, thread_id: UUID) -> GrantRequestDTO:
    device = await register_device(db, OWNER, DeviceCreate(label="Laptop"))
    return await create_request(
        db,
        OWNER,
        GrantRequestCreate(
            project_id=PROJECT, device_id=device.id, scopes=SCOPES, thread_id=thread_id
        ),
    )


async def _issue(db: AsyncSession, thread_id: UUID) -> Any:
    request = await _request(db, thread_id)
    await decide_request(db, OWNER, request.id, approved=True)
    return await exchange_request(db, OWNER, request.id)


async def test_request_with_thread_binds_the_issued_grant(db: AsyncSession) -> None:
    request = await _request(db, THREAD)
    assert request.thread_id == THREAD
    assert request.thread_label == "Literature review"
    await decide_request(db, OWNER, request.id, approved=True)
    issued = await exchange_request(db, OWNER, request.id)
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="artifacts:publish"
    )
    assert ctx.thread_id == THREAD and ctx.run_id is None


@pytest.mark.parametrize("bad", sorted(FOREIGN_THREADS))
async def test_foreign_or_dead_thread_is_refused(db: AsyncSession, bad: str) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await _request(db, FOREIGN_THREADS[bad])
    # Refused before anything is stored, not merely hidden on read-back.
    assert await db.scalar(select(func.count(IntegrationGrantRequest.id))) == 0


@pytest.mark.parametrize(
    "change,approve_first",
    [
        ({"source_project_id": None}, False),  # moved before approval
        ({"is_deleted": True}, True),  # deleted between approve and exchange
    ],
)
async def test_thread_changed_after_request_blocks_issuance(
    db: AsyncSession, change: dict[str, Any], approve_first: bool
) -> None:
    request = await _request(db, THREAD)
    if approve_first:
        await decide_request(db, OWNER, request.id, approved=True)
    await db.execute(update(Thread).where(Thread.id == THREAD).values(**change))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        if approve_first:
            await exchange_request(db, OWNER, request.id)
        else:
            await decide_request(db, OWNER, request.id, approved=True)


async def test_renewal_keeps_thread(db: AsyncSession) -> None:
    issued = await _issue(db, THREAD)
    renewed = await renew_grant(db, OWNER, issued.grant_id)
    ctx = await resolve_integration_context(
        db, renewed.token, required_scope="tools:read"
    )
    assert ctx.thread_id == THREAD


async def test_null_title_thread_label(db: AsyncSession) -> None:
    request = await _request(db, UNTITLED_THREAD)
    assert (await request_dto(db, OWNER, request.id)).thread_label == "Untitled chat"


async def test_request_without_thread_has_no_label(db: AsyncSession) -> None:
    device = await register_device(db, OWNER, DeviceCreate(label="Laptop"))
    request = await create_request(
        db,
        OWNER,
        GrantRequestCreate(project_id=PROJECT, device_id=device.id, scopes=SCOPES),
    )
    assert request.thread_id is None and request.thread_label is None


async def test_standalone_publication_lands_in_the_bound_chat(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        artifacts,
        "get_artifact_storage",
        lambda storage=MemoryArtifactStorage(): storage,
    )
    issued = await _issue(db, THREAD)
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="artifacts:publish"
    )
    reserve = ReserveArtifactUploadRequest(
        publication_id=uuid4(),
        byte_size=len(CONTENT),
        mime_type="text/markdown",
        sha256=hashlib.sha256(CONTENT).hexdigest(),
    )
    upload = await artifacts.reserve_upload(db, ctx, reserve)
    await artifacts.store_upload(db, ctx, upload.upload_id, CONTENT)
    version = await artifacts.publish_version(
        db,
        ctx,
        PublishVersionRequest(
            publication_id=reserve.publication_id,
            upload_id=upload.upload_id,
            title="handoff.md",
            provenance=ArtifactProvenance(producer="harness"),
        ),
    )
    rows = await artifacts.list_thread_artifacts(
        db, user_id=USER, organization_id=ORG, thread_id=THREAD
    )
    assert [r.reference.version_id for r in rows] == [version.version_id]
    assert rows[0].reference.run_id is None
