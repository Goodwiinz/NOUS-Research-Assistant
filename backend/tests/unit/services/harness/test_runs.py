"""External ownership survives uncertain native execution and atomic failures.

Mutation verification (2026-09-28): for each guard below, temporarily remove
its predicate, run ``python -m pytest -q <this-file> -k <named-test> -p
no:cacheprovider``, and restore the source. Each mutated test was observed FAIL.

* harness/runs.py: bind_external_submission actor/org/thread/queued/provider
  guards -> test_binding_checks_actual_run; authorize_external_submission
  device/project/deletion/scope -> test_different_owned_device_cannot_use_grant,
  test_current_authority_rechecked, test_read_only_grant_cannot_execute.
* models/{agent_run,harness_session}.py: active indexes -> test_database_enforces_*.
* agent_run_service.py: native projection and execution provider predicates;
  agent_submission_service.py: queued failure, dispatch and terminal provider
  predicates -> test_native_execution_and_projection_cannot_seize_external_run.
* agent_submission_service.py: abandonment provider predicate ->
  test_external_approval_pause_cannot_be_abandoned (direct helper assertion;
  acceptance alone masks the defect through later workspace-conflict rollback).
* agent_submission_service.py: replay run/thread/provider/workspace predicates
  -> test_replay_honors_run_scoped_context and test_replay_cannot_change_execution_binding.
* harness/runs.py: cancellation intent and terminal absorption ->
  test_cancel_intent_survives_recovery_and_native_completion and
  test_terminal_evidence_is_absorbing.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import (
    accept_submission,
    request_run_cancellation,
)
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.runs import record_observation
from src.services.integrations.context import (
    IntegrationAccessDenied,
    create_request,
    decide_request,
    exchange_request,
    resolve_integration_context,
)
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, WORKSPACE, CONVERSATION, THREAD, DEVICE, LOCAL = (
    uuid4() for _ in range(8)
)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    from src.models.harness_session import HarnessSession

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def disable_transactions(connection: Any, record: Any) -> None:
        connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
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
        ]
        for model in models:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(id=ORG, name="org", storage_limit_bytes=1000)
        )
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'harness@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(USER), "org": str(ORG)},
        )
        session.add_all(
            [
                Workspace(id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG),
                Collection(id=PROJECT, workspace_id=WORKSPACE, name="p"),
                Conversation(
                    id=CONVERSATION,
                    workspace_id=WORKSPACE,
                    title="c",
                    created_by_id=USER,
                ),
                Thread(
                    id=THREAD,
                    conversation_id=CONVERSATION,
                    source_project_id=PROJECT,
                    title="t",
                    created_by_id=USER,
                    message_count=0,
                ),
                BridgeDevice(
                    id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"
                ),
                WorkspaceBinding(
                    device_id=DEVICE,
                    workspace_id=LOCAL,
                    project_id=PROJECT,
                    label="repo",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
async def context(db: AsyncSession) -> IntegrationContext:
    owner = SimpleNamespace(id=USER, organization_id=ORG)
    req = await create_request(
        db,
        owner,
        GrantRequestCreate(
            project_id=PROJECT, device_id=DEVICE, scopes={"harness:execute"}
        ),
    )
    await decide_request(db, owner, req.id, True)
    issued = await exchange_request(db, owner, req.id)
    return await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )


def request(**kwargs: Any) -> AgentExecuteRequest:
    return AgentExecuteRequest(
        messages=[
            AgentMessage(role="user", content="hello", client_message_id=uuid4())
        ],
        execution_provider="codex",
        device_id=DEVICE,
        workspace_id=LOCAL,
        thread_id=str(THREAD),
        **kwargs,
    )


async def accept(db: AsyncSession, context: IntegrationContext, req: Any = None) -> Any:
    return await accept_submission(
        db,
        current_user=SimpleNamespace(id=USER, organization_id=ORG),
        request=req or request(),
        thread=await db.get(Thread, THREAD),
        integration_context=context,
    )


@pytest.fixture
async def external_run(db: AsyncSession, context: IntegrationContext) -> Any:
    accepted = await accept(db, context)
    return SimpleNamespace(id=UUID(accepted.run_id))


async def test_unknown_preserves_writer(db: AsyncSession, external_run: Any) -> None:
    run = await record_observation(db, run_id=external_run.id, observation="unknown")
    assert run.status == "recovering"
    assert run.workspace_locked is True
    assert not JobStatus.RECOVERING.is_terminal


async def test_late_completion_releases_both_claims(
    db: AsyncSession, external_run: Any, context: IntegrationContext
) -> None:
    await record_observation(db, run_id=external_run.id, observation="unknown")
    done = await record_observation(db, run_id=external_run.id, observation="completed")
    assert done.status == "completed" and not done.workspace_locked
    late = await record_observation(db, run_id=external_run.id, observation="running")
    assert late.status == "completed" and not late.workspace_locked
    assert (await accept(db, context)).run_id != str(external_run.id)


async def test_recovery_blocks_second_thread_writer(
    db: AsyncSession, external_run: Any, context: IntegrationContext
) -> None:
    await record_observation(db, run_id=external_run.id, observation="unknown")
    with pytest.raises(ActiveRunConflict):
        await accept(db, context)


async def test_recovery_blocks_second_workspace_writer(
    db: AsyncSession, external_run: Any, context: IntegrationContext
) -> None:
    await record_observation(db, run_id=external_run.id, observation="unknown")
    other = Thread(
        id=uuid4(),
        conversation_id=CONVERSATION,
        source_project_id=PROJECT,
        title="other",
        created_by_id=USER,
    )
    db.add(other)
    await db.commit()
    req = request().model_copy(update={"thread_id": str(other.id)})
    with pytest.raises(ActiveRunConflict):
        await accept_submission(
            db,
            current_user=SimpleNamespace(id=USER, organization_id=ORG),
            request=req,
            thread=other,
            integration_context=context,
        )


async def test_cancel_intent_survives_recovery_and_native_completion(
    db: AsyncSession, external_run: Any
) -> None:
    from src.models.harness_session import HarnessCommand, HarnessReceipt

    connection = await db.connection()
    await connection.run_sync(HarnessCommand.__table__.create)
    await connection.run_sync(HarnessReceipt.__table__.create)
    await db.commit()
    # Trusted running evidence means a start may already have escaped.
    await record_observation(db, run_id=external_run.id, observation="running")
    await record_observation(db, run_id=external_run.id, observation="unknown")
    stopped = await request_run_cancellation(
        db,
        run_id=str(external_run.id),
        thread_id=THREAD,
        user_id=USER,
        organization_id=ORG,
        reason="user",
    )
    assert stopped is not None and stopped.claimed
    await db.commit()
    unknown = await record_observation(
        db, run_id=external_run.id, observation="unknown"
    )
    assert unknown.cancel_requested and unknown.workspace_locked
    running = await record_observation(
        db, run_id=external_run.id, observation="running"
    )
    assert running.status == "stopping" and running.cancel_requested
    done = await record_observation(db, run_id=external_run.id, observation="completed")
    assert (
        done.status == "completed"
        and done.cancel_requested
        and not done.workspace_locked
    )


async def test_binding_failure_rolls_back_entire_accept(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.harness import runs

    real = runs.bind_external_submission

    async def fail_after_bind(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        raise RuntimeError("injected binding failure")

    monkeypatch.setattr(runs, "bind_external_submission", fail_after_bind)
    with pytest.raises(RuntimeError, match="injected"):
        await accept(db, context)
    from src.models.harness_session import HarnessSession

    for model in [AgentRun, ChatMessage, AgentRunEvent, AgentOutbox, HarnessSession]:
        assert await db.scalar(select(func.count()).select_from(model)) == 0
    thread = await db.get(Thread, THREAD)
    assert thread is not None and thread.message_count == 0


@pytest.mark.parametrize("field", ["device_id", "workspace_id", "thread_id"])
def test_external_request_requires_binding(field: str) -> None:
    values = request().model_dump()
    values[field] = None
    with pytest.raises(ValidationError):
        AgentExecuteRequest(**values)


def test_default_provider_is_nous() -> None:
    assert (
        AgentExecuteRequest(
            messages=[AgentMessage(role="user", content="hello")]
        ).execution_provider
        == "nous"
    )


@pytest.mark.parametrize(
    "observation,status", [("failed", "failed"), ("interrupted", "cancelled")]
)
async def test_terminal_evidence_is_absorbing(
    db: AsyncSession, external_run: Any, observation: Any, status: str
) -> None:
    from src.models.harness_session import HarnessSession

    result = await record_observation(
        db, run_id=external_run.id, observation=observation
    )
    assert result.status == status and not result.workspace_locked
    later = await record_observation(
        db, run_id=external_run.id, observation="completed"
    )
    assert later.status == status
    session = await db.scalar(select(HarnessSession))
    assert session is not None and session.observation == observation
    assert await db.scalar(select(func.count()).select_from(AgentRunEvent)) == 2


@pytest.mark.parametrize(
    "field",
    ["user_id", "organization_id", "project_id", "thread_id", "run_id", "grant_id"],
)
async def test_forged_context_denied(
    db: AsyncSession, context: IntegrationContext, field: str
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context.model_copy(update={field: uuid4()}))
    assert await db.scalar(select(func.count()).select_from(AgentRun)) == 0


@pytest.mark.parametrize(
    "model,field,value",
    [
        (BridgeDevice, "user_id", uuid4()),
        (BridgeDevice, "organization_id", uuid4()),
        (BridgeDevice, "revoked_at", datetime.now(timezone.utc)),
        (BridgeDevice, "is_deleted", True),
        (WorkspaceBinding, "project_id", uuid4()),
        (WorkspaceBinding, "is_deleted", True),
        (Thread, "source_project_id", uuid4()),
        (Thread, "is_deleted", True),
        (Conversation, "is_deleted", True),
        (Workspace, "owner_id", uuid4()),
        (Workspace, "is_deleted", True),
        (Collection, "is_deleted", True),
        (Organization, "is_active", False),
        (User, "is_active", False),
        (IntegrationGrant, "revoked_at", datetime.now(timezone.utc)),
        (
            IntegrationGrant,
            "expires_at",
            datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
        (IntegrationGrant, "scopes", ["tools:read"]),
        (IntegrationGrantRequest, "consent_revoked_at", datetime.now(timezone.utc)),
    ],
)
async def test_current_authority_rechecked(
    db: AsyncSession, context: IntegrationContext, model: Any, field: str, value: Any
) -> None:
    await db.execute(update(model).values(**{field: value}))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context)
    assert await db.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_different_owned_device_cannot_use_grant(
    db: AsyncSession, context: IntegrationContext
) -> None:
    other = uuid4()
    db.add_all(
        [
            BridgeDevice(id=other, user_id=USER, organization_id=ORG, label="other"),
            WorkspaceBinding(
                device_id=other, workspace_id=LOCAL, project_id=PROJECT, label="repo"
            ),
        ]
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context, request().model_copy(update={"device_id": other}))


async def test_unregistered_workspace_denied(
    db: AsyncSession, context: IntegrationContext
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await accept(
            db, context, request().model_copy(update={"workspace_id": uuid4()})
        )


async def test_replay_rechecks_revoked_authority(
    db: AsyncSession, context: IntegrationContext
) -> None:
    req = request()
    first = await accept(db, context, req)
    assert (await accept(db, context, req)).run_id == first.run_id
    await db.execute(
        update(IntegrationGrant).values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context, req)


async def test_native_execution_and_projection_cannot_seize_external_run(
    db: AsyncSession, external_run: Any
) -> None:
    from src.services.agent.agent_run_service import claim_execution, upsert_run
    from src.services.agent.agent_submission_service import (
        fail_queued_submission,
        finalize_submission,
        mark_submission_dispatched,
    )

    run_id = str(external_run.id)
    assert (
        await claim_execution(db, run_id, lease_owner="native", lease_seconds=60)
        == "duplicate"
    )
    assert not await mark_submission_dispatched(
        db, run_id=run_id, outbox_id=None, organization_id=ORG, user_id=USER
    )
    assert not await fail_queued_submission(
        db,
        run_id=run_id,
        thread_id=THREAD,
        organization_id=ORG,
        user_id=USER,
        error_code="native",
        error="native",
        payload={"code": "native", "message": "native"},
    )
    await upsert_run(db, job_id=run_id, status=JobStatus.FAILED)
    assert not await finalize_submission(
        db, run_id=run_id, status=JobStatus.FAILED, organization_id=ORG
    )
    run = await db.get(AgentRun, run_id)
    assert run is not None
    await db.refresh(run)
    assert run.status == "queued" and run.lease_owner is None


async def test_sweeper_delegates_external_absence_without_reading_native_store(
    db: AsyncSession, external_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core import database
    from src.models.harness_session import HarnessSession
    from src.services.agent import job_store
    from src.tasks.agent_run_tasks import _sweep_stale_agent_runs

    await db.execute(
        update(AgentRun).values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
    )
    await db.commit()
    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    native_read = AsyncMock(
        side_effect=AssertionError("external runs must not consult native job store")
    )
    monkeypatch.setattr(job_store, "get_job_fresh", native_read)
    result = await _sweep_stale_agent_runs(lease_owner="sweeper")
    assert result == {
        "scanned": 1,
        "failed": 0,
        "repaired": 0,
        "skipped": 1,
        "cancelled": 0,
    }
    native_read.assert_not_called()
    db.expire_all()
    run = await db.get(AgentRun, str(external_run.id))
    session = await db.scalar(select(HarnessSession))
    assert run is not None and run.status == "recovering"
    assert session is not None and session.workspace_locked


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", uuid4()),
        ("organization_id", uuid4()),
        ("thread_id", None),
        ("status", "completed"),
        ("execution_provider", "codex"),
    ],
)
async def test_binding_checks_actual_run(
    db: AsyncSession, context: IntegrationContext, field: str, value: Any
) -> None:
    from src.services.harness.runs import bind_external_submission

    values: dict[str, Any] = dict(
        job_id=str(uuid4()),
        user_id=USER,
        organization_id=ORG,
        thread_id=THREAD,
        status="queued",
        execution_provider="nous",
    )
    values[field] = value
    db.add(AgentRun(**values))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await bind_external_submission(
            db,
            run_id=UUID(values["job_id"]),
            context=context,
            device_id=DEVICE,
            workspace_id=LOCAL,
        )
    await db.rollback()


async def test_database_enforces_recovering_thread_claim(
    db: AsyncSession, external_run: Any
) -> None:
    from sqlalchemy.exc import IntegrityError

    await record_observation(db, run_id=external_run.id, observation="unknown")
    db.add(AgentRun(job_id=str(uuid4()), thread_id=THREAD, status="queued"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_database_enforces_uncertain_workspace_claim(
    db: AsyncSession, external_run: Any, context: IntegrationContext
) -> None:
    from sqlalchemy.exc import IntegrityError

    from src.models.harness_session import HarnessSession

    await record_observation(db, run_id=external_run.id, observation="unknown")
    other = str(uuid4())
    db.add(AgentRun(job_id=other, status="queued"))
    await db.flush()
    db.add(
        HarnessSession(
            run_id=other,
            grant_id=context.grant_id,
            device_id=DEVICE,
            workspace_id=LOCAL,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_external_approval_pause_cannot_be_abandoned(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    from src.services.agent.agent_submission_service import abandon_awaiting_submission

    await db.execute(update(AgentRun).values(status="awaiting_confirmation"))
    await db.commit()
    abandoned = await abandon_awaiting_submission(
        db, thread_id=THREAD, organization_id=ORG, user_id=USER, reason="replacement"
    )
    assert abandoned is None, "external approval pause must retain its writer"
    with pytest.raises(ActiveRunConflict):
        await accept(db, context)
    run = await db.get(AgentRun, str(external_run.id))
    assert run is not None and run.status == "awaiting_confirmation"


@pytest.mark.parametrize("target", ["thread", "workspace", "provider"])
async def test_replay_cannot_change_execution_binding(
    db: AsyncSession, context: IntegrationContext, target: str
) -> None:
    req = request()
    first = await accept(db, context, req)
    other_thread = Thread(
        id=uuid4(),
        conversation_id=CONVERSATION,
        source_project_id=PROJECT,
        title="other",
        created_by_id=USER,
    )
    other_workspace = uuid4()
    db.add_all(
        [
            other_thread,
            WorkspaceBinding(
                device_id=DEVICE,
                workspace_id=other_workspace,
                project_id=PROJECT,
                label="other",
            ),
        ]
    )
    await db.commit()
    updates: dict[str, Any] = (
        {"thread_id": str(other_thread.id)}
        if target == "thread"
        else (
            {"workspace_id": other_workspace}
            if target == "workspace"
            else {"execution_provider": "nous"}
        )
    )
    changed = req.model_copy(update=updates)
    with pytest.raises(ActiveRunConflict):
        await accept_submission(
            db,
            current_user=SimpleNamespace(id=USER, organization_id=ORG),
            request=changed,
            thread=other_thread if target == "thread" else await db.get(Thread, THREAD),
            integration_context=context,
        )
    assert await db.scalar(select(func.count()).select_from(AgentRun)) == 1


async def test_replay_honors_run_scoped_context(
    db: AsyncSession, context: IntegrationContext
) -> None:
    req = request()
    first = await accept(db, context, req)
    await record_observation(db, run_id=UUID(first.run_id), observation="completed")
    second = await accept(db, context)
    await db.execute(
        update(IntegrationGrant).values(thread_id=THREAD, run_id=second.run_id)
    )
    await db.commit()
    bound = context.model_copy(
        update={"thread_id": THREAD, "run_id": UUID(second.run_id)}
    )
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, bound, req)


async def test_read_only_grant_cannot_execute(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await db.execute(update(IntegrationGrant).values(scopes=["tools:read"]))
    await db.execute(update(IntegrationGrantRequest).values(scopes=["tools:read"]))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context)
