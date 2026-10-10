r"""Real-Postgres evidence that harness dispatch skips a row another worker holds.

Guards under test: the per-run ``AgentRun`` claim and ``AgentOutbox`` select in
``services/harness/delivery.py::dispatch_pending``, which lock their rows with
``.with_for_update(skip_locked=True)``. The run is claimed first to keep the
Stop/lease lock order (run, then outbox).

Focused command, from the repository root (``PY`` and the throwaway PostgreSQL
come from Gate 0 of ``docs/testing/harness-live-proof.md``)::

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch \
    ENVIRONMENT=testing PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
    backend/tests/integration/test_harness_dispatch_postgres.py

Mutation verification (2026-10-04, local PostgreSQL 14; re-run 2026-10-10,
PostgreSQL 16, after the per-run claim): replacing
``.with_for_update(skip_locked=True)`` on the ``AgentOutbox`` select in
``services/harness/delivery.py::dispatch_pending`` with ``.with_for_update()``
makes the worker wait on the held row and the ``[outbox]`` case FAIL with
TimeoutError; doing the same to the ``AgentRun`` claim fails the ``[run]``
case. Restore the source after each mutation.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.base import Base
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.harness_session import HarnessCommand, HarnessReceipt, HarnessSession
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import GrantRequestCreate
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.delivery import dispatch_pending
from src.services.integrations.context import (
    create_request,
    decide_request,
    exchange_request,
    resolve_integration_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    Conversation,
    Thread,
    ChatMessage,
    AgentRun,
    AgentRuntimeSnapshot,
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


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    return dsn


@pytest.mark.parametrize("held_model", [AgentOutbox, AgentRun], ids=["outbox", "run"])
async def test_dispatch_skips_outbox_row_locked_by_another_worker(
    monkeypatch: pytest.MonkeyPatch, held_model: Any
) -> None:
    # `or ""` keeps dsn a `str` without leaning on pytest.skip being NoReturn:
    # the Lint Backend job has no pytest installed, so there skip() is Any.
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    schema = "harness_dispatch_" + uuid.uuid4().hex
    admin = create_async_engine(_async_dsn(dsn))
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        _async_dsn(dsn), connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as connection:
            tables = [cast(Any, model).__table__ for model in _MODELS]
            await connection.run_sync(Base.metadata.create_all, tables=tables)
        user, org, workspace, project, conversation, thread, device, local = (
            uuid.uuid4() for _ in range(8)
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(
                text("""INSERT INTO organizations (id, created_at, updated_at,
                    is_deleted, name, storage_tier, storage_used_bytes,
                    storage_limit_bytes, is_active) VALUES (:id, now(), now(),
                    false, 'org', 'FREE', 0, 1000, true)"""),
                {"id": org},
            )
            await db.execute(
                text("""INSERT INTO users (id, created_at, updated_at, is_deleted,
                    email, password_hash, first_name, last_name, role, is_active,
                    organization_id, login_count) VALUES (:id, now(), now(), false,
                    :email, 'unused', 'x', 'y', 'USER', true, :org, 0)"""),
                {"id": user, "email": f"{user}@example.test", "org": org},
            )
            # One flush per parent level: these models declare no relationships,
            # so the unit of work cannot order the foreign keys itself.
            for row in (
                Workspace(id=workspace, name="w", owner_id=user, organization_id=org),
                Collection(id=project, workspace_id=workspace, name="p"),
                Conversation(
                    id=conversation,
                    workspace_id=workspace,
                    title="c",
                    created_by_id=user,
                ),
                Thread(
                    id=thread,
                    conversation_id=conversation,
                    source_project_id=project,
                    title="t",
                    created_by_id=user,
                    message_count=0,
                ),
                BridgeDevice(
                    id=device, user_id=user, organization_id=org, label="laptop"
                ),
                WorkspaceBinding(
                    device_id=device,
                    workspace_id=local,
                    project_id=project,
                    label="repo",
                ),
            ):
                db.add(row)
                await db.flush()
            await db.commit()
            owner = SimpleNamespace(id=user, organization_id=org)
            grant_request = await create_request(
                db,
                owner,
                GrantRequestCreate(
                    project_id=project, device_id=device, scopes={"harness:execute"}
                ),
            )
            await decide_request(db, owner, grant_request.id, True)
            issued = await exchange_request(db, owner, grant_request.id)
            context = await resolve_integration_context(
                db, issued.token, required_scope="harness:execute"
            )
            await accept_submission(
                db,
                current_user=owner,
                request=AgentExecuteRequest(
                    messages=[
                        AgentMessage(
                            role="user", content="hello", client_message_id=uuid.uuid4()
                        )
                    ],
                    execution_provider="codex",
                    device_id=device,
                    workspace_id=local,
                    thread_id=str(thread),
                ),
                thread=await db.get(Thread, thread),
                integration_context=context,
            )

        async with factory() as holder, factory() as worker:
            # Another worker (or Stop) holds the pending run's outbox or run
            # row in an open transaction.
            held = await holder.scalar(select(held_model).with_for_update().limit(1))
            assert held is not None
            assert await asyncio.wait_for(dispatch_pending(worker), timeout=5) == 0
            await holder.rollback()
            assert await asyncio.wait_for(dispatch_pending(worker), timeout=5) == 1
            assert await dispatch_pending(worker) == 0
            commands = await worker.scalar(
                select(func.count()).select_from(HarnessCommand)
            )
            assert commands == 1
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()
