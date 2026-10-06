"""Restricted grant authorization, using a local SQLite database."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from src.models.collection import Collection
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.integrations.context import (
    IntegrationAccessDenied,
    check_scopes,
    mint_integration_grant,
    resolve_integration_context,
    revoke_integration_grant,
)

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, WORKSPACE = (uuid4() for _ in range(4))
# PROJECT is the first live Collection of WORKSPACE; P2 is the second and P3 is
# soft-deleted (the `library` fixture). OTHER_WORKSPACE is never seeded.
P2, P3, OTHER_WORKSPACE = (uuid4() for _ in range(3))
# The live Collection `_second_workspace` seeds in a workspace of its own.
ELSEWHERE = uuid4()
SOON = datetime.now(timezone.utc) + timedelta(hours=1)
# An owned chat in WORKSPACE that belongs to no project (Thread.source_project_id
# is NULL), and a run of it: the `chat` fixture.
CHAT_THREAD, CHAT_RUN = uuid4(), uuid4()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from src.models.agent_run import AgentRun
    from src.models.bridge_device import BridgeDevice, WorkspaceBinding
    from src.models.conversation import Conversation
    from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
    from src.models.thread import Thread
    from src.models.tool_action import IntegrationToolAction

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
        IntegrationToolAction,
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
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'integration@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(USER), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                id=WORKSPACE,
                name="Project workspace",
                owner_id=USER,
                organization_id=ORG,
            )
        )
        await session.execute(
            insert(Collection).values(
                id=PROJECT, name="Project", workspace_id=WORKSPACE
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
async def issued(db: AsyncSession) -> Any:
    return await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )


@pytest.fixture
async def chat(db: AsyncSession) -> None:
    """A real chat of USER with no source project, and a run of it.

    A workspace has no single project for a chat to belong to, yet a lookup on
    `Thread.source_project_id == project_id` with no project is `IS NULL` and
    matches this very chat. Only the guard in validate_binding refuses it.
    """
    from src.models.agent_run import AgentRun
    from src.models.conversation import Conversation
    from src.models.thread import Thread

    conversation = uuid4()
    await db.execute(
        insert(Conversation).values(
            id=conversation, workspace_id=WORKSPACE, title="Chat", created_by_id=USER
        )
    )
    await db.execute(
        insert(Thread).values(
            id=CHAT_THREAD,
            conversation_id=conversation,
            created_by_id=USER,
            source_project_id=None,
            title="Unattached chat",
        )
    )
    await db.execute(
        insert(AgentRun).values(
            job_id=str(CHAT_RUN),
            organization_id=ORG,
            user_id=USER,
            thread_id=CHAT_THREAD,
            project_id=None,
            status="running",
        )
    )
    await db.commit()


async def test_revocation_is_checked_per_call(db: AsyncSession, issued: Any) -> None:
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="tools:read"
    )
    assert ctx.grant_id == issued.grant_id
    assert ctx.run_id is None and ctx.thread_id is None
    await revoke_integration_grant(db, issued.grant_id)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")


async def test_scope_cannot_be_broadened(db: AsyncSession, issued: Any) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, issued.token, required_scope="tools:write"
        )


@pytest.mark.parametrize(
    "field,value",
    [("user_id", uuid4()), ("organization_id", uuid4()), ("project_id", uuid4())],
)
async def test_foreign_identity_or_project_denied(
    db: AsyncSession, field: Any, value: Any
) -> None:
    kwargs: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )
    kwargs[field] = value
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(db, **kwargs)


@pytest.mark.parametrize("model", [Collection, Workspace, User, Organization])
async def test_deleted_ancestor_denied_each_call(
    db: AsyncSession, issued: Any, model: Any
) -> None:
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")


async def test_expiry_and_jwt_are_denied(db: AsyncSession, issued: Any) -> None:
    from src.models.integration_grant import IntegrationGrant

    await db.execute(
        update(IntegrationGrant).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    for token in [issued.token, "eyJhbGciOiJIUzI1NiJ9.payload.signature"]:
        with pytest.raises(IntegrationAccessDenied):
            await resolve_integration_context(db, token, required_scope="tools:read")


@pytest.mark.parametrize(
    "is_cli,headers", [(True, []), (False, [(b"x-nous-integration-grant", b"opaque")])]
)
async def test_interactive_auth_rejects_cli_and_grant_headers(
    is_cli: Any, headers: Any
) -> None:
    from src.api.integrations.auth import require_interactive_user
    from src.core.security import TokenData

    with pytest.raises(HTTPException) as denied:
        await require_interactive_user(
            Request({"type": "http", "headers": headers}),
            TokenData(is_cli=is_cli),
            User(id=USER),
        )
    assert denied.value.status_code == 403


@pytest.fixture
async def owner() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=USER, organization_id=ORG)


@pytest.fixture
async def pending(db: AsyncSession, owner: Any) -> Any:
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import create_request, register_device

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    return await create_request(
        db,
        owner,
        GrantRequestCreate(
            project_id=PROJECT, device_id=device.id, scopes={"tools:read"}
        ),
    )


async def test_consent_exchange_is_one_time_and_renewal_preserves_ceiling(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from src.services.integrations.context import (
        IntegrationConflict,
        decide_request,
        exchange_request,
        renew_grant,
    )

    await decide_request(db, owner, pending.id, True)
    issued = await exchange_request(db, owner, pending.id)
    with pytest.raises(IntegrationConflict):
        await exchange_request(db, owner, pending.id)
    renewed = await renew_grant(db, owner, issued.grant_id)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")
    assert (
        await resolve_integration_context(
            db, renewed.token, required_scope="tools:read"
        )
    ).project_id == PROJECT
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, renewed.token, required_scope="tools:write"
        )
    await revoke_integration_grant(db, issued.grant_id)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, renewed.token, required_scope="tools:read"
        )


async def test_device_grant_requires_one_unambiguous_active_consent(
    db: AsyncSession, owner: Any
) -> None:
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import (
        create_request,
        decide_request,
        exchange_request,
        register_device,
    )

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    for _ in range(2):
        request = await create_request(
            db,
            owner,
            GrantRequestCreate(
                project_id=PROJECT,
                device_id=device.id,
                scopes={"harness:execute"},
            ),
        )
        await decide_request(db, owner, request.id, True)
        await exchange_request(db, owner, request.id)

    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            scopes=frozenset({"harness:execute"}),
            device_id=device.id,
        )


async def test_owner_binding_and_expired_request(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from types import SimpleNamespace

    from src.models.integration_grant import IntegrationGrantRequest
    from src.services.integrations.context import (
        IntegrationConflict,
        decide_request,
        exchange_request,
        request_dto,
    )

    with pytest.raises(IntegrationAccessDenied):
        await request_dto(
            db, SimpleNamespace(id=uuid4(), organization_id=ORG), pending.id
        )
    with pytest.raises(IntegrationConflict):
        await exchange_request(db, owner, pending.id)
    await db.execute(
        update(IntegrationGrantRequest).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    assert (await request_dto(db, owner, pending.id)).status == "expired"
    with pytest.raises(IntegrationConflict):
        await decide_request(db, owner, pending.id, True)


async def test_device_revocation_and_expired_grants_cannot_renew(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from src.models.bridge_device import BridgeDevice
    from src.models.integration_grant import IntegrationGrant
    from src.services.integrations.context import (
        decide_request,
        exchange_request,
        renew_grant,
    )

    await decide_request(db, owner, pending.id, True)
    issued = await exchange_request(db, owner, pending.id)
    await db.execute(update(BridgeDevice).values(revoked_at=datetime.now(timezone.utc)))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")
    with pytest.raises(IntegrationAccessDenied):
        await renew_grant(db, owner, issued.grant_id)
    await db.execute(update(BridgeDevice).values(revoked_at=None))
    await db.execute(
        update(IntegrationGrant).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await renew_grant(db, owner, issued.grant_id)


async def test_public_project_denied_but_explicit_cross_org_member_allowed(
    db: AsyncSession,
) -> None:
    from src.models.workspace import WorkspaceRole

    owning_org = uuid4()
    await db.execute(
        insert(Organization).values(
            id=owning_org, name="Project owning org", storage_limit_bytes=1000000
        )
    )
    await db.execute(
        update(Workspace).values(
            owner_id=uuid4(), organization_id=owning_org, is_public=True
        )
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            scopes=frozenset({"tools:read"}),
        )
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    issued = await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )
    assert (
        await resolve_integration_context(db, issued.token, required_scope="tools:read")
    ).organization_id == ORG
    await db.execute(update(WorkspaceMember).values(is_deleted=True))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, issued.token, required_scope="tools:read")


async def test_workspace_listing_rechecks_permissions_and_foreign_devices(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from src.schemas.integration_context import WorkspaceBindingCreate
    from src.services.integrations.context import (
        bind_workspace,
        list_workspaces,
        owned_device,
    )

    with pytest.raises(IntegrationAccessDenied):
        await owned_device(db, pending.device_id, uuid4(), ORG)
    binding = WorkspaceBindingCreate(
        workspace_id=uuid4(), project_id=PROJECT, label="Analysis"
    )
    await bind_workspace(db, owner, pending.device_id, binding)
    assert len(await list_workspaces(db, owner, pending.device_id)) == 1
    await db.execute(update(Collection).values(is_deleted=True))
    await db.commit()
    assert await list_workspaces(db, owner, pending.device_id) == []


async def test_http_cli_cannot_approve_or_change_displayed_scope(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.integrations import router
    from src.core.database import get_db
    from src.core.dependencies import get_current_user
    from src.core.security import TokenData, get_current_user_token

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: owner
    token = TokenData(is_cli=True, user_id=str(USER), organization_id=str(ORG))
    app.dependency_overrides[get_current_user_token] = lambda: token
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = f"/api/v1/integrations/grant-requests/{pending.id}"
        response = await client.post(url + "/decision", json={"approved": True})
        assert response.status_code == 403
        token.is_cli = False
        response = await client.post(
            url + "/decision", json={"approved": True, "scopes": ["tools:write"]}
        )
        assert response.status_code == 422
        assert (await client.get(url)).json()["status"] == "pending"
        response = await client.post(
            url + "/decision",
            json={"approved": True},
            headers={"X-NOUS-Integration-Grant": "opaque"},
        )
        assert response.status_code == 403
        assert (
            await client.post(url + "/decision", json={"approved": True})
        ).status_code == 200
        assert (await client.post(url + "/exchange")).status_code == 403
        token.is_cli = True
        response = await client.post(url + "/exchange")
        assert response.status_code == 200
        assert set(response.json()) == {"token", "grant_id"}
        assert (await client.post(url + "/exchange")).status_code == 409
        assert "token" not in (await client.get(url)).json()


async def test_dual_credentials_bind_actor_and_org(
    db: AsyncSession, issued: Any
) -> None:
    from types import SimpleNamespace

    from src.api.integrations.auth import integration_context
    from src.core.security import TokenData

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-nous-integration-grant", issued.token.encode())],
        }
    )
    token = TokenData(is_cli=True, user_id=str(USER), organization_id=str(ORG))
    assert (
        await integration_context(
            request,
            "tools:read",
            db,
            token,
            User(id=USER, organization_id=ORG),
        )
    ).grant_id == issued.grant_id
    for user_id, org_id in [(uuid4(), ORG), (USER, uuid4())]:
        token = TokenData(
            is_cli=True, user_id=str(user_id), organization_id=str(org_id)
        )
        with pytest.raises(HTTPException):
            await integration_context(
                request,
                "tools:read",
                db,
                token,
                User(id=user_id, organization_id=org_id),
            )


async def test_device_bound_mint_requires_consumed_browser_consent(
    db: AsyncSession, owner: Any, pending: Any
) -> None:
    from src.services.integrations.context import decide_request, exchange_request

    kwargs: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        device_id=pending.device_id,
        scopes=frozenset({"tools:read"}),
    )
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(db, **kwargs)
    await decide_request(db, owner, pending.id, True)
    await exchange_request(db, owner, pending.id)
    issued = await mint_integration_grant(db, **kwargs)
    assert (
        await resolve_integration_context(db, issued.token, required_scope="tools:read")
    ).project_id == PROJECT


@pytest.mark.parametrize("owning_org_state", ["inactive", "deleted", "missing"])
async def test_cross_org_project_requires_live_owning_organization(
    db: AsyncSession, owner: Any, pending: Any, owning_org_state: str
) -> None:
    from src.models.workspace import WorkspaceRole
    from src.services.integrations.context import (
        decide_request,
        exchange_request,
        renew_grant,
    )

    owning_org = uuid4()
    await db.execute(
        insert(Organization).values(
            id=owning_org, name="Owning organization", storage_limit_bytes=1000000
        )
    )
    await db.execute(
        update(Workspace).values(organization_id=owning_org, owner_id=uuid4())
    )
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    # Both organizations are persisted and active; an explicitly invited member
    # retains the actor's org in context and may mint, resolve and renew.
    unbound = await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )
    assert (
        await resolve_integration_context(
            db, unbound.token, required_scope="tools:read"
        )
    ).organization_id == ORG
    await decide_request(db, owner, pending.id, True)
    bound = await exchange_request(db, owner, pending.id)
    bound = await renew_grant(db, owner, bound.grant_id)
    assert (
        await resolve_integration_context(db, bound.token, required_scope="tools:read")
    ).organization_id == ORG

    if owning_org_state == "missing":
        await db.execute(delete(Organization).where(Organization.id == owning_org))
    else:
        changes = (
            {"is_active": False}
            if owning_org_state == "inactive"
            else {"is_deleted": True}
        )
        await db.execute(
            update(Organization).where(Organization.id == owning_org).values(**changes)
        )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            scopes=frozenset({"tools:read"}),
        )
    for token in (unbound.token, bound.token):
        with pytest.raises(IntegrationAccessDenied):
            await resolve_integration_context(db, token, required_scope="tools:read")
    with pytest.raises(IntegrationAccessDenied):
        await renew_grant(db, owner, bound.grant_id)


async def test_http_owner_revocation_supports_cli_dual_credentials_and_browser(
    db: AsyncSession, owner: Any, pending: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from src.services.integrations import context as service

    # `nous-harness disconnect` must also end the CLI login it used (CLI JWTs
    # carry no device id), and only after the grant revocation commits.
    cutoffs: list[tuple[str, bool]] = []

    async def record(user_id: str) -> None:
        cutoffs.append((user_id, db.in_transaction()))

    monkeypatch.setattr(service, "revoke_user_cli_tokens", record)
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.integrations import router
    from src.core.database import get_db
    from src.core.dependencies import get_current_user
    from src.core.security import TokenData, get_current_user_token
    from src.services.integrations.context import decide_request, exchange_request

    await decide_request(db, owner, pending.id, True)
    issued = await exchange_request(db, owner, pending.id)
    browser_grant = await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT,
        scopes=frozenset({"tools:read"}),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    actor = owner
    app.dependency_overrides[get_current_user] = lambda: actor
    token = TokenData(is_cli=True, user_id=str(USER), organization_id=str(ORG))
    app.dependency_overrides[get_current_user_token] = lambda: token
    headers = {
        "Authorization": "Bearer verified-cli-jwt",
        "X-NOUS-Integration-Grant": issued.token,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = f"/api/v1/integrations/grants/{issued.grant_id}"
        actor = SimpleNamespace(id=uuid4(), organization_id=ORG)
        token.user_id = str(actor.id)
        assert (await client.delete(url, headers=headers)).status_code == 403
        actor = owner
        token.user_id = str(USER)
        assert (
            await client.delete(
                url,
                headers={**headers, "X-NOUS-Integration-Grant": browser_grant.token},
            )
        ).status_code == 403
        assert cutoffs == []  # refused deletes revoke nothing
        assert (await client.delete(url, headers=headers)).status_code == 204
        assert cutoffs == [(str(USER), False)]
        with pytest.raises(IntegrationAccessDenied):
            await resolve_integration_context(
                db, issued.token, required_scope="tools:read"
            )
        browser_url = f"/api/v1/integrations/grants/{browser_grant.grant_id}"
        assert (
            await client.delete(
                browser_url, headers={"Authorization": "Bearer verified-cli-jwt"}
            )
        ).status_code == 403
        token.is_cli = False
        assert (await client.delete(browser_url, headers=headers)).status_code == 403
        actor = SimpleNamespace(id=uuid4(), organization_id=ORG)
        token.user_id = str(actor.id)
        assert (await client.delete(browser_url)).status_code == 403
        actor = owner
        token.user_id = str(USER)
        assert (await client.delete(browser_url)).status_code == 204
        assert len(cutoffs) == 1  # a browser grant delete leaves CLI logins alone
        with pytest.raises(IntegrationAccessDenied):
            await resolve_integration_context(
                db, browser_grant.token, required_scope="tools:read"
            )


@pytest.mark.parametrize(
    "scope", ["artifacts:read", "artifacts:edit", "artifacts:share"]
)
def test_unenforced_artifact_scopes_cannot_be_requested(scope: str) -> None:
    # No route enforces these yet; the slice that enforces one re-adds it.
    with pytest.raises(IntegrationAccessDenied):
        check_scopes({"harness:execute", scope})


def test_library_scopes_are_standard() -> None:
    from src.schemas.integration_context import STANDARD_SCOPES

    assert {"library:read", "library:write"} <= STANDARD_SCOPES


async def test_grant_accepts_workspace_binding_without_project(
    db: AsyncSession,
) -> None:
    from src.models.integration_grant import IntegrationGrant

    grant = IntegrationGrant(
        id=uuid4(),
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        workspace_id=WORKSPACE,
        scopes=["library:read"],
        token_hash="x" * 64,
        expires_at=SOON,
        consented_at=datetime.now(timezone.utc),
    )
    db.add(grant)
    await db.commit()
    db.expunge_all()  # expire_on_commit is off here; force a real read-back
    stored = await db.get(IntegrationGrant, grant.id)
    assert stored is not None
    assert stored.project_id is None and stored.workspace_id == WORKSPACE


# The minimum NOT NULL columns of each table that carries a grant binding.
_BINDING_ROWS: dict[str, dict[str, Any]] = {
    "integration_grant_requests": {
        "user_id": USER,
        "organization_id": ORG,
        "device_id": uuid4(),
        "scopes": ["library:read"],
        "status": "pending",
        "expires_at": SOON,
    },
    "integration_grants": {
        "user_id": USER,
        "organization_id": ORG,
        "scopes": ["library:read"],
        "token_hash": "y" * 64,
        "expires_at": SOON,
        "consented_at": SOON,
    },
    "integration_tool_actions": {
        "organization_id": ORG,
        "user_id": USER,
        "invocation_id": uuid4(),
        "tool_name": "create_note",
        "arguments": {},
        "argument_hash": "z" * 64,
        "state": "awaiting_approval",
    },
}


_BINDINGS: dict[str, dict[str, Any]] = {
    "project": {"project_id": PROJECT},
    "workspace": {"workspace_id": WORKSPACE},
    "both": {"project_id": PROJECT, "workspace_id": WORKSPACE},
    "neither": {},
}
# The shapes each table must refuse, and the constraint that refuses them.
# Requests and grants bind exactly one of a Collection or a workspace. On an
# action project_id is the target Collection and workspace_id the grant's
# binding, so a workspace-grant action aimed at a Collection sets both.
_REFUSED: dict[str, dict[str, str]] = {
    "integration_grant_requests": {"both": "one_binding", "neither": "one_binding"},
    "integration_grants": {"both": "one_binding", "neither": "one_binding"},
    "integration_tool_actions": {"neither": "some_binding"},
}


@pytest.mark.parametrize("table_name", sorted(_BINDING_ROWS))
@pytest.mark.parametrize("binding", sorted(_BINDINGS))
async def test_database_enforces_the_binding_rule(
    db: AsyncSession, table_name: str, binding: str
) -> None:
    from src.models.base import Base

    row = insert(Base.metadata.tables[table_name]).values(
        **_BINDING_ROWS[table_name], **_BINDINGS[binding]
    )
    constraint = _REFUSED[table_name].get(binding)
    if constraint is None:
        await db.execute(row)
        await db.commit()
        return
    with pytest.raises(IntegrityError, match=f"ck_{table_name}_{constraint}"):
        await db.execute(row)


# --- Workspace-scoped grants (Plan 07, slice 1) -----------------------------


@pytest.fixture
async def library(db: AsyncSession) -> None:
    """WORKSPACE holds the live PROJECT and P2, plus the soft-deleted P3."""
    await db.execute(
        insert(Collection).values(
            [
                dict(id=P2, name="Second", workspace_id=WORKSPACE, is_deleted=False),
                dict(id=P3, name="Retired", workspace_id=WORKSPACE, is_deleted=True),
            ]
        )
    )
    await db.commit()


async def _mint(
    db: AsyncSession,
    *,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: set[str],
) -> str:
    """Store a grant row with a known token and return the bearer token."""
    from src.models.integration_grant import IntegrationGrant

    token = "nous_ig_" + token_urlsafe(32)
    db.add(
        IntegrationGrant(
            id=uuid4(),
            user_id=USER,
            organization_id=ORG,
            project_id=project_id,
            workspace_id=workspace_id,
            scopes=sorted(scopes),
            token_hash=sha256(token.encode()).hexdigest(),
            expires_at=SOON,
            consented_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return token


async def _second_workspace(db: AsyncSession) -> UUID:
    """Another workspace the same user owns, with a live Collection of its own.

    The user can reach it, so only the grant's binding keeps it out of a scope.
    """
    second = uuid4()
    await db.execute(
        insert(Workspace).values(
            id=second, name="Second workspace", owner_id=USER, organization_id=ORG
        )
    )
    await db.execute(
        insert(Collection).values(id=ELSEWHERE, name="Elsewhere", workspace_id=second)
    )
    await db.commit()
    return second


LIBRARY_READ = {"tools:read", "library:read"}


async def test_workspace_grant_resolves_context_with_workspace(
    db: AsyncSession,
) -> None:
    token = await _mint(db, workspace_id=WORKSPACE, scopes=LIBRARY_READ)
    ctx = await resolve_integration_context(db, token, required_scope="library:read")
    assert ctx.project_id is None and ctx.workspace_id == WORKSPACE


async def test_resolved_context_carries_the_scopes_of_its_grant(
    db: AsyncSession, issued: Any
) -> None:
    # The read gateway authorizes each tool against these, so they must be
    # exactly what the stored grant holds: no more, whichever scope was asked.
    workspace_token = await _mint(db, workspace_id=WORKSPACE, scopes=LIBRARY_READ)
    workspace_ctx = await resolve_integration_context(
        db, workspace_token, required_scope="tools:read"
    )
    assert workspace_ctx.scopes == frozenset(LIBRARY_READ)
    project_ctx = await resolve_integration_context(
        db, issued.token, required_scope="tools:read"
    )
    assert project_ctx.scopes == frozenset({"tools:read"})


def test_a_context_built_without_a_grant_holds_no_scopes() -> None:
    from src.schemas.integration_context import IntegrationContext

    # Fail closed: a context nobody resolved from a grant authorizes no tool.
    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    assert ctx.scopes == frozenset()


async def test_authorized_scope_lists_live_collections_of_workspace(
    db: AsyncSession, library: None
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope

    await _second_workspace(db)  # a live Collection the user owns, elsewhere
    ctx = IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        workspace_id=WORKSPACE,
        grant_id=uuid4(),
    )
    # Only this workspace's live Collections: P3 is deleted, ELSEWHERE is not here.
    assert await authorized_scope(db, ctx) == {PROJECT, P2}


async def test_authorized_scope_of_a_project_grant_is_just_that_project(
    db: AsyncSession, library: None
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope

    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    assert await authorized_scope(db, ctx) == {PROJECT}


@pytest.mark.parametrize("binding", ["project", "workspace"])
async def test_authorized_scope_filter_selects_the_scope_by_its_binding_not_its_ids(
    db: AsyncSession, library: None, binding: str
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope_filter

    await _second_workspace(db)  # a live Collection the user owns, elsewhere
    bound = PROJECT if binding == "project" else WORKSPACE
    ctx = IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT if binding == "project" else None,
        workspace_id=WORKSPACE if binding == "workspace" else None,
        grant_id=uuid4(),
    )
    scope = await authorized_scope_filter(db, ctx)
    # A condition that binds the grant's one id, however many Collections the
    # scope holds. An IN list of their ids would pass SQLite and fail
    # PostgreSQL once a workspace holds more than 32,767 of them.
    assert list(scope.compile().params.values()) == [bound]
    # It leaves soft-deletion to the statement it joins, so ask for live ones.
    live = select(Collection.id).where(scope, Collection.is_deleted.is_(False))
    expected = {PROJECT} if binding == "project" else {PROJECT, P2}
    assert {UUID(str(found)) for found in await db.scalars(live)} == expected


# What ends a project grant's access. authorized_scope re-checks it on every
# call, so each change must turn the next call into a denial.
LOST_ACCESS: dict[str, Any] = {
    "collection-deleted": update(Collection)
    .where(Collection.id == PROJECT)
    .values(is_deleted=True),
    "workspace-deleted": update(Workspace).values(is_deleted=True),
    "owner-changed-without-membership": update(Workspace).values(owner_id=uuid4()),
    "user-inactive": update(User).values(is_active=False),
    "user-deleted": update(User).values(is_deleted=True),
    "organization-inactive": update(Organization).values(is_active=False),
}


@pytest.mark.parametrize("change", sorted(LOST_ACCESS))
async def test_authorized_scope_of_a_project_grant_is_denied_once_access_is_lost(
    db: AsyncSession, library: None, change: str
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope

    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    assert await authorized_scope(db, ctx) == {PROJECT}  # allowed until it changes
    await db.execute(LOST_ACCESS[change])
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await authorized_scope(db, ctx)


@pytest.mark.parametrize("change", sorted(LOST_ACCESS))
async def test_authorized_scope_filter_is_denied_once_access_is_lost(
    db: AsyncSession, library: None, change: str
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope_filter

    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    await authorized_scope_filter(db, ctx)  # allowed until it changes
    await db.execute(LOST_ACCESS[change])
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await authorized_scope_filter(db, ctx)


async def test_authorized_scope_of_a_project_grant_follows_membership(
    db: AsyncSession, library: None
) -> None:
    from src.models.workspace import WorkspaceRole
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations.context import authorized_scope

    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=uuid4()
    )
    # The owner leaves; a live membership alone keeps the project reachable...
    await db.execute(update(Workspace).values(owner_id=uuid4()))
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    assert await authorized_scope(db, ctx) == {PROJECT}
    # ...and revoking it ends the access.
    await db.execute(update(WorkspaceMember).values(is_deleted=True))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await authorized_scope(db, ctx)


@pytest.mark.parametrize("getter", ["authorized_scope", "authorized_scope_filter"])
@pytest.mark.parametrize("case", ["unknown", "public_non_member", "deleted"])
async def test_authorized_scope_refuses_foreign_workspace(
    db: AsyncSession, library: None, case: str, getter: str
) -> None:
    from src.schemas.integration_context import IntegrationContext
    from src.services.integrations import context as integration_context

    workspace = OTHER_WORKSPACE
    if case == "public_non_member":
        await db.execute(
            insert(Workspace).values(
                id=OTHER_WORKSPACE,
                name="Theirs",
                owner_id=uuid4(),
                organization_id=ORG,
                is_public=True,
            )
        )
    elif case == "deleted":
        workspace = WORKSPACE
        await db.execute(update(Workspace).values(is_deleted=True))
    await db.commit()
    ctx = IntegrationContext(
        user_id=USER, organization_id=ORG, workspace_id=workspace, grant_id=uuid4()
    )
    with pytest.raises(IntegrationAccessDenied):
        await getattr(integration_context, getter)(db, ctx)


@pytest.mark.parametrize("model", [Workspace, User, Organization])
async def test_deleted_ancestor_denies_a_workspace_grant_each_call(
    db: AsyncSession, model: Any
) -> None:
    token = await _mint(db, workspace_id=WORKSPACE, scopes=LIBRARY_READ)
    await resolve_integration_context(db, token, required_scope="library:read")
    await db.execute(update(model).values(is_deleted=True))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, token, required_scope="library:read")


@pytest.mark.parametrize("owning_org_state", ["inactive", "deleted", "missing"])
async def test_workspace_grant_follows_membership_not_organization(
    db: AsyncSession, owning_org_state: str
) -> None:
    from src.models.workspace import WorkspaceRole

    # Same rule as a project grant: an explicitly invited member of a workspace
    # owned by another live organization may hold a grant, a public workspace
    # alone is not access, and the owning organization must stay live.
    owning_org = uuid4()
    await db.execute(
        insert(Organization).values(
            id=owning_org, name="Owning organization", storage_limit_bytes=1000000
        )
    )
    await db.execute(
        update(Workspace).values(
            owner_id=uuid4(), organization_id=owning_org, is_public=True
        )
    )
    await db.commit()
    kwargs: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        workspace_id=WORKSPACE,
        scopes=frozenset(LIBRARY_READ),
    )
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(db, **kwargs)
    await db.execute(
        insert(WorkspaceMember).values(
            workspace_id=WORKSPACE, user_id=USER, role=WorkspaceRole.VIEWER
        )
    )
    await db.commit()
    issued = await mint_integration_grant(db, **kwargs)
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="library:read"
    )
    assert (ctx.organization_id, ctx.workspace_id) == (ORG, WORKSPACE)

    if owning_org_state == "missing":
        await db.execute(delete(Organization).where(Organization.id == owning_org))
    else:
        changes = (
            {"is_active": False}
            if owning_org_state == "inactive"
            else {"is_deleted": True}
        )
        await db.execute(
            update(Organization).where(Organization.id == owning_org).values(**changes)
        )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, issued.token, required_scope="library:read"
        )
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(db, **kwargs)


@pytest.mark.parametrize(
    "bindings", [{}, {"project_id": PROJECT, "workspace_id": WORKSPACE}]
)
def test_context_and_request_bind_exactly_one_of_project_or_workspace(
    bindings: dict[str, Any],
) -> None:
    from pydantic import ValidationError

    from src.schemas.integration_context import GrantRequestCreate, IntegrationContext

    with pytest.raises(ValidationError):
        IntegrationContext(
            user_id=USER, organization_id=ORG, grant_id=uuid4(), **bindings
        )
    with pytest.raises(ValidationError):
        GrantRequestCreate(device_id=uuid4(), scopes={"tools:read"}, **bindings)


@pytest.mark.parametrize(
    "bindings,extra",
    [
        ({"project_id": None, "workspace_id": None}, {}),
        ({"project_id": PROJECT, "workspace_id": WORKSPACE}, {}),
        ({"project_id": None, "workspace_id": WORKSPACE}, {"thread_id": CHAT_THREAD}),
        ({"project_id": None, "workspace_id": WORKSPACE}, {"run_id": CHAT_RUN}),
        (
            {"project_id": None, "workspace_id": WORKSPACE},
            {"thread_id": CHAT_THREAD, "run_id": CHAT_RUN},
        ),
    ],
    ids=["neither", "both", "workspace-chat", "workspace-run", "workspace-chat-run"],
)
async def test_validate_binding_needs_one_binding_and_no_chat_on_a_workspace(
    db: AsyncSession, chat: None, bindings: dict[str, Any], extra: dict[str, Any]
) -> None:
    from src.services.integrations.context import validate_binding

    # The workspace itself is a valid binding; the chat and run are real and
    # owned, so nothing but the rule that a workspace carries no chat refuses it.
    await validate_binding(
        db, user_id=USER, organization_id=ORG, project_id=None, workspace_id=WORKSPACE
    )
    with pytest.raises(IntegrationAccessDenied):
        await validate_binding(
            db, user_id=USER, organization_id=ORG, **bindings, **extra
        )


@pytest.mark.parametrize(
    "scopes",
    [
        {"library:read"},
        {"harness:execute", "library:read"},
        {"tools:read", "library:write"},
        {"tools:read", "tools:write", "library:write"},  # no library:read
        {"tools:read", "library:read", "library:write"},  # no tools:write
    ],
)
def test_library_scopes_need_the_gateway_scopes_they_imply(scopes: set[str]) -> None:
    with pytest.raises(IntegrationAccessDenied):
        check_scopes(scopes)


@pytest.mark.parametrize(
    "scopes",
    [
        {"library:read"},
        {"tools:read", "library:write"},
        {"tools:read", "tools:write", "library:write"},
    ],
    ids=["read-alone", "write-without-read-or-write-gateway", "write-without-read"],
)
@pytest.mark.parametrize("binding", ["project", "workspace"])
async def test_incomplete_library_scopes_are_refused_wherever_scopes_are_checked(
    db: AsyncSession, owner: Any, scopes: set[str], binding: str
) -> None:
    from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import create_request, register_device

    bound: dict[str, Any] = (
        {"project_id": PROJECT} if binding == "project" else {"workspace_id": WORKSPACE}
    )
    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    with pytest.raises(IntegrationAccessDenied):
        await create_request(
            db, owner, GrantRequestCreate(device_id=device.id, scopes=scopes, **bound)
        )
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            scopes=frozenset(scopes),
            **bound,
        )
    # Refused before anything is stored, not merely hidden on read-back.
    assert await db.scalar(select(func.count(IntegrationGrantRequest.id))) == 0
    assert await db.scalar(select(func.count(IntegrationGrant.id))) == 0
    # A row that slipped in some other way is refused when it is resolved.
    token = await _mint(db, scopes=scopes, **bound)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, token, required_scope=sorted(scopes)[0])


@pytest.mark.parametrize(
    "scopes",
    [
        {"tools:read", "library:read"},
        {"tools:read", "tools:write", "library:read", "library:write"},
        {"harness:execute", "tools:read", "tools:write", "library:read"},
    ],
)
def test_complete_library_scope_sets_are_accepted(scopes: set[str]) -> None:
    check_scopes(scopes)


@pytest.mark.parametrize(
    "scope",
    ["harness:execute", "artifacts:publish", "handoff:read", "handoff:write"],
)
async def test_workspace_grants_are_mcp_only(
    db: AsyncSession, owner: Any, scope: str
) -> None:
    from src.models.integration_grant import IntegrationGrantRequest
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import create_request, register_device

    scopes = LIBRARY_READ | {scope}
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            workspace_id=WORKSPACE,
            scopes=frozenset(scopes),
        )
    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    with pytest.raises(IntegrationAccessDenied):
        await create_request(
            db,
            owner,
            GrantRequestCreate(
                workspace_id=WORKSPACE, device_id=device.id, scopes=scopes
            ),
        )
    # Refused before anything is stored, not merely hidden on read-back.
    assert await db.scalar(select(func.count(IntegrationGrantRequest.id))) == 0
    # A row that slipped in some other way is refused when it is resolved.
    token = await _mint(db, workspace_id=WORKSPACE, scopes=scopes)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(db, token, required_scope=scope)


async def test_workspace_consent_binds_the_grant_and_survives_renewal(
    db: AsyncSession, owner: Any
) -> None:
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import (
        create_request,
        decide_request,
        exchange_request,
        register_device,
        renew_grant,
    )

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    shown = await create_request(
        db,
        owner,
        GrantRequestCreate(
            workspace_id=WORKSPACE, device_id=device.id, scopes=LIBRARY_READ
        ),
    )
    assert (shown.project_id, shown.project_label) == (None, None)
    assert (shown.workspace_id, shown.workspace_label) == (
        WORKSPACE,
        "Project workspace",
    )
    await decide_request(db, owner, shown.id, True)
    issued = await exchange_request(db, owner, shown.id)
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="library:read"
    )
    assert (ctx.project_id, ctx.workspace_id) == (None, WORKSPACE)

    renewed = await renew_grant(db, owner, issued.grant_id)
    ctx = await resolve_integration_context(
        db, renewed.token, required_scope="library:read"
    )
    assert (ctx.project_id, ctx.workspace_id) == (None, WORKSPACE)
    # The consent's scope ceiling still holds across the renewal.
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, renewed.token, required_scope="tools:write"
        )


async def test_request_for_a_foreign_workspace_is_refused_before_storing(
    db: AsyncSession, owner: Any
) -> None:
    from src.models.integration_grant import IntegrationGrantRequest
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import create_request, register_device

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    with pytest.raises(IntegrationAccessDenied):
        await create_request(
            db,
            owner,
            GrantRequestCreate(
                workspace_id=OTHER_WORKSPACE, device_id=device.id, scopes=LIBRARY_READ
            ),
        )
    assert await db.scalar(select(func.count(IntegrationGrantRequest.id))) == 0


async def test_workspace_request_cannot_carry_a_chat(
    db: AsyncSession, owner: Any, chat: None
) -> None:
    from src.models.integration_grant import IntegrationGrantRequest
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import create_request, register_device

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    with pytest.raises(IntegrationAccessDenied):
        await create_request(
            db,
            owner,
            GrantRequestCreate(
                workspace_id=WORKSPACE,
                device_id=device.id,
                scopes=LIBRARY_READ,
                thread_id=CHAT_THREAD,
            ),
        )
    # Refused before anything is stored, not merely hidden on read-back.
    assert await db.scalar(select(func.count(IntegrationGrantRequest.id))) == 0


@pytest.mark.parametrize(
    "extra",
    [{"thread_id": CHAT_THREAD}, {"thread_id": CHAT_THREAD, "run_id": CHAT_RUN}],
    ids=["chat", "chat-and-run"],
)
async def test_workspace_grant_cannot_be_minted_for_a_chat(
    db: AsyncSession, chat: None, extra: dict[str, Any]
) -> None:
    from src.models.integration_grant import IntegrationGrant

    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            workspace_id=WORKSPACE,
            scopes=frozenset(LIBRARY_READ),
            **extra,
        )
    assert await db.scalar(select(func.count(IntegrationGrant.id))) == 0


async def _consent(
    db: AsyncSession, owner: Any, device_id: UUID, workspace_id: UUID
) -> None:
    from src.schemas.integration_context import GrantRequestCreate
    from src.services.integrations.context import (
        create_request,
        decide_request,
        exchange_request,
    )

    request = await create_request(
        db,
        owner,
        GrantRequestCreate(
            workspace_id=workspace_id, device_id=device_id, scopes=LIBRARY_READ
        ),
    )
    await decide_request(db, owner, request.id, True)
    await exchange_request(db, owner, request.id)


async def test_device_mint_needs_a_consent_for_the_same_workspace(
    db: AsyncSession, owner: Any
) -> None:
    from src.schemas.integration_context import DeviceCreate
    from src.services.integrations.context import register_device

    other = await _second_workspace(db)
    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    await _consent(db, owner, device.id, WORKSPACE)
    kwargs: dict[str, Any] = dict(
        user_id=USER,
        organization_id=ORG,
        device_id=device.id,
        scopes=frozenset(LIBRARY_READ),
    )
    # A consent for one workspace is not authority over another workspace, nor
    # over a single project, even one inside the consented workspace.
    for denied in ({"workspace_id": other}, {"project_id": PROJECT}):
        with pytest.raises(IntegrationAccessDenied):
            await mint_integration_grant(db, **kwargs, **denied)
    issued = await mint_integration_grant(db, **kwargs, workspace_id=WORKSPACE)
    ctx = await resolve_integration_context(
        db, issued.token, required_scope="library:read"
    )
    assert (ctx.project_id, ctx.workspace_id) == (None, WORKSPACE)


async def test_workspace_grant_consent_is_rechecked_against_its_workspace(
    db: AsyncSession, owner: Any
) -> None:
    from src.models.integration_grant import IntegrationGrant
    from src.schemas.integration_context import DeviceCreate, GrantRequestCreate
    from src.services.integrations.context import (
        create_request,
        decide_request,
        exchange_request,
        register_device,
    )

    other = await _second_workspace(db)
    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    request = await create_request(
        db,
        owner,
        GrantRequestCreate(
            workspace_id=WORKSPACE, device_id=device.id, scopes=LIBRARY_READ
        ),
    )
    await decide_request(db, owner, request.id, True)
    issued = await exchange_request(db, owner, request.id)
    await resolve_integration_context(db, issued.token, required_scope="library:read")
    # Re-pointing the grant at another workspace the user owns must not borrow
    # the consent that was given for the first one.
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == issued.grant_id)
        .values(workspace_id=other)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await resolve_integration_context(
            db, issued.token, required_scope="library:read"
        )


async def test_http_grant_request_takes_a_workspace_or_a_project_not_both(
    db: AsyncSession, owner: Any
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.integrations import router
    from src.core.database import get_db
    from src.core.dependencies import get_current_user
    from src.core.security import TokenData, get_current_user_token
    from src.schemas.integration_context import DeviceCreate
    from src.services.integrations.context import register_device

    device = await register_device(db, owner, DeviceCreate(label="Laptop"))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: owner
    token = TokenData(is_cli=True, user_id=str(USER), organization_id=str(ORG))
    app.dependency_overrides[get_current_user_token] = lambda: token
    url = "/api/v1/integrations/grant-requests"
    body: dict[str, Any] = {"device_id": str(device.id), "scopes": sorted(LIBRARY_READ)}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        shown = await client.post(url, json={**body, "workspace_id": str(WORKSPACE)})
        assert shown.status_code == 200
        assert shown.json()["workspace_id"] == str(WORKSPACE)
        assert shown.json()["workspace_label"] == "Project workspace"
        assert shown.json()["project_id"] is None
        assert shown.json()["project_label"] is None
        for bindings in (
            {},
            {"workspace_id": str(WORKSPACE), "project_id": str(PROJECT)},
        ):
            assert (
                await client.post(url, json={**body, **bindings})
            ).status_code == 422
        refused = await client.post(url, json={**body, "workspace_id": str(uuid4())})
        assert refused.status_code == 403
        assert refused.json() == {"detail": "Integration access denied"}
