"""Owner listing and revocation of connected devices, proven end to end."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.bridge_device import BridgeDevice
from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
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
TOKENS = {name: "nous_ig_" + name.ljust(43, "x") for name in ("a", "a2", "b", "d")}


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
