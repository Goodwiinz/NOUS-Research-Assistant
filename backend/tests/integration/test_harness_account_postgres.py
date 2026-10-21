r"""PostgreSQL evidence for Local Codex runs under an account approval.

Account-connection design §3; the SQLite twin, with the mutation notes, is
backend/tests/unit/services/harness/test_account_runs.py. Here the account
rule, the lease query's ``IN (subquery)`` and the long-run swap run on real
PostgreSQL.

Focused command, from the repository root (``PY`` and the throwaway
PostgreSQL come from Gate 0 of ``docs/testing/harness-live-proof.md``)::

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch \
    ENVIRONMENT=testing PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
    backend/tests/integration/test_harness_account_postgres.py
"""

import os
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User, UserRole
from src.models.workspace import Workspace, WorkspaceMember
from src.services.harness.approvals import (
    NativeDecision,
    ingest_native_request,
    native_request_context,
    resolve_native_request,
)
from src.services.harness.delivery import (
    dispatch_pending,
    ingest_bridge_event,
    lease_commands,
    lease_runs,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    renew_grant,
    resolve_integration_context,
)
from tests.unit.services.harness.test_account_runs import (
    ACCOUNT_DEVICE,
    CONVERSATION_B,
    FOLDER_A,
    FOLDER_B,
    OWNER,
    PROJECT_B,
    THREAD_B,
    WORKSPACE_B,
    event_for,
    expire_run_grants,
    leave_account,
    native_request_event,
    pair_account,
    pair_project,
    running_event,
    start,
)
from tests.unit.services.harness.test_runs import (
    CONVERSATION,
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def mock_external_services() -> None:
    """This database-only fixture uses no vector, mail, or model clients."""


@pytest.fixture
async def pg(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "harness_account_" + uuid4().hex
    admin = create_async_engine(dsn)
    async with admin.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runtime_snapshots (id UUID PRIMARY KEY)"
            )
            models: list[Any] = [
                Organization,
                User,
                Document,
                Workspace,
                WorkspaceMember,
                Collection,
                Conversation,
                Thread,
                ChatMessage,
                AgentRun,
                AgentRunEvent,
                AgentOutbox,
                BridgeDevice,
                WorkspaceBinding,
                IntegrationGrantRequest,
                IntegrationGrant,
                HarnessSession,
                HarnessCommand,
                HarnessReceipt,
                HarnessNativeRequest,
            ]
            for model in models:
                await conn.run_sync(model.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            levels: list[list[Any]] = [
                [Organization(id=ORG, name="org", storage_limit_bytes=1000)],
                [
                    User(
                        id=USER,
                        organization_id=ORG,
                        email="harness@example.test",
                        password_hash="unused",
                        first_name="Test",
                        last_name="User",
                        role=UserRole.USER,
                    )
                ],
                [
                    Workspace(
                        id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG
                    ),
                    Workspace(
                        id=WORKSPACE_B, name="w2", owner_id=USER, organization_id=ORG
                    ),
                ],
                [
                    Collection(id=PROJECT, workspace_id=WORKSPACE, name="p"),
                    Collection(id=PROJECT_B, workspace_id=WORKSPACE_B, name="p2"),
                    Conversation(
                        id=CONVERSATION,
                        workspace_id=WORKSPACE,
                        title="c",
                        created_by_id=USER,
                    ),
                    Conversation(
                        id=CONVERSATION_B,
                        workspace_id=WORKSPACE_B,
                        title="c2",
                        created_by_id=USER,
                    ),
                    BridgeDevice(
                        id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"
                    ),
                    BridgeDevice(
                        id=ACCOUNT_DEVICE,
                        user_id=USER,
                        organization_id=ORG,
                        label="desktop",
                    ),
                ],
                [
                    Thread(
                        id=THREAD,
                        conversation_id=CONVERSATION,
                        source_project_id=PROJECT,
                        title="t",
                        created_by_id=USER,
                        message_count=0,
                    ),
                    Thread(
                        id=THREAD_B,
                        conversation_id=CONVERSATION_B,
                        source_project_id=PROJECT_B,
                        title="t2",
                        created_by_id=USER,
                        message_count=0,
                    ),
                    WorkspaceBinding(
                        device_id=DEVICE,
                        workspace_id=LOCAL,
                        project_id=PROJECT,
                        label="repo",
                    ),
                    WorkspaceBinding(
                        device_id=ACCOUNT_DEVICE,
                        workspace_id=FOLDER_A,
                        project_id=None,
                        label="a",
                    ),
                    WorkspaceBinding(
                        device_id=ACCOUNT_DEVICE,
                        workspace_id=FOLDER_B,
                        project_id=None,
                        label="b",
                    ),
                ],
            ]
            for level in levels:
                db.add_all(level)
                await db.flush()
            await db.commit()
        monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
        monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_one_account_bridge_leases_runs_in_two_projects(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        run_a = await start(db, THREAD, FOLDER_A)
        run_b = await start(db, THREAD_B, FOLDER_B)
        assert await dispatch_pending(db) == 2
    async with pg() as bridge:
        commands = await lease_commands(bridge, account, ACCOUNT_DEVICE)
        assert {(str(c.runId), c.projectId) for c in commands} == {
            (run_a, PROJECT),
            (run_b, PROJECT_B),
        }
        assert set(await lease_runs(bridge, account, ACCOUNT_DEVICE)) == {run_a, run_b}
        for command in commands:
            assert await ingest_bridge_event(bridge, account, event_for(command)) >= 1


async def test_a_project_that_leaves_the_account_is_refused_at_the_next_command(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        run_a = await start(db, THREAD, FOLDER_A)
        run_b = await start(db, THREAD_B, FOLDER_B)
        assert await dispatch_pending(db) == 2
        commands = {
            str(c.runId): c for c in await lease_commands(db, account, ACCOUNT_DEVICE)
        }
        await leave_account(db)
        await db.execute(update(HarnessCommand).values(lease_until=None))
        await db.commit()
    async with pg() as bridge:
        assert [
            str(c.runId) for c in await lease_commands(bridge, account, ACCOUNT_DEVICE)
        ] == [run_a]
        assert set(await lease_runs(bridge, account, ACCOUNT_DEVICE)) == {run_a}
        with pytest.raises(IntegrationAccessDenied):
            await ingest_bridge_event(bridge, account, event_for(commands[run_b]))


async def test_a_long_run_outlives_its_run_grant_and_resolves_an_approval_card(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        await start(db, THREAD_B, FOLDER_A)
        assert await dispatch_pending(db) == 1
        (first,) = await lease_commands(db, account, ACCOUNT_DEVICE)
        await expire_run_grants(db)
        issued = await renew_grant(db, OWNER, account.grant_id)
        renewed = await resolve_integration_context(
            db, issued.token, required_scope="harness:execute"
        )
        await db.execute(update(HarnessCommand).values(lease_until=None))
        await db.commit()
    async with pg() as bridge:
        (again,) = await lease_commands(bridge, renewed, ACCOUNT_DEVICE)
        assert again.commandId == first.commandId
        await ingest_bridge_event(bridge, renewed, running_event(again, 1))
        request_id, _ = await ingest_native_request(
            bridge, renewed, native_request_event(again, 2)
        )
    async with pg() as browser:
        decision = await native_request_context(browser, request_id, USER)
        await resolve_native_request(
            browser, decision, request_id, NativeDecision.deny()
        )
    async with pg() as bridge:
        kinds = [
            c.body.kind for c in await lease_commands(bridge, renewed, ACCOUNT_DEVICE)
        ]
        assert kinds == ["respond"]


async def test_a_project_pairing_still_cannot_lease_another_projects_run(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        pairing_a = await pair_project(db, PROJECT, DEVICE)
        folder_b = uuid4()
        db.add(
            WorkspaceBinding(
                device_id=DEVICE, workspace_id=folder_b, project_id=PROJECT_B, label="b"
            )
        )
        await db.commit()
        pairing_b = await pair_project(db, PROJECT_B, DEVICE)
        run_b = await start(db, THREAD_B, folder_b, device=DEVICE)
        assert await dispatch_pending(db) == 1
    async with pg() as bridge:
        assert await lease_commands(bridge, pairing_a, DEVICE) == []
        assert await lease_runs(bridge, pairing_a, DEVICE) == {}
        (command,) = await lease_commands(bridge, pairing_b, DEVICE)
        assert str(command.runId) == run_b
        with pytest.raises(IntegrationAccessDenied):
            await ingest_bridge_event(bridge, pairing_a, event_for(command))


async def test_with_both_approvals_on_one_device_the_account_lineage_runs_every_folder(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """Both approvals live on one device: the account approval wins for every
    folder it covers (as the bridge's account pairing holds that lineage), and
    the project pairing's lineage leases none of those runs."""
    async with pg() as db:
        account = await pair_account(db)
        project = await pair_project(db, PROJECT, ACCOUNT_DEVICE)
        tied = uuid4()
        db.add(
            WorkspaceBinding(
                device_id=ACCOUNT_DEVICE,
                workspace_id=tied,
                project_id=PROJECT,
                label="tied",
            )
        )
        await db.commit()
        via_account_folder = await start(db, THREAD_B, FOLDER_A)
        via_project_folder = await start(db, THREAD, tied)
        sessions = (await db.scalars(select(HarnessSession))).all()
        lineage: dict[str, Any] = {}
        for session in sessions:
            grant = await db.get(IntegrationGrant, session.grant_id)
            assert grant is not None
            lineage[session.run_id] = grant.request_id
        assert lineage == {
            via_account_folder: account.consent_id,
            via_project_folder: account.consent_id,
        }
        assert project.consent_id != account.consent_id
        assert await dispatch_pending(db) == 2
    async with pg() as bridge:
        assert await lease_commands(bridge, project, ACCOUNT_DEVICE) == []
    async with pg() as bridge:
        leased = {
            str(c.runId): c.projectId
            for c in await lease_commands(bridge, account, ACCOUNT_DEVICE)
        }
        assert leased == {via_account_folder: PROJECT_B, via_project_folder: PROJECT}
