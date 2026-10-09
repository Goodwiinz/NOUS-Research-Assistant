"""Two-connection proof of the Stop/first-delivery winner."""

import asyncio
import os
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.harness_session import HarnessCommand, HarnessReceipt, HarnessSession
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User, UserRole
from src.models.workspace import Workspace, WorkspaceMember
from src.services.agent.agent_submission_service import request_run_cancellation
from src.services.harness import delivery
from tests.unit.services.harness.test_runs import (
    CONVERSATION,
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
    accept,
    context,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def mock_external_services() -> None:
    """This database-only fixture uses no vector, mail, or model clients."""


@pytest.fixture
async def pg_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert dsn is not None
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "harness_cancel_" + uuid4().hex
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
            ]
            for model in models:
                await conn.run_sync(model.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(Organization(id=ORG, name="org", storage_limit_bytes=1000))
            await db.flush()
            db.add(
                User(
                    id=USER,
                    organization_id=ORG,
                    email="harness@example.test",
                    password_hash="unused",
                    first_name="Test",
                    last_name="User",
                    role=UserRole.USER,
                )
            )
            await db.flush()
            db.add(
                Workspace(id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG)
            )
            await db.flush()
            db.add_all(
                [
                    Collection(id=PROJECT, workspace_id=WORKSPACE, name="p"),
                    Conversation(
                        id=CONVERSATION,
                        workspace_id=WORKSPACE,
                        title="c",
                        created_by_id=USER,
                    ),
                    BridgeDevice(
                        id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    Thread(
                        id=THREAD,
                        conversation_id=CONVERSATION,
                        source_project_id=PROJECT,
                        title="t",
                        created_by_id=USER,
                        message_count=0,
                    ),
                    WorkspaceBinding(
                        device_id=DEVICE,
                        workspace_id=LOCAL,
                        project_id=PROJECT,
                        label="repo",
                    ),
                ]
            )
            await db.commit()
        monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


@pytest.mark.parametrize("winner", ["stop", "lease"])
async def test_first_lease_races_stop(
    pg_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    winner: str,
) -> None:
    async with pg_factory() as setup:
        consent = await getattr(context, "__wrapped__")(setup)
        accepted = await accept(setup, consent)
        assert await delivery.dispatch_pending(setup) == 1
    reached, release, loser_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_locked = delivery._locked

    async def paused_locked(db: AsyncSession, run_id: str) -> Any:
        result = await real_locked(db, run_id)
        if winner == "lease":
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(delivery, "_locked", paused_locked)

    async def stop() -> Any:
        async with pg_factory() as db:
            if winner == "lease":
                loser_entered.set()
            result = await request_run_cancellation(
                db,
                run_id=accepted.run_id,
                thread_id=THREAD,
                organization_id=ORG,
                user_id=USER,
                reason="user",
            )
            if winner == "stop":
                reached.set()
                await release.wait()
            await db.commit()
            return result

    async def lease() -> Any:
        async with pg_factory() as db:
            if winner == "stop":
                loser_entered.set()
            return await delivery.lease_commands(db, consent, DEVICE)

    first = asyncio.create_task(stop() if winner == "stop" else lease())
    second = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        second = asyncio.create_task(lease() if winner == "stop" else stop())
        await asyncio.wait_for(loser_entered.wait(), 5)
        await asyncio.sleep(0.1)
        assert not second.done(), "The losing operation escaped the run lock"
        release.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second), 10
        )
        leased = second_result if winner == "stop" else first_result
        async with pg_factory() as check:
            run: Any = await check.get(AgentRun, accepted.run_id)
            session = await check.scalar(select(HarnessSession))
            command = await check.scalar(select(HarnessCommand))
            assert run is not None and session is not None and command is not None
            if winner == "stop":
                assert leased == [] and run.status == "cancelled"
                assert not session.workspace_locked and command.lease_until is None
            else:
                assert len(leased) == 1 and command.lease_until is not None
                assert run.status == "stopping" and session.workspace_locked
    finally:
        release.set()
        await asyncio.gather(
            first, *([] if second is None else [second]), return_exceptions=True
        )
