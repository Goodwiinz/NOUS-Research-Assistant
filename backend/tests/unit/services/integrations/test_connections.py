"""Owner listing and revocation of connected devices, proven end to end."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core import cli_token_revocation as ctr
from src.models.bridge_device import BridgeDevice
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_connections import ConnectionConsent
from src.services.integrations.connections import (
    ConnectionNotFound,
    disconnect_device,
    list_connections,
    revoke_consent,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    resolve_integration_context,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG, WORKSPACE, PROJECT, OTHER_PROJECT = (uuid4() for _ in range(6))
LAPTOP, DESKTOP = uuid4(), uuid4()
CONSENT_A, CONSENT_B, CONSENT_DESKTOP, CONSENT_DENIED = (uuid4() for _ in range(4))
SOON = datetime.now(timezone.utc) + timedelta(hours=1)
TOKENS = {name: "nous_ig_" + name.ljust(43, "x") for name in ("a", "a2", "b", "d", "w")}
# A consumed Plan 07 workspace consent on the desktop (`workspace_consent`).
CONSENT_WORKSPACE = uuid4()
CONVERSATION, CHAT = uuid4(), uuid4()
LIBRARY = ["library:read", "tools:read"]


@pytest.fixture(autouse=True)
def cli_store(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    client.eval.return_value = 1
    monkeypatch.setattr(ctr, "_get_redis", AsyncMock(return_value=client))
    return client


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        insert(Organization).values(id=ORG, name="Org", storage_limit_bytes=1000000)
    )
    for user_id, email in ((USER, "owner@example.test"), (OTHER_USER, "o@e.test")):
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(user_id), "org": str(ORG), "email": email},
        )
    await session.execute(
        insert(Workspace).values(
            id=WORKSPACE, name="Workspace", owner_id=USER, organization_id=ORG
        )
    )
    for project_id, name in ((PROJECT, "Thesis"), (OTHER_PROJECT, "Grant")):
        await session.execute(
            insert(Collection).values(id=project_id, name=name, workspace_id=WORKSPACE)
        )
    for device_id, label in ((LAPTOP, "Laptop"), (DESKTOP, "Desktop")):
        await session.execute(
            insert(BridgeDevice).values(
                id=device_id, user_id=USER, organization_id=ORG, label=label
            )
        )
    for consent_id, device_id, project_id, status in (
        (CONSENT_A, LAPTOP, PROJECT, "consumed"),
        (CONSENT_B, LAPTOP, OTHER_PROJECT, "consumed"),
        (CONSENT_DESKTOP, DESKTOP, PROJECT, "consumed"),
        (CONSENT_DENIED, LAPTOP, PROJECT, "denied"),
    ):
        await session.execute(
            insert(IntegrationGrantRequest).values(
                id=consent_id,
                user_id=USER,
                organization_id=ORG,
                project_id=project_id,
                device_id=device_id,
                scopes=["tools:read", "harness:execute"],
                status=status,
                expires_at=SOON,
            )
        )
    # CONSENT_A carries a renewed grant too: both must stop working.
    for token_name, consent_id, device_id, project_id in (
        ("a", CONSENT_A, LAPTOP, PROJECT),
        ("a2", CONSENT_A, LAPTOP, PROJECT),
        ("b", CONSENT_B, LAPTOP, OTHER_PROJECT),
        ("d", CONSENT_DESKTOP, DESKTOP, PROJECT),
    ):
        await session.execute(
            insert(IntegrationGrant).values(
                id=uuid4(),
                user_id=USER,
                organization_id=ORG,
                project_id=project_id,
                device_id=device_id,
                request_id=consent_id,
                scopes=["tools:read", "harness:execute"],
                token_hash=sha256(TOKENS[token_name].encode()).hexdigest(),
                expires_at=SOON,
                consented_at=datetime.now(timezone.utc),
            )
        )
    await session.commit()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'connections.db'}")
    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
        Conversation,
        Thread,
        BridgeDevice,
        IntegrationGrantRequest,
        IntegrationGrant,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await _seed(session)
        yield session
    await engine.dispose()


@pytest.fixture
async def workspace_consent(db: AsyncSession) -> None:
    """CONSENT_WORKSPACE: every live project of WORKSPACE, with its grant."""
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=CONSENT_WORKSPACE,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            device_id=DESKTOP,
            scopes=LIBRARY,
            status="consumed",
            expires_at=SOON,
        )
    )
    await db.execute(
        insert(IntegrationGrant).values(
            id=uuid4(),
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            device_id=DESKTOP,
            request_id=CONSENT_WORKSPACE,
            scopes=LIBRARY,
            token_hash=sha256(TOKENS["w"].encode()).hexdigest(),
            expires_at=SOON,
            consented_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


@pytest.fixture
async def chat_bound(db: AsyncSession) -> None:
    """CONSENT_A is bound to CHAT, a chat of PROJECT (Plan 06 slice 2)."""
    await db.execute(
        insert(Conversation).values(
            id=CONVERSATION, workspace_id=WORKSPACE, title="C", created_by_id=USER
        )
    )
    await db.execute(
        insert(Thread).values(
            id=CHAT,
            conversation_id=CONVERSATION,
            created_by_id=USER,
            source_project_id=PROJECT,
            title="Literature review",
        )
    )
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == CONSENT_A)
        .values(thread_id=CHAT)
    )
    await db.commit()


async def _consents(db: AsyncSession, device_id: UUID) -> dict[UUID, ConnectionConsent]:
    devices = await list_connections(db, await _user(db))
    device = next((d for d in devices if d.device_id == device_id), None)
    assert device is not None
    return {c.request_id: c for c in device.consents}


async def _user(db: AsyncSession, user_id: UUID = USER) -> User:
    user = await db.get(User, user_id)
    assert user is not None
    return cast(User, user)


async def _works(db: AsyncSession, token_name: str) -> bool:
    try:
        await resolve_integration_context(
            db, TOKENS[token_name], required_scope="tools:read"
        )
    except IntegrationAccessDenied:
        return False
    return True


async def test_lists_each_device_with_its_live_consents(db: AsyncSession) -> None:
    devices = await list_connections(db, await _user(db))
    by_label = {d.device_label: d for d in devices}
    assert set(by_label) == {"Laptop", "Desktop"}
    laptop = by_label["Laptop"]
    assert {c.request_id for c in laptop.consents} == {CONSENT_A, CONSENT_B}
    assert {c.project_label for c in laptop.consents} == {"Thesis", "Grant"}
    assert laptop.consents[0].scopes == ["harness:execute", "tools:read"]
    assert await list_connections(db, await _user(db, OTHER_USER)) == []


async def test_revoking_one_consent_stops_only_that_project(db: AsyncSession) -> None:
    assert all([await _works(db, t) for t in ("a", "a2", "b", "d")])
    await revoke_consent(db, await _user(db), CONSENT_A)
    assert not await _works(db, "a")
    assert not await _works(db, "a2")  # the renewed grant too
    assert await _works(db, "b")
    assert await _works(db, "d")
    laptop = next(
        d for d in await list_connections(db, await _user(db)) if d.device_id == LAPTOP
    )
    assert [c.request_id for c in laptop.consents] == [CONSENT_B]
    grants = (
        await db.scalars(
            select(IntegrationGrant).where(IntegrationGrant.request_id == CONSENT_A)
        )
    ).all()
    assert all(g.revoked_at is not None for g in grants)


async def test_disconnecting_a_device_stops_all_its_access(db: AsyncSession) -> None:
    await disconnect_device(db, await _user(db), LAPTOP)
    assert not await _works(db, "a")
    assert not await _works(db, "b")
    assert await _works(db, "d")
    labels = [d.device_label for d in await list_connections(db, await _user(db))]
    assert labels == ["Desktop"]


@pytest.mark.parametrize("case", ["other-user", "unknown", "twice"])
async def test_revocation_is_owner_only_and_single_use(
    db: AsyncSession, case: str
) -> None:
    if case == "twice":
        await revoke_consent(db, await _user(db), CONSENT_A)
        await disconnect_device(db, await _user(db), DESKTOP)
    user = await _user(db, OTHER_USER if case == "other-user" else USER)
    request_id = uuid4() if case == "unknown" else CONSENT_A
    device_id = uuid4() if case == "unknown" else DESKTOP
    with pytest.raises(ConnectionNotFound):
        await revoke_consent(db, user, request_id)
    with pytest.raises(ConnectionNotFound):
        await disconnect_device(db, user, device_id)
    if case == "other-user":
        assert await _works(db, "a") and await _works(db, "d")


async def test_a_user_without_an_organization_sees_nothing(db: AsyncSession) -> None:
    orphan = await _user(db)
    await db.execute(update(User).where(User.id == USER).values(is_active=True))
    setattr(orphan, "organization_id", None)
    with pytest.raises(ConnectionNotFound):
        await list_connections(db, orphan)


@pytest.mark.parametrize("ancestor", ["project", "workspace", "organization", "member"])
async def test_listing_hides_projects_after_access_is_lost(
    db: AsyncSession, ancestor: str
) -> None:
    if ancestor == "project":
        await db.execute(
            update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
        )
    elif ancestor == "workspace":
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
    elif ancestor == "organization":
        await db.execute(
            update(Organization).where(Organization.id == ORG).values(is_active=False)
        )
    else:
        await db.execute(
            update(Workspace)
            .where(Workspace.id == WORKSPACE)
            .values(owner_id=OTHER_USER)
        )
        await db.execute(
            insert(WorkspaceMember).values(workspace_id=WORKSPACE, user_id=USER)
        )
        await db.commit()
        assert any(d.consents for d in await list_connections(db, await _user(db)))
        await db.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.user_id == USER)
            .values(is_deleted=True)
        )
    await db.commit()
    devices = await list_connections(db, await _user(db))
    assert len(devices) == 2
    consents = [c for d in devices for c in d.consents]
    assert all(c.project_id != PROJECT for c in consents)
    if ancestor == "project":
        assert [c.project_id for c in consents] == [OTHER_PROJECT]
    else:
        assert consents == []


async def test_expired_approval_is_hidden_but_consumed_consent_remains(
    db: AsyncSession,
) -> None:
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.execute(update(IntegrationGrantRequest).values(expires_at=expired))
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == CONSENT_A)
        .values(status="approved")
    )
    await db.commit()
    devices = await list_connections(db, await _user(db))
    ids = {c.request_id for d in devices for c in d.consents}
    assert ids == {CONSENT_B, CONSENT_DESKTOP}
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == CONSENT_A)
        .values(expires_at=SOON)
    )
    await db.commit()
    assert CONSENT_A in {
        c.request_id
        for d in await list_connections(db, await _user(db))
        for c in d.consents
    }


@pytest.mark.parametrize("failure", ["missing", "error", "unconfirmed"])
async def test_failed_cli_revocation_rolls_back_device_and_grants(
    db: AsyncSession,
    cli_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "missing":
        monkeypatch.setattr(ctr, "_get_redis", AsyncMock(return_value=None))
    elif failure == "error":
        cli_store.eval.side_effect = RuntimeError("private redis address")
    else:
        cli_store.eval.return_value = 0
    with pytest.raises(ctr.CliTokenRevocationUnavailable):
        await disconnect_device(db, await _user(db), LAPTOP)
    device = await db.get(BridgeDevice, LAPTOP)
    consent = await db.get(IntegrationGrantRequest, CONSENT_A)
    assert device is not None and device.revoked_at is None
    assert consent is not None and consent.consent_revoked_at is None
    assert all([await _works(db, t) for t in ("a", "a2", "b", "d")])


# DV-1: a workspace consent (Plan 07) was labelled through authorized_project
# with project_id None and silently dropped.
async def test_lists_a_workspace_consent_with_its_workspace(
    db: AsyncSession, workspace_consent: None
) -> None:
    consents = await _consents(db, DESKTOP)
    assert set(consents) == {CONSENT_DESKTOP, CONSENT_WORKSPACE}
    listed = consents[CONSENT_WORKSPACE]
    assert listed.kind == "workspace"
    assert (listed.workspace_id, listed.workspace_label) == (WORKSPACE, "Workspace")
    assert (listed.project_id, listed.project_label) == (None, None)
    assert listed.scopes == LIBRARY
    project = consents[CONSENT_DESKTOP]
    assert project.kind == "project"
    assert (project.project_label, project.workspace_id) == ("Thesis", None)


async def test_revoking_a_workspace_consent_stops_only_it(
    db: AsyncSession, workspace_consent: None
) -> None:
    assert CONSENT_WORKSPACE in await _consents(db, DESKTOP)
    assert await _works(db, "w") and await _works(db, "d")
    await revoke_consent(db, await _user(db), CONSENT_WORKSPACE)
    assert not await _works(db, "w")
    assert await _works(db, "d")
    assert set(await _consents(db, DESKTOP)) == {CONSENT_DESKTOP}


async def test_a_workspace_consent_is_hidden_once_its_workspace_is_gone(
    db: AsyncSession, workspace_consent: None
) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
    )
    await db.commit()
    assert await _consents(db, DESKTOP) == {}


# AD-3: a chat-bound device works only from its chat; say which one.
async def test_a_chat_bound_consent_names_its_chat(
    db: AsyncSession, chat_bound: None
) -> None:
    consents = await _consents(db, LAPTOP)
    bound, unbound = consents[CONSENT_A], consents[CONSENT_B]
    assert (bound.thread_id, bound.thread_label) == (CHAT, "Literature review")
    assert (unbound.thread_id, unbound.thread_label) == (None, None)


@pytest.mark.parametrize("change", ["deleted", "moved"])
async def test_a_consent_whose_chat_is_gone_stays_listed_without_a_label(
    db: AsyncSession, chat_bound: None, change: str
) -> None:
    # It can no longer mint (validate_binding refuses the chat) but stays
    # listed, so the owner can still revoke it (DECISION C-1).
    values: dict[str, Any] = (
        {"is_deleted": True}
        if change == "deleted"
        else {"source_project_id": OTHER_PROJECT}
    )
    await db.execute(update(Thread).where(Thread.id == CHAT).values(**values))
    await db.commit()
    listed = (await _consents(db, LAPTOP))[CONSENT_A]
    assert (listed.thread_id, listed.thread_label) == (CHAT, None)
