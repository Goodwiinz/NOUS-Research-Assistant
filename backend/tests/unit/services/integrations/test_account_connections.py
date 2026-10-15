"""Account connections: schemas, grants, approvals, devices and folders.

The database tests run on SQLite always and on PostgreSQL when
ORCHESTRATION_TEST_DATABASE_URL is set (docs/testing/harness-live-proof.md Gate
0). Without the URL the PostgreSQL half is skipped, which is NOT RUN, never PASS.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.base import Base
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.integration_connections import ConnectionConsent
from src.schemas.integration_context import (
    BindingKind,
    GrantRequestCreate,
    IntegrationContext,
    IssuedGrant,
    WorkspaceBindingCreate,
)
from src.schemas.tool_actions import ActionActor
from src.services.integrations import context as service

pytestmark = pytest.mark.unit
SCHEMA_IDS = {
    name: uuid4() for name in ("user", "org", "project", "workspace", "device")
}


def _ctx(**values: Any) -> IntegrationContext:
    return IntegrationContext(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        grant_id=uuid4(),
        **values,
    )


def test_context_kind_is_derived_for_project_and_workspace_never_for_account() -> None:
    assert _ctx(project_id=SCHEMA_IDS["project"]).binding_kind == "project"
    assert _ctx(workspace_id=SCHEMA_IDS["workspace"]).binding_kind == "workspace"
    assert _ctx(binding_kind="account").binding_kind == "account"
    # Both NULL means "bound to nothing" unless the kind says account.
    with pytest.raises(
        ValidationError, match="exactly one of project_id or workspace_id"
    ):
        _ctx()
    with pytest.raises(ValidationError):
        _ctx(project_id=SCHEMA_IDS["project"], workspace_id=SCHEMA_IDS["workspace"])
    for wrong in (
        {"binding_kind": "account", "project_id": SCHEMA_IDS["project"]},
        {"binding_kind": "account", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "project", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "workspace", "project_id": SCHEMA_IDS["project"]},
    ):
        with pytest.raises(ValidationError, match="binding does not match"):
            _ctx(**wrong)


def test_grant_request_names_exactly_one_binding_and_no_chat_for_an_account() -> None:
    device, scopes = SCHEMA_IDS["device"], {"tools:read"}
    account = GrantRequestCreate(account=True, device_id=device, scopes=scopes)
    assert account.binding_kind == "account"
    assert (
        GrantRequestCreate(
            project_id=SCHEMA_IDS["project"], device_id=device, scopes=scopes
        ).binding_kind
        == "project"
    )
    assert (
        GrantRequestCreate(
            workspace_id=SCHEMA_IDS["workspace"], device_id=device, scopes=scopes
        ).binding_kind
        == "workspace"
    )
    with pytest.raises(
        ValidationError, match="exactly one of project_id or workspace_id"
    ):
        GrantRequestCreate(device_id=device, scopes=scopes)
    for extra in (
        {"project_id": SCHEMA_IDS["project"]},
        {"workspace_id": SCHEMA_IDS["workspace"]},
    ):
        with pytest.raises(ValidationError, match="account excludes"):
            GrantRequestCreate(account=True, device_id=device, scopes=scopes, **extra)
    with pytest.raises(ValidationError, match="cannot be bound to a chat"):
        GrantRequestCreate(
            account=True, device_id=device, scopes=scopes, thread_id=uuid4()
        )


def test_a_connection_consent_may_be_the_whole_account() -> None:
    consent = ConnectionConsent(
        request_id=uuid4(),
        kind="account",
        project_id=None,
        project_label=None,
        workspace_id=None,
        workspace_label=None,
        thread_id=None,
        thread_label=None,
        scopes=["harness:execute", "tools:read"],
        status="consumed",
    )
    assert consent.kind == "account"


def test_action_actor_may_name_its_kind() -> None:
    actor = ActionActor(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        project_id=None,
        binding_kind="account",
    )
    assert actor.binding_kind == "account"
    legacy = ActionActor(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        project_id=uuid4(),
    )
    assert legacy.binding_kind is None  # derived where it is used (tool_actions)


# --- database: one account, a device, and what lies outside the account -----

ORG, OTHER_ORG = uuid4(), uuid4()
USER, COLLEAGUE, OUTSIDER = uuid4(), uuid4(), uuid4()
OWNED, MEMBER_WS, FOREIGN_WS = uuid4(), uuid4(), uuid4()
# P_FOREIGN: USER is a live member of another organization's workspace.
# authorized_project admits it; the account does not.
P_OWNED, P_OWNED_2, P_MEMBER, P_FOREIGN = (uuid4() for _ in range(4))
DEVICE, OTHER_DEVICE = uuid4(), uuid4()
OWNER = SimpleNamespace(id=USER, organization_id=ORG)
ACCOUNT_RUN = frozenset(
    {"harness:execute", "tools:read", "tools:write", "library:read", "library:write"}
)
_TABLES: list[Any] = [
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


async def _seed_account(db: AsyncSession) -> None:
    for org in (ORG, OTHER_ORG):
        await db.execute(
            insert(Organization).values(id=org, name=str(org), storage_limit_bytes=1)
        )
    for user, org in ((USER, ORG), (COLLEAGUE, ORG), (OUTSIDER, OTHER_ORG)):
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', "
                "'U', 'USER', :active, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                ":deleted)"
            ),
            {
                "id": str(user),
                "org": str(org),
                "email": f"{user}@example.test",
                "active": True,
                "deleted": False,
            },
        )
    for workspace, owner, org, name in (
        (OWNED, USER, ORG, "Owned"),
        (MEMBER_WS, COLLEAGUE, ORG, "Shared"),
        (FOREIGN_WS, OUTSIDER, OTHER_ORG, "Foreign"),
    ):
        await db.execute(
            insert(Workspace).values(
                id=workspace, name=name, owner_id=owner, organization_id=org
            )
        )
    for workspace in (MEMBER_WS, FOREIGN_WS):
        await db.execute(
            insert(WorkspaceMember).values(
                id=uuid4(),
                workspace_id=workspace,
                user_id=USER,
                role=WorkspaceRole.EDITOR,
            )
        )
    for project, workspace, name in (
        (P_OWNED, OWNED, "Thesis"),
        (P_OWNED_2, OWNED, "Grant"),
        (P_MEMBER, MEMBER_WS, "Shared notes"),
        (P_FOREIGN, FOREIGN_WS, "Foreign"),
    ):
        await db.execute(
            insert(Collection).values(id=project, name=name, workspace_id=workspace)
        )
    for device, label in ((DEVICE, "Laptop"), (OTHER_DEVICE, "Desktop")):
        await db.execute(
            insert(BridgeDevice).values(
                id=device, user_id=USER, organization_id=ORG, label=label
            )
        )
    await db.commit()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession]:
    if request.param == "sqlite":
        dsn, schema = "sqlite+aiosqlite:///:memory:", None
    else:
        raw = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
        if not raw:
            pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
        dsn = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        schema = "account_connections_" + uuid4().hex
    admin = create_async_engine(dsn) if schema else None
    if admin is not None:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = (
        create_async_engine(
            dsn, connect_args={"server_settings": {"search_path": schema}}
        )
        if schema
        else create_async_engine(dsn)
    )
    try:
        async with engine.begin() as conn:
            # Nullable chat and run associations keep their real FK targets.
            await conn.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            await conn.run_sync(
                Base.metadata.create_all, tables=[m.__table__ for m in _TABLES]
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _seed_account(session)
            yield session
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.dispose()


def _account_ctx(**values: Any) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        binding_kind="account",
        grant_id=uuid4(),
        **values,
    )


# --- scopes by kind ------------------------------------------------------------


def test_an_account_may_hold_only_the_account_scopes() -> None:
    service.check_scopes(ACCOUNT_RUN, binding_kind="account")
    service.check_scopes({"tools:read", "harness:execute"}, binding_kind="account")
    for project_only in (
        "artifacts:publish",
        "context:read",
        "handoff:read",
        "handoff:write",
    ):
        with pytest.raises(service.IntegrationAccessDenied):
            service.check_scopes({"tools:read", project_only}, binding_kind="account")
    # The scope implications still hold for an account.
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"tools:read", "library:write"}, binding_kind="account")


def test_project_and_workspace_scope_rules_are_unchanged() -> None:
    # Regression for the signature change: the old keyword still works.
    service.check_scopes({"tools:read", "artifacts:publish", "context:read"})
    service.check_scopes({"tools:read", "library:read"}, workspace_bound=True)
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"tools:read", "context:read"}, workspace_bound=True)
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"harness:execute"}, binding_kind="workspace")


def test_check_scopes_refuses_both_spellings_of_the_kind() -> None:
    kinds: tuple[BindingKind, ...] = ("workspace", "account")
    for kind in kinds:
        with pytest.raises(ValueError, match="not both"):
            service.check_scopes(
                {"tools:read"}, binding_kind=kind, workspace_bound=True
            )
    # The default kind with the old keyword is the old spelling alone.
    service.check_scopes({"tools:read"}, workspace_bound=True)


# --- validate_binding: the account branch --------------------------------------


async def test_an_account_binding_needs_a_device_and_no_chat_or_run(
    db: AsyncSession,
) -> None:
    await service.validate_binding(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        device_id=DEVICE,
        binding_kind="account",
    )
    extras: tuple[dict[str, Any], ...] = (
        {"device_id": None},
        {"device_id": DEVICE, "thread_id": uuid4()},
        {"device_id": DEVICE, "run_id": uuid4()},
        {"device_id": DEVICE, "project_id": P_OWNED},
        {"device_id": DEVICE, "workspace_id": OWNED},
        {"device_id": uuid4()},  # not the user's device
    )
    for extra in extras:
        values: dict[str, Any] = {"project_id": None, **extra}
        with pytest.raises(service.IntegrationAccessDenied):
            await service.validate_binding(
                db, user_id=USER, organization_id=ORG, binding_kind="account", **values
            )


async def test_both_null_without_the_kind_is_still_bound_to_nothing(
    db: AsyncSession,
) -> None:
    with pytest.raises(service.IntegrationAccessDenied):
        await service.validate_binding(
            db, user_id=USER, organization_id=ORG, project_id=None, device_id=DEVICE
        )


async def test_an_account_binding_ends_with_its_user(db: AsyncSession) -> None:
    await db.execute(update(User).where(User.id == USER).values(is_active=False))
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.validate_binding(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            device_id=DEVICE,
            binding_kind="account",
        )


# --- authorized_scope / authorized_scope_filter ----------------------------------


async def test_an_account_scope_is_the_account_rule_not_authorized_project(
    db: AsyncSession,
) -> None:
    assert await service.authorized_scope(db, _account_ctx()) == {
        P_OWNED,
        P_OWNED_2,
        P_MEMBER,
    }
    # P_FOREIGN stays reachable by a project pairing, never by the account.
    await service.authorized_project(db, USER, ORG, P_FOREIGN)


async def test_an_account_scope_is_rechecked_on_every_call(db: AsyncSession) -> None:
    await db.execute(
        update(Organization).where(Organization.id == ORG).values(is_active=False)
    )
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.authorized_scope_filter(db, _account_ctx())


# --- the flag, account requests, the approval DTO and the exchange -------------


@pytest.fixture(autouse=True)
def account_connections_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)


async def _approve(
    db: AsyncSession,
    *,
    device: UUID = DEVICE,
    scopes: frozenset[str] = ACCOUNT_RUN,
    **binding: Any,
) -> tuple[UUID, IssuedGrant]:
    """Request, approve and exchange one consent; account unless a project or
    workspace is given."""
    data = GrantRequestCreate(
        device_id=device,
        scopes=set(scopes),
        **(binding or {"account": True}),
    )
    request = await service.create_request(db, OWNER, data)
    await service.decide_request(db, OWNER, request.id, True)
    return request.id, await service.exchange_request(db, OWNER, request.id)


async def test_an_account_request_shows_what_it_covers(db: AsyncSession) -> None:
    dto = await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(account=True, device_id=DEVICE, scopes=set(ACCOUNT_RUN)),
    )
    assert dto.binding_kind == "account"
    assert (dto.project_id, dto.workspace_id, dto.thread_id) == (None, None, None)
    assert (dto.project_label, dto.workspace_label) == (None, None)
    assert dto.device_label == "Laptop"
    assert dto.account_summary is not None
    # OWNED and MEMBER_WS; FOREIGN_WS is another organization's.
    assert (dto.account_summary.workspace_count, dto.account_summary.project_count) == (
        2,
        3,
    )


async def test_a_project_request_is_unchanged_and_has_no_summary(
    db: AsyncSession,
) -> None:
    dto = await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(project_id=P_OWNED, device_id=DEVICE, scopes={"tools:read"}),
    )
    assert (dto.binding_kind, dto.project_label, dto.account_summary) == (
        "project",
        "Thesis",
        None,
    )


async def test_account_requests_are_refused_while_the_flag_is_off(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", False)
    with pytest.raises(service.AccountConnectionsDisabled):
        await service.create_request(
            db,
            OWNER,
            GrantRequestCreate(account=True, device_id=DEVICE, scopes={"tools:read"}),
        )
    assert await db.scalar(select(IntegrationGrantRequest.id)) is None
    # Project pairings do not depend on the flag.
    await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(project_id=P_OWNED, device_id=DEVICE, scopes={"tools:read"}),
    )


async def test_an_account_request_refuses_project_only_scopes(db: AsyncSession) -> None:
    with pytest.raises(service.IntegrationAccessDenied):
        await service.create_request(
            db,
            OWNER,
            GrantRequestCreate(
                account=True,
                device_id=DEVICE,
                scopes={"tools:read", "artifacts:publish"},
            ),
        )


async def test_exchange_issues_an_account_kind_pairing_grant(db: AsyncSession) -> None:
    consent, issued = await _approve(db)
    grant = await db.get(IntegrationGrant, issued.grant_id)
    assert grant is not None
    assert (grant.binding_kind, grant.project_id, grant.workspace_id) == (
        "account",
        None,
        None,
    )
    assert (grant.request_id, grant.device_id) == (consent, DEVICE)


# --- grant validation across kinds (Task 8) ---------------------------------------

SOON = datetime.now(timezone.utc) + timedelta(hours=1)


async def _store_grant(
    db: AsyncSession,
    *,
    request_id: UUID,
    binding_kind: str,
    project_id: UUID | None = None,
    device: UUID = DEVICE,
    scopes: frozenset[str] = ACCOUNT_RUN,
) -> str:
    """A live grant row with a known token, as a mint or exchange stores it."""
    token = "nous_ig_" + token_urlsafe(32)
    db.add(
        IntegrationGrant(
            id=uuid4(),
            user_id=USER,
            organization_id=ORG,
            binding_kind=binding_kind,
            project_id=project_id,
            device_id=device,
            request_id=request_id,
            scopes=sorted(scopes),
            token_hash=sha256(token.encode()).hexdigest(),
            expires_at=SOON,
            consented_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return token


async def _revoke(db: AsyncSession, consent: UUID) -> None:
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == consent)
        .values(consent_revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def test_an_account_grant_resolves_to_an_account_context(
    db: AsyncSession,
) -> None:
    consent, issued = await _approve(db)
    ctx = await service.resolve_integration_context(
        db, issued.token, required_scope="tools:read"
    )
    assert (ctx.binding_kind, ctx.project_id, ctx.workspace_id) == (
        "account",
        None,
        None,
    )
    assert ctx.consent_id == consent and ctx.scopes == ACCOUNT_RUN
    await _revoke(db, consent)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, issued.token, required_scope="tools:read"
        )


# SECURITY-CRITICAL: approval/grant kind matching.
@pytest.mark.parametrize("case", ["project-consent", "other-device-account-consent"])
async def test_an_account_grant_needs_an_account_consent_of_its_device(
    db: AsyncSession, case: str
) -> None:
    if case == "project-consent":
        consent, _ = await _approve(
            db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
        )
    else:
        consent, _ = await _approve(db, device=OTHER_DEVICE)
    # tools:read alone, so only the consent's kind (or device) can refuse it.
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="account",
        scopes=frozenset({"tools:read"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="tools:read"
        )


# SECURITY-CRITICAL: a project grant under an account consent.
@pytest.mark.parametrize(
    "leaves", ["archived-workspace", "archived-project", "membership-ended"]
)
async def test_a_project_grant_under_an_account_consent_ends_when_its_project_leaves(
    db: AsyncSession, leaves: str
) -> None:
    consent, _ = await _approve(db)
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_MEMBER,
        scopes=frozenset({"harness:execute"}),
    )
    ctx = await service.resolve_integration_context(
        db, token, required_scope="harness:execute"
    )
    assert (ctx.binding_kind, ctx.project_id, ctx.consent_id) == (
        "project",
        P_MEMBER,
        consent,
    )
    change = {
        "archived-workspace": update(Workspace)
        .where(Workspace.id == MEMBER_WS)
        .values(is_archived=True),
        "archived-project": update(Collection)
        .where(Collection.id == P_MEMBER)
        .values(research_status="archived"),
        "membership-ended": update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == MEMBER_WS)
        .values(is_deleted=True),
    }[leaves]
    await db.execute(change)
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="harness:execute"
        )


async def test_an_account_consent_never_covers_another_organizations_project(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    await service.authorized_project(db, USER, ORG, P_FOREIGN)  # a pairing could
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_FOREIGN,
        scopes=frozenset({"harness:execute"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="harness:execute"
        )


async def test_a_project_grant_still_needs_its_own_project_consent(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_OWNED_2,
        scopes=frozenset({"tools:read"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="tools:read"
        )


async def test_renewal_and_resume_keep_the_account_kind(db: AsyncSession) -> None:
    consent, issued = await _approve(db)
    renewed = await service.renew_grant(db, OWNER, issued.grant_id)
    stored = await db.get(IntegrationGrant, renewed.grant_id, populate_existing=True)
    assert stored is not None and stored.binding_kind == "account"
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == renewed.grant_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db.commit()
    resumed = await service.resume_grant(db, OWNER, renewed.grant_id, renewed.token)
    ctx = await service.resolve_integration_context(
        db, resumed.token, required_scope="tools:read"
    )
    assert (ctx.binding_kind, ctx.consent_id) == ("account", consent)


# SECURITY-CRITICAL: an account consent never covers more than account scopes.
async def test_a_project_grant_never_outgrows_the_account_consent_it_stands_on(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    within = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_MEMBER,
        scopes=frozenset({"harness:execute"}),
    )
    await service.resolve_integration_context(
        db, within, required_scope="harness:execute"
    )
    # artifacts:publish is a project scope the account consent does not hold.
    wider = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_MEMBER,
        scopes=frozenset({"harness:execute", "artifacts:publish"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, wider, required_scope="harness:execute"
        )


async def _expire_grant(db: AsyncSession, token: str) -> UUID:
    """Lapse the stored grant behind ``token`` (inside the resume window)."""
    grant_id: UUID | None = await db.scalar(
        select(IntegrationGrant.id).where(
            IntegrationGrant.token_hash == sha256(token.encode()).hexdigest()
        )
    )
    assert grant_id is not None
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == grant_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db.commit()
    return grant_id


# SECURITY-CRITICAL: resume re-checks that the consent admits the binding.
@pytest.mark.parametrize("leaves", ["archived-workspace", "archived-project"])
async def test_resume_of_a_project_grant_under_an_account_consent_ends_with_its_project(
    db: AsyncSession, leaves: str
) -> None:
    consent, _ = await _approve(db)
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_MEMBER,
        scopes=frozenset({"harness:execute"}),
    )
    grant_id = await _expire_grant(db, token)
    # Control: while the project is in the account, the lapsed grant resumes.
    resumed = await service.resume_grant(db, OWNER, grant_id, token)
    grant_id = await _expire_grant(db, resumed.token)
    change = {
        "archived-workspace": update(Workspace)
        .where(Workspace.id == MEMBER_WS)
        .values(is_archived=True),
        "archived-project": update(Collection)
        .where(Collection.id == P_MEMBER)
        .values(research_status="archived"),
    }[leaves]
    await db.execute(change)
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resume_grant(db, OWNER, grant_id, resumed.token)


# SECURITY-CRITICAL: an account grant resumes only from an account consent.
async def test_resume_of_an_account_grant_refuses_a_project_consent(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="account",
        scopes=frozenset({"tools:read"}),
    )
    grant_id = await _expire_grant(db, token)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resume_grant(db, OWNER, grant_id, token)


# --- a run's project grant under an account approval (Task 9) ----------------------

RUN = frozenset({"harness:execute"})


async def _mint(db: AsyncSession, project: UUID) -> Any:
    issued = await service.mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=project,
        scopes=RUN,
        device_id=DEVICE,
    )
    return await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)


async def test_a_run_grant_is_minted_under_the_account_consent(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    grant = await _mint(db, P_MEMBER)
    assert (grant.binding_kind, grant.project_id, grant.request_id) == (
        "project",
        P_MEMBER,
        consent,
    )


async def test_no_run_grant_outside_the_account(db: AsyncSession) -> None:
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_FOREIGN)


# SECURITY-CRITICAL: consent precedence (the PR 3 planner's question).
async def test_the_account_consent_wins_over_a_live_project_consent(
    db: AsyncSession,
) -> None:
    await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    account, _ = await _approve(db)
    grant = await _mint(db, P_OWNED)
    assert grant.request_id == account


async def test_two_account_consents_are_ambiguous(db: AsyncSession) -> None:
    await _approve(db)
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


async def test_project_consents_alone_keep_the_old_rule(db: AsyncSession) -> None:
    scopes = frozenset({"harness:execute", "tools:read"})
    first, _ = await _approve(db, project_id=P_OWNED, scopes=scopes)
    assert (await _mint(db, P_OWNED)).request_id == first
    await _approve(db, project_id=P_OWNED, scopes=scopes)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


async def test_an_account_consent_without_the_run_scope_mints_nothing(
    db: AsyncSession,
) -> None:
    await _approve(db, scopes=frozenset({"tools:read"}))
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


@pytest.mark.parametrize(
    ("consent_kind", "expected"), [("account", "account"), ("project", "project")]
)
async def test_consent_kind_picks_the_lineage_the_caller_names(
    db: AsyncSession, consent_kind: BindingKind, expected: str
) -> None:
    # Two live consents on one device only arise through the API (a full
    # bridge pairing registers a new device), so the API makes them here.
    project, _ = await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    account, _ = await _approve(db)
    issued = await service.mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=P_OWNED,
        scopes=RUN,
        device_id=DEVICE,
        consent_kind=consent_kind,
    )
    grant = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    assert grant is not None
    assert grant.request_id == {"account": account, "project": project}[expected]


async def test_consent_kind_account_still_fails_closed_on_two_account_consents(
    db: AsyncSession,
) -> None:
    await _approve(db)
    await _approve(db)  # a second account request on one device is allowed
    with pytest.raises(service.IntegrationAccessDenied):
        await service.mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=P_OWNED,
            scopes=RUN,
            device_id=DEVICE,
            consent_kind="account",
        )


async def test_the_mint_never_issues_an_account_grant(db: AsyncSession) -> None:
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.mint_integration_grant(
            db, user_id=USER, organization_id=ORG, scopes=RUN, device_id=DEVICE
        )


# --- devices and folders (Task 10) -------------------------------------------------


def test_an_omitted_folder_project_is_an_account_folder() -> None:
    assert WorkspaceBindingCreate(label="repo", workspace_id=uuid4()).project_id is None
    assert (
        WorkspaceBindingCreate(
            label="repo", workspace_id=uuid4(), project_id=None
        ).project_id
        is None
    )


async def _consent_row(
    db: AsyncSession,
    *,
    device: UUID,
    binding: dict[str, Any],
    scopes: list[str],
    revoked: bool = False,
    thread: UUID | None = None,
    status: str = "consumed",
    user: UUID = USER,
) -> UUID:
    """A consent written directly, consumed unless a status is given, for shapes
    create_request refuses to make here (a chat-bound one needs a real chat, a
    colleague's one is not ours to approve)."""
    consent = uuid4()
    if thread is not None:
        await db.execute(
            text("INSERT INTO threads (id) VALUES (:id)"), {"id": str(thread)}
        )
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent,
            user_id=user,
            organization_id=ORG,
            device_id=device,
            scopes=scopes,
            status=status,
            expires_at=SOON,
            thread_id=thread,
            consent_revoked_at=datetime.now(timezone.utc) if revoked else None,
            **binding,
        )
    )
    await db.commit()
    return consent


async def test_list_devices_reports_account_and_project_run_consents(
    db: AsyncSession,
) -> None:
    run = ["harness:execute", "tools:read"]
    await _consent_row(
        db, device=DEVICE, binding={"binding_kind": "account"}, scopes=run
    )
    await _consent_row(
        db, device=OTHER_DEVICE, binding={"project_id": P_OWNED}, scopes=run
    )
    chat = uuid4()
    await _consent_row(
        db,
        device=OTHER_DEVICE,
        binding={"project_id": P_OWNED_2},
        scopes=run,
        thread=chat,
    )
    # Ignored: no harness:execute, and a revoked account consent.
    await _consent_row(
        db, device=OTHER_DEVICE, binding={"project_id": P_MEMBER}, scopes=["tools:read"]
    )
    await _consent_row(
        db,
        device=OTHER_DEVICE,
        binding={"binding_kind": "account"},
        scopes=run,
        revoked=True,
    )
    listed = {device.id: device for device in await service.list_devices(db, OWNER)}
    assert (listed[DEVICE].account_wide, listed[DEVICE].project_ids) == (True, [])
    assert listed[DEVICE].bound_thread_ids == []
    assert listed[OTHER_DEVICE].account_wide is False
    assert listed[OTHER_DEVICE].project_ids == sorted([P_OWNED, P_OWNED_2], key=str)
    # A project-wide run consent beside the chat-bound one: not restricted.
    assert listed[OTHER_DEVICE].bound_thread_ids == []


# SECURITY-CRITICAL: an account folder needs a live account approval.
async def test_an_account_folder_needs_a_live_account_run_consent(
    db: AsyncSession,
) -> None:
    folder = WorkspaceBindingCreate(label="repo", workspace_id=uuid4(), project_id=None)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    await _approve(db, scopes=frozenset({"tools:read"}))  # cannot run Codex
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    await _approve(db, device=OTHER_DEVICE)  # another computer's approval
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    # A project approval of this very device that can run Codex is not an
    # account approval.
    await _approve(db, project_id=P_OWNED)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    consent, _ = await _approve(db)
    bound = await service.bind_workspace(db, OWNER, DEVICE, folder)
    assert bound.project_id is None
    assert (await service.bind_workspace(db, OWNER, DEVICE, folder)).id == bound.id
    await _revoke(db, consent)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(
                label="other", workspace_id=uuid4(), project_id=None
            ),
        )


@pytest.mark.parametrize("status", ["pending", "denied", "approved"])
async def test_an_account_request_not_yet_exchanged_admits_no_account_folder(
    db: AsyncSession, status: str
) -> None:
    await _consent_row(
        db,
        device=DEVICE,
        binding={"binding_kind": "account"},
        scopes=["harness:execute", "tools:read"],
        status=status,
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="repo", workspace_id=uuid4(), project_id=None),
        )


async def test_another_users_account_consent_admits_no_account_folder(
    db: AsyncSession,
) -> None:
    await _consent_row(
        db,
        device=DEVICE,
        binding={"binding_kind": "account"},
        scopes=["harness:execute", "tools:read"],
        user=COLLEAGUE,
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="repo", workspace_id=uuid4(), project_id=None),
        )


async def test_list_devices_reports_no_account_wide_once_the_account_is_not_live(
    db: AsyncSession,
) -> None:
    await _approve(db)
    assert {d.id: d.account_wide for d in await service.list_devices(db, OWNER)}[
        DEVICE
    ] is True
    await db.execute(
        update(Organization).where(Organization.id == ORG).values(is_active=False)
    )
    await db.commit()
    assert {d.id: d.account_wide for d in await service.list_devices(db, OWNER)}[
        DEVICE
    ] is False


async def test_list_devices_reports_no_account_wide_with_two_account_approvals(
    db: AsyncSession,
) -> None:
    """Two runnable account approvals make every run on the device ambiguous
    (the mint fails closed), so the composer must not offer the computer."""
    run = ["harness:execute", "tools:read"]
    await _consent_row(
        db, device=DEVICE, binding={"binding_kind": "account"}, scopes=run
    )
    listed = {d.id: d for d in await service.list_devices(db, OWNER)}
    assert listed[DEVICE].account_wide is True
    await _consent_row(
        db, device=DEVICE, binding={"binding_kind": "account"}, scopes=run
    )
    listed = {d.id: d for d in await service.list_devices(db, OWNER)}
    assert listed[DEVICE].account_wide is False


async def test_list_workspaces_keeps_account_folders_while_the_approval_lives(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    account_folder = await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(label="any", workspace_id=uuid4(), project_id=None),
    )
    project_folder = await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(
            label="thesis", workspace_id=uuid4(), project_id=P_OWNED
        ),
    )
    listed = {
        binding.id for binding in await service.list_workspaces(db, OWNER, DEVICE)
    }
    assert listed == {account_folder.id, project_folder.id}
    await _revoke(db, consent)
    listed = {
        binding.id for binding in await service.list_workspaces(db, OWNER, DEVICE)
    }
    assert listed == {project_folder.id}


async def test_a_folder_keeps_its_kind_and_a_project_folder_its_project(
    db: AsyncSession,
) -> None:
    await _approve(db)
    workspace_id = uuid4()
    await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(
            label="repo", workspace_id=workspace_id, project_id=None
        ),
    )
    with pytest.raises(service.IntegrationConflict):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(
                label="repo", workspace_id=workspace_id, project_id=P_OWNED
            ),
        )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="x", workspace_id=uuid4(), project_id=uuid4()),
        )


async def test_account_folders_need_the_user_active_in_a_live_organization(
    db: AsyncSession,
) -> None:
    await _approve(db)
    folder = await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(label="any", workspace_id=uuid4(), project_id=None),
    )
    await db.execute(
        update(Organization).where(Organization.id == ORG).values(is_active=False)
    )
    await db.commit()
    # The consent row still reads live; the account itself no longer does.
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="more", workspace_id=uuid4(), project_id=None),
        )
    assert folder.id not in {
        binding.id for binding in await service.list_workspaces(db, OWNER, DEVICE)
    }
