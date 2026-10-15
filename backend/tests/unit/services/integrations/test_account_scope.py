"""The account rule: which workspaces and projects one account approval covers.

SECURITY-CRITICAL. Runs on SQLite always and on PostgreSQL when
ORCHESTRATION_TEST_DATABASE_URL is set (docs/testing/harness-live-proof.md
Gate 0). Without the URL the PostgreSQL half is skipped, which is NOT RUN,
never PASS.

The rule is /projects' organization rule (project_access
.accessible_research_workspace_ids_query) plus "not archived", with the user
and organization live. It is stricter than authorized_project: an explicit
member of another organization's workspace keeps project-pairing access, but
that workspace is never in the account.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.collection import Collection
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.integrations.context import (
    IntegrationAccessDenied,
    account_scope_condition,
    account_summary,
    account_workspace_condition,
    authorized_account,
    authorized_account_workspace,
)
from src.services.research_engine.project_access import (
    accessible_research_workspace_ids_query,
)

pytestmark = pytest.mark.unit

ORG, OTHER_ORG = uuid4(), uuid4()
USER, COLLEAGUE, OUTSIDER = uuid4(), uuid4(), uuid4()
# name: (owner, organization_id, is_archived, is_deleted, is_public, USER's
# membership: None, "live" or "lapsed"). A None organization is a legacy row,
# which belongs to its owner's organization.
WORKSPACES: dict[str, tuple[UUID, UUID | None, bool, bool, bool, str | None]] = {
    "owned": (USER, ORG, False, False, False, None),
    "member": (COLLEAGUE, ORG, False, False, False, "live"),
    "legacy-owned": (USER, None, False, False, False, None),
    "legacy-colleague-member": (COLLEAGUE, None, False, False, False, "live"),
    "lapsed-member": (COLLEAGUE, ORG, False, False, False, "lapsed"),
    "not-member": (COLLEAGUE, ORG, False, False, False, None),
    "public-not-member": (COLLEAGUE, ORG, False, False, True, None),
    "other-org-member": (OUTSIDER, OTHER_ORG, False, False, False, "live"),
    "legacy-other-org-member": (OUTSIDER, None, False, False, False, "live"),
    "archived": (USER, ORG, True, False, False, None),
    "deleted": (USER, ORG, False, True, False, None),
}
WORKSPACE = {name: uuid4() for name in WORKSPACES}
PROJECT = {name: uuid4() for name in WORKSPACES}
# Extra Collections in "owned": the rule must drop the first two by themselves.
ARCHIVED_PROJECT, DELETED_PROJECT, PAUSED_PROJECT = uuid4(), uuid4(), uuid4()
IN_ACCOUNT = {"owned", "member", "legacy-owned", "legacy-colleague-member"}


async def _seed(db: AsyncSession) -> None:
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
    for name, (owner, org, archived, deleted, public, membership) in WORKSPACES.items():
        await db.execute(
            insert(Workspace).values(
                id=WORKSPACE[name],
                name=name,
                owner_id=owner,
                organization_id=org,
                is_archived=archived,
                is_deleted=deleted,
                is_public=public,
            )
        )
        await db.execute(
            insert(Collection).values(
                id=PROJECT[name], name=name, workspace_id=WORKSPACE[name]
            )
        )
        if membership is not None:
            await db.execute(
                insert(WorkspaceMember).values(
                    id=uuid4(),
                    workspace_id=WORKSPACE[name],
                    user_id=USER,
                    role=WorkspaceRole.EDITOR,
                    is_deleted=membership == "lapsed",
                )
            )
    await db.execute(
        insert(Collection).values(
            [
                dict(
                    id=ARCHIVED_PROJECT,
                    name="archived project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="archived",
                    is_deleted=False,
                ),
                dict(
                    id=DELETED_PROJECT,
                    name="deleted project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="active",
                    is_deleted=True,
                ),
                dict(
                    id=PAUSED_PROJECT,
                    name="paused project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="paused",
                    is_deleted=False,
                ),
            ]
        )
    )
    await db.commit()


_MODELS: list[Any] = [Organization, User, Workspace, WorkspaceMember, Collection]


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            for model in _MODELS:
                await conn.run_sync(model.__table__.create)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await _seed(session)
                yield session
        finally:
            await engine.dispose()
        return
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "account_scope_" + uuid4().hex
    admin = create_async_engine(dsn)
    async with admin.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all, tables=[m.__table__ for m in _MODELS]
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _seed(session)
            yield session
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _account_projects(db: AsyncSession) -> set[UUID]:
    rows = await db.scalars(
        select(Collection.id).where(account_scope_condition(USER, ORG))
    )
    return {UUID(str(value)) for value in rows.all()}


async def _account_workspaces(db: AsyncSession) -> set[UUID]:
    rows = await db.scalars(
        select(Workspace.id).where(account_workspace_condition(USER, ORG))
    )
    return {UUID(str(value)) for value in rows.all()}


@pytest.mark.parametrize("name", sorted(WORKSPACES))
async def test_each_workspace_is_in_the_account_only_for_its_reason(
    db: AsyncSession, name: str
) -> None:
    inside = name in IN_ACCOUNT
    assert (WORKSPACE[name] in await _account_workspaces(db)) is inside
    assert (PROJECT[name] in await _account_projects(db)) is inside


async def test_the_account_is_exactly_its_live_unarchived_projects(
    db: AsyncSession,
) -> None:
    assert await _account_projects(db) == {PROJECT[n] for n in IN_ACCOUNT} | {
        PAUSED_PROJECT
    }
    assert await _account_workspaces(db) == {WORKSPACE[n] for n in IN_ACCOUNT}


async def test_the_account_agrees_with_the_projects_rule(db: AsyncSession) -> None:
    # /projects (and so nous --rc) lists accessible_research_workspace_ids:
    # the same organization rule, legacy NULL-org rows included. The account
    # adds only "not archived".
    research = {
        UUID(str(value))
        for value in (
            await db.scalars(accessible_research_workspace_ids_query(USER))
        ).all()
    }
    assert await _account_workspaces(db) == research - {WORKSPACE["archived"]}


@pytest.mark.parametrize(
    "lapse",
    ["user-inactive", "user-deleted", "user-left", "org-inactive", "org-deleted"],
)
async def test_a_lapsed_identity_empties_the_account(
    db: AsyncSession, lapse: str
) -> None:
    statement = {
        "user-inactive": update(User).where(User.id == USER).values(is_active=False),
        "user-deleted": update(User).where(User.id == USER).values(is_deleted=True),
        "user-left": update(User)
        .where(User.id == USER)
        .values(organization_id=OTHER_ORG),
        "org-inactive": update(Organization)
        .where(Organization.id == ORG)
        .values(is_active=False),
        "org-deleted": update(Organization)
        .where(Organization.id == ORG)
        .values(is_deleted=True),
    }[lapse]
    await db.execute(statement)
    await db.commit()
    assert await _account_projects(db) == set()
    assert await _account_workspaces(db) == set()
    with pytest.raises(IntegrationAccessDenied):
        await authorized_account(db, USER, ORG)


async def test_the_account_is_evaluated_on_every_statement(db: AsyncSession) -> None:
    await authorized_account(db, USER, ORG)
    fresh = uuid4()
    await db.execute(
        insert(Collection).values(
            id=fresh, name="new", workspace_id=WORKSPACE["member"]
        )
    )
    await db.execute(
        update(Workspace)
        .where(Workspace.id == WORKSPACE["owned"])
        .values(is_archived=True)
    )
    await db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == WORKSPACE["legacy-colleague-member"]
        )
    )
    await db.commit()
    assert await _account_projects(db) == {
        PROJECT["member"],
        PROJECT["legacy-owned"],
        fresh,
    }


async def test_authorized_account_workspace_names_only_account_workspaces(
    db: AsyncSession,
) -> None:
    row = await authorized_account_workspace(db, USER, ORG, WORKSPACE["member"])
    assert (row.id, row.name) == (WORKSPACE["member"], "member")
    for name in sorted(set(WORKSPACES) - IN_ACCOUNT):
        with pytest.raises(IntegrationAccessDenied):
            await authorized_account_workspace(db, USER, ORG, WORKSPACE[name])
    with pytest.raises(IntegrationAccessDenied):
        await authorized_account_workspace(db, USER, ORG, uuid4())


async def test_account_summary_counts_what_the_rule_covers_today(
    db: AsyncSession,
) -> None:
    summary = await account_summary(db, USER, ORG)
    assert (summary.workspace_count, summary.project_count) == (4, 5)
    await db.execute(
        insert(Workspace).values(
            id=uuid4(), name="empty", owner_id=USER, organization_id=ORG
        )
    )
    await db.commit()
    summary = await account_summary(db, USER, ORG)
    assert (summary.workspace_count, summary.project_count) == (5, 5)
