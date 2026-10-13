"""Resuming a device's lapsed grant from its browser consent (2026-10-13).

Only the holder of the grant's own token can resume it, and only while the
consent, the device and the binding still validate, within 30 days of expiry.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_run import AgentRun
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import (
    DeviceCreate,
    GrantRequestCreate,
    IssuedGrant,
)
from src.services.integrations import connections
from src.services.integrations.context import (
    RESUME_WINDOW,
    IntegrationAccessDenied,
    IntegrationConflict,
    create_request,
    decide_request,
    exchange_request,
    mint_integration_grant,
    register_device,
    renew_grant,
    resolve_integration_context,
    resume_grant,
    revoke_integration_grant,
)

pytestmark = pytest.mark.unit
USER, OTHER_USER, ORG = uuid4(), uuid4(), uuid4()
WORKSPACE, PROJECT, OTHER_PROJECT = uuid4(), uuid4(), uuid4()
CONVERSATION, THREAD = uuid4(), uuid4()
OWNER = SimpleNamespace(id=USER, organization_id=ORG)
OTHER = SimpleNamespace(id=OTHER_USER, organization_id=ORG)
# What `nous --rc` pairs with: harness runs plus project reads.
SCOPES = {"harness:execute", "tools:read"}


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
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(id=ORG, name="Org", storage_limit_bytes=10**6)
        )
        for user_id, email in ((USER, "o@example.test"), (OTHER_USER, "x@e.test")):
            await session.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', 'U', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
                ),
                {"id": str(user_id), "org": str(ORG), "email": email},
            )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE, name="W", owner_id=USER, organization_id=ORG
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
                id=CONVERSATION, workspace_id=WORKSPACE, title="C", created_by_id=USER
            )
        )
        await session.execute(
            insert(Thread).values(
                id=THREAD,
                conversation_id=CONVERSATION,
                created_by_id=USER,
                source_project_id=PROJECT,
                title="Chat",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _pair(
    db: AsyncSession, *, thread_id: UUID | None = None
) -> tuple[UUID, UUID, IssuedGrant]:
    """Register a device and pair it the way `connect` does: create, approve
    in the browser, exchange. Returns (device, consent, issued grant)."""
    device = await register_device(db, OWNER, DeviceCreate(label="Laptop"))
    request = await create_request(
        db,
        OWNER,
        GrantRequestCreate(
            project_id=PROJECT, device_id=device.id, scopes=SCOPES, thread_id=thread_id
        ),
    )
    await decide_request(db, OWNER, request.id, True)
    issued = await exchange_request(db, OWNER, request.id)
    return device.id, request.id, issued


@pytest.fixture
async def paired(db: AsyncSession) -> tuple[UUID, UUID, IssuedGrant]:
    return await _pair(db)


async def _lapse(db: AsyncSession, grant_id: UUID, ago: timedelta) -> None:
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == grant_id)
        .values(expires_at=datetime.now(timezone.utc) - ago)
    )
    await db.commit()


async def _refused(db: AsyncSession, grant_id: UUID, token: str) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await resume_grant(db, OWNER, grant_id, token)


async def test_lapsed_grant_resumes_under_the_same_consent_and_device(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    device, consent, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")

    resumed = await resume_grant(db, OWNER, issued.grant_id, issued.token)

    assert resumed.grant_id != issued.grant_id and resumed.token != issued.token
    context = await resolve_integration_context(
        db, resumed.token, required_scope="harness:execute"
    )
    assert context.project_id == PROJECT
    assert context.consent_id == consent
    assert context.scopes == frozenset(SCOPES)
    successor = await db.get(IntegrationGrant, resumed.grant_id)
    assert successor is not None and successor.device_id == device
    old = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    assert old is not None and old.revoked_at is not None
    # No new approval and no new device: the pairing is the same one.
    assert (
        await db.scalar(select(func.count()).select_from(IntegrationGrantRequest)) == 1
    )
    assert await db.scalar(select(func.count()).select_from(BridgeDevice)) == 1


async def test_a_resumed_grant_cannot_be_resumed_twice(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    resumed = await resume_grant(db, OWNER, issued.grant_id, issued.token)
    await _refused(db, issued.grant_id, issued.token)
    # The successor is the one that resumes next time.
    await _lapse(db, resumed.grant_id, timedelta(hours=1))
    assert (await resume_grant(db, OWNER, resumed.grant_id, resumed.token)).token


async def test_a_live_grant_resumes_like_a_renewal(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, consent, issued = paired
    resumed = await resume_grant(db, OWNER, issued.grant_id, issued.token)
    context = await resolve_integration_context(
        db, resumed.token, required_scope="tools:read"
    )
    assert context.consent_id == consent
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")


async def test_resume_refuses_without_the_grant_token(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    _, _, other = await _pair(db)
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    for token in (
        "",
        "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        other.token,
        # The right shape, but not this grant's token.
        "nous_ig_" + "A" * 43,
        issued.token + "x",
    ):
        await _refused(db, issued.grant_id, token)
    # Nothing was spent by the refusals.
    assert (await resume_grant(db, OWNER, issued.grant_id, issued.token)).token


async def test_resume_refuses_another_users_grant(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    for caller in (OTHER, SimpleNamespace(id=USER, organization_id=uuid4())):
        with pytest.raises(IntegrationAccessDenied):
            await resume_grant(db, caller, issued.grant_id, issued.token)


async def test_resume_refuses_a_token_renewed_away(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    renewed = await renew_grant(db, OWNER, issued.grant_id)
    await _lapse(db, renewed.grant_id, timedelta(hours=1))
    await _refused(db, issued.grant_id, issued.token)
    assert (await resume_grant(db, OWNER, renewed.grant_id, renewed.token)).token


async def test_resume_refuses_after_cli_disconnect(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    await revoke_integration_grant(db, issued.grant_id)
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_revoked_consent(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, consent, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await connections.revoke_consent(db, OWNER, consent)  # type: ignore[arg-type]
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_consent_revoked_without_its_grants(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    """The consent row alone is checked, not only the grant's revocation."""
    _, consent, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == consent)
        .values(consent_revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_disconnected_device(
    db: AsyncSession,
    paired: tuple[UUID, UUID, IssuedGrant],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, _, issued = paired

    async def no_cutoff(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(connections, "revoke_user_cli_tokens", no_cutoff)
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await connections.disconnect_device(db, OWNER, device)  # type: ignore[arg-type]
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_revoked_device_whose_grant_was_not_revoked(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    """validate_binding re-checks the device itself."""
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(update(BridgeDevice).values(revoked_at=datetime.now(timezone.utc)))
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


@pytest.mark.parametrize(
    "model,values",
    [
        (Collection, {"is_deleted": True}),
        (Workspace, {"is_deleted": True}),
        (User, {"is_active": False}),
        (Organization, {"is_active": False}),
    ],
)
async def test_resume_refuses_when_the_binding_no_longer_validates(
    db: AsyncSession,
    paired: tuple[UUID, UUID, IssuedGrant],
    model: Any,
    values: dict[str, Any],
) -> None:
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(update(model).values(**values))
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_chat_bound_grant_whose_chat_moved(
    db: AsyncSession,
) -> None:
    _, _, issued = await _pair(db, thread_id=THREAD)
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(
        update(Thread)
        .where(Thread.id == THREAD)
        .values(source_project_id=OTHER_PROJECT)
    )
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)
    await db.execute(
        update(Thread).where(Thread.id == THREAD).values(source_project_id=PROJECT)
    )
    await db.commit()
    context = await resolve_integration_context(
        db,
        (await resume_grant(db, OWNER, issued.grant_id, issued.token)).token,
        required_scope="tools:read",
    )
    assert context.thread_id == THREAD


async def test_resume_refuses_scopes_wider_than_the_consent(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == issued.grant_id)
        .values(scopes=sorted(SCOPES | {"tools:write"}))
    )
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_grant_without_device_or_consent(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    # A trusted internal grant with no device has no pairing to resume.
    internal = await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )
    await _lapse(db, internal.grant_id, timedelta(hours=1))
    await _refused(db, internal.grant_id, internal.token)
    # A device grant whose consent link is gone (ON DELETE SET NULL).
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == issued.grant_id)
        .values(request_id=None)
    )
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_refuses_a_run_grant(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    _, _, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == issued.grant_id)
        .values(run_id=str(uuid4()))
    )
    await db.commit()
    await _refused(db, issued.grant_id, issued.token)


async def test_resume_window_is_thirty_days(db: AsyncSession) -> None:
    assert RESUME_WINDOW == timedelta(days=30)
    _, _, recent = await _pair(db)
    await _lapse(db, recent.grant_id, timedelta(days=29))
    assert (await resume_grant(db, OWNER, recent.grant_id, recent.token)).token
    _, _, stale = await _pair(db)
    await _lapse(db, stale.grant_id, timedelta(days=31))
    await _refused(db, stale.grant_id, stale.token)


async def test_a_renewal_after_resume_keeps_the_consent(
    db: AsyncSession, paired: tuple[UUID, UUID, IssuedGrant]
) -> None:
    """The resumed grant is a normal grant: the running bridge renews it."""
    _, consent, issued = paired
    await _lapse(db, issued.grant_id, timedelta(hours=1))
    resumed = await resume_grant(db, OWNER, issued.grant_id, issued.token)
    renewed = await renew_grant(db, OWNER, resumed.grant_id)
    context = await resolve_integration_context(
        db, renewed.token, required_scope="tools:read"
    )
    assert context.consent_id == consent
    with pytest.raises(IntegrationAccessDenied):
        await renew_grant(db, OWNER, resumed.grant_id)
