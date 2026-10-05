"""Restricted grant authorization, using a local SQLite database."""

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, insert, text, update
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


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from src.models.bridge_device import BridgeDevice, WorkspaceBinding
    from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest

    tables: list[Any] = [
        Organization,
        User,
        Workspace,
        WorkspaceMember,
        Collection,
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
