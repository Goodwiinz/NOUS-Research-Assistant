"""Bridge delivery replay and authority regressions."""

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice
from src.models.harness_session import HarnessCommand, HarnessReceipt, HarnessSession
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.schemas.harness import BridgeEvent, ProducerEvent, Start
from src.schemas.integration_context import IntegrationContext
from src.services.agent.agent_submission_service import (
    finalize_submission,
    request_run_cancellation,
)
from src.services.harness.delivery import (
    dispatch_pending,
    ingest_bridge_event,
    lease_commands,
    reconcile_pending,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    renew_grant,
    resolve_integration_context,
)
from src.shared.enums import AgentOutboxStatus, JobStatus
from tests.unit.services.harness.test_runs import (  # noqa: F401
    DEVICE,
    LOCAL,
    ORG,
    THREAD,
    USER,
    context,
    db,
    external_run,
)

pytestmark = pytest.mark.unit


async def test_reject_unknown_command(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    event = BridgeEvent(
        deviceId=DEVICE,
        workspaceId=LOCAL,
        runId=external_run.id,
        commandId=uuid4(),
        generation=1,
        sourceId="source",
        sourceSeq=1,
        body=ProducerEvent.model_validate(
            {
                "kind": "event",
                "eventType": "assistant.delta",
                "payload": {"text": "hello"},
            }
        ),
    )
    with pytest.raises(PermissionError):
        await ingest_bridge_event(db, context, event)


@pytest.fixture(autouse=True)
async def delivery_tables(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = await db.connection()
    models: list[Any] = [HarnessCommand, HarnessReceipt]
    for model in models:
        await connection.run_sync(model.__table__.create)
    await db.commit()
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)


@pytest.fixture
async def command(
    db: AsyncSession, external_run: Any, context: IntegrationContext
) -> Any:
    assert await dispatch_pending(db) == 1
    commands = await lease_commands(db, context, DEVICE)
    assert len(commands) == 1
    return commands[0]


def event(command: Any, seq: int = 1, body: Any = None, **changes: Any) -> BridgeEvent:
    values = command.model_dump(exclude={"expiresAt", "body"})
    values.update(
        sourceId="stable-source",
        sourceSeq=seq,
        body=body
        or {
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"text": "answer"},
        },
    )
    values.update(changes)
    return cast(BridgeEvent, BridgeEvent.model_validate(values))


async def test_duplicate_is_same_canonical_sequence_after_restart(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    value = event(command)
    seq = await ingest_bridge_event(db, context, value)
    assert seq == 2
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as restarted:
        assert await ingest_bridge_event(restarted, context, value) == seq
    assert await db.scalar(select(func.count()).select_from(AgentRunEvent)) == 2
    assert await db.scalar(select(func.count()).select_from(HarnessReceipt)) == 1


async def test_duplicate_conflicting_payload_is_rejected(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    await ingest_bridge_event(db, context, event(command))
    conflict = event(
        command,
        body={
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"text": "different"},
        },
    )
    with pytest.raises(ValueError, match="conflicting"):
        await ingest_bridge_event(db, context, conflict)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 2),
        ("deviceId", uuid4()),
        ("workspaceId", uuid4()),
        ("commandId", uuid4()),
        ("runId", uuid4()),
    ],
)
async def test_envelope_binding(
    db: AsyncSession, context: IntegrationContext, command: Any, field: str, value: Any
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, context, event(command, **{field: value}))


@pytest.mark.parametrize(
    "field",
    ["user_id", "organization_id", "project_id", "thread_id", "run_id", "grant_id"],
)
async def test_context_binding(
    db: AsyncSession, context: IntegrationContext, command: Any, field: str
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(
            db, context.model_copy(update={field: uuid4()}), event(command)
        )


@pytest.mark.parametrize(
    "model,values",
    [
        (
            IntegrationGrant,
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        ),
        (IntegrationGrant, {"revoked_at": datetime.now(timezone.utc)}),
        (IntegrationGrant, {"scopes": ["tools:read"]}),
        (IntegrationGrantRequest, {"consent_revoked_at": datetime.now(timezone.utc)}),
        (BridgeDevice, {"revoked_at": datetime.now(timezone.utc)}),
    ],
)
async def test_authority_rechecked_for_replay_and_poll(
    db: AsyncSession, context: IntegrationContext, command: Any, model: Any, values: Any
) -> None:
    value = event(command)
    await ingest_bridge_event(db, context, value)
    await db.execute(update(model).values(**values))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, context, value)
    with pytest.raises(IntegrationAccessDenied):
        await lease_commands(db, context, DEVICE)


async def test_gaps_and_changed_source_do_not_advance_cursor(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    with pytest.raises(ValueError, match="gap"):
        await ingest_bridge_event(db, context, event(command, 2))
    await ingest_bridge_event(db, context, event(command))
    with pytest.raises(ValueError, match="source"):
        await ingest_bridge_event(db, context, event(command, 2, sourceId="new"))
    assert await ingest_bridge_event(db, context, event(command, 2)) == 3


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "event", "eventType": "run.completed", "payload": {}},
        {
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"unknown": "bad"},
        },
        {
            "kind": "event",
            "eventType": "tool.started",
            "payload": {"tool_call_id": "x", "name": "n", "secret": "bad"},
        },
        {
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"text": "\\" * 17000},
        },
    ],
)
def test_only_bounded_typed_producer_payloads(body: Any) -> None:
    from src.schemas.harness import BridgeCommand

    c = BridgeCommand(
        deviceId=uuid4(),
        runId=uuid4(),
        commandId=uuid4(),
        workspaceId=uuid4(),
        generation=1,
        expiresAt=datetime.now(timezone.utc),
        body=Start(kind="start", input="test"),
    )
    with pytest.raises(ValidationError):
        event(c, body=body)


async def test_dispatch_is_idempotent_and_leased(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    assert await dispatch_pending(db) == 0
    assert await lease_commands(db, context, DEVICE) == []
    await db.execute(
        update(HarnessCommand).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    replay = await lease_commands(db, context, DEVICE)
    assert (
        replay[0].commandId == command.commandId
        and replay[0].generation == command.generation
    )


async def test_disabled_only_prevents_dispatch(
    db: AsyncSession,
    context: IntegrationContext,
    external_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    assert await dispatch_pending(db) == 0
    assert await db.scalar(select(func.count()).select_from(HarnessCommand)) == 0


async def test_renewal_preserves_command_identity(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    from types import SimpleNamespace

    issued = await renew_grant(
        db, SimpleNamespace(id=USER, organization_id=ORG), context.grant_id
    )
    renewed = await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )
    await db.execute(update(HarnessCommand).values(lease_until=None))
    await db.commit()
    commands = await lease_commands(db, renewed, DEVICE)
    assert commands[0].commandId == command.commandId
    assert await ingest_bridge_event(db, renewed, event(command)) == 2


async def running(db: AsyncSession, context: IntegrationContext, command: Any) -> None:
    await ingest_bridge_event(
        db,
        context,
        event(
            command,
            body={
                "kind": "observation",
                "state": "running",
                "sessionId": "native",
                "turnId": "turn",
            },
        ),
    )


async def cancel(db: AsyncSession, run_id: str) -> None:
    await request_run_cancellation(
        db,
        run_id=run_id,
        thread_id=THREAD,
        user_id=USER,
        organization_id=ORG,
        reason="user",
    )
    await db.commit()


async def test_queued_cancellation_before_dispatch_releases_workspace(
    db: AsyncSession,
    context: IntegrationContext,
    external_run: Any,
) -> None:
    await cancel(db, str(external_run.id))

    assert await dispatch_pending(db) == 0

    run = await db.get(AgentRun, str(external_run.id))
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == str(external_run.id))
    )
    outbox = await db.scalar(
        select(AgentOutbox).where(AgentOutbox.run_id == str(external_run.id))
    )
    terminal_event = await db.scalar(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == str(external_run.id))
        .order_by(AgentRunEvent.seq.desc())
    )
    assert run is not None and run.status == JobStatus.CANCELLED.value
    assert session is not None and not session.workspace_locked
    assert outbox is not None and outbox.status == AgentOutboxStatus.FAILED.value
    assert terminal_event is not None and terminal_event.event_type == "run.cancelled"


@pytest.mark.parametrize("failure", ["revoked_grant", "missing_message"])
async def test_undeliverable_outbox_fails_run_and_releases_workspace(
    db: AsyncSession,
    context: IntegrationContext,
    external_run: Any,
    failure: str,
) -> None:
    run = await db.get(AgentRun, str(external_run.id))
    assert run is not None
    if failure == "revoked_grant":
        grant = await db.get(IntegrationGrant, context.grant_id)
        assert grant is not None
        grant.revoked_at = datetime.now(timezone.utc)
    else:
        setattr(run, "user_message_id", None)
    await db.commit()

    assert await dispatch_pending(db) == 0

    db.expire_all()
    run = await db.get(AgentRun, str(external_run.id))
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == str(external_run.id))
    )
    outbox = await db.scalar(
        select(AgentOutbox).where(AgentOutbox.run_id == str(external_run.id))
    )
    terminal_event = await db.scalar(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == str(external_run.id))
        .order_by(AgentRunEvent.seq.desc())
    )
    assert run is not None and run.status == JobStatus.FAILED.value
    assert run.error_code == "harness_dispatch_failed"
    assert session is not None and not session.workspace_locked
    assert outbox is not None and outbox.status == AgentOutboxStatus.FAILED.value
    assert terminal_event is not None and terminal_event.event_type == "run.failed"


@pytest.mark.parametrize(
    "outcome,status", [("completed", "completed"), ("interrupted", "cancelled")]
)
async def test_cancel_race_requires_terminal_and_projects(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    status: str,
) -> None:
    from src.services.agent import agent_execution_service as execution

    persist = AsyncMock(return_value=str(uuid4()))
    monkeypatch.setattr(execution, "_persist_assistant_message_safe", persist)
    await running(db, context, command)
    await ingest_bridge_event(db, context, event(command, 2))
    await cancel(db, str(command.runId))
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    stop = (await lease_commands(db, context, DEVICE))[0]
    assert (
        stop.body.kind == "interrupt"
        and stop.body.sessionId == "native"
        and stop.body.turnId == "turn"
    )
    session = await db.scalar(select(HarnessSession))
    assert session is not None and session.workspace_locked
    seq = await ingest_bridge_event(
        db,
        context,
        event(
            command,
            3,
            body={
                "kind": "observation",
                "state": outcome,
                "sessionId": "native",
                "turnId": "turn",
            },
        ),
    )
    assert seq == 5
    persist.assert_awaited_once_with(
        thread_id=str(THREAD),
        content="answer",
        model_name="codex",
        tool_executions_out=None,
        stopped=outcome == "interrupted",
        required=True,
        client_message_id=str(command.runId),
    )
    db.expire_all()
    run = await db.get(AgentRun, str(command.runId))
    session = await db.scalar(select(HarnessSession))
    assert (
        run is not None
        and run.status == status
        and run.assistant_message_id is not None
    )
    assert session is not None and not session.workspace_locked


async def test_failed_turn_without_output_projects_non_empty_assistant_text(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that fails before any delta must not persist an empty assistant
    row: ChatMessageResponse requires content, so the thread would 500 on read."""
    from src.services.agent import agent_execution_service as execution

    persist = AsyncMock(return_value=str(uuid4()))
    monkeypatch.setattr(execution, "_persist_assistant_message_safe", persist)
    await running(db, context, command)
    await ingest_bridge_event(
        db,
        context,
        event(
            command,
            2,
            body={
                "kind": "observation",
                "state": "failed",
                "sessionId": "native",
                "turnId": "turn",
            },
        ),
    )
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["content"] == "External execution failed."
    db.expire_all()
    run = await db.get(AgentRun, str(command.runId))
    assert run is not None and run.status == "failed"


async def test_projection_failure_retains_ownership_and_retries_after_restart(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution

    persist = AsyncMock(side_effect=RuntimeError("database lost"))
    monkeypatch.setattr(execution, "_persist_assistant_message_safe", persist)
    await running(db, context, command)
    value = event(
        command,
        2,
        body={
            "kind": "observation",
            "state": "completed",
            "sessionId": "native",
            "turnId": "turn",
        },
    )
    with pytest.raises(RuntimeError, match="database lost"):
        await ingest_bridge_event(db, context, value)
    session = await db.scalar(select(HarnessSession))
    assert session is not None and session.workspace_locked
    assert await db.scalar(select(func.count()).select_from(AgentRunEvent)) == 2
    await db.commit()
    persist.side_effect = None
    persist.return_value = str(uuid4())
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as restarted:
        assert await reconcile_pending(restarted) == 1
        assert await ingest_bridge_event(restarted, context, value) == 3
    assert persist.await_count == 2


async def test_wrong_native_turn_and_post_terminal_events_fail_closed(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_persist_assistant_message_safe",
        AsyncMock(return_value=str(uuid4())),
    )
    await running(db, context, command)
    with pytest.raises(ValueError, match="identity"):
        await ingest_bridge_event(
            db,
            context,
            event(
                command,
                2,
                body={
                    "kind": "observation",
                    "state": "completed",
                    "sessionId": "native",
                    "turnId": "foreign",
                },
            ),
        )
    value = event(
        command,
        2,
        body={
            "kind": "observation",
            "state": "completed",
            "sessionId": "native",
            "turnId": "turn",
        },
    )
    seq = await ingest_bridge_event(db, context, value)
    assert await ingest_bridge_event(db, context, value) == seq
    with pytest.raises(ValueError, match="terminal"):
        await ingest_bridge_event(db, context, event(command, 3))


async def test_native_default_cannot_finalize_codex(
    db: AsyncSession, command: Any
) -> None:
    assert not await finalize_submission(
        db, run_id=str(command.runId), status=JobStatus.COMPLETED, organization_id=ORG
    )
    run = await db.get(AgentRun, str(command.runId))
    assert run is not None and run.status == "queued"


async def test_real_assistant_projection_is_idempotent(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.models.chat_message import ChatMessage
    from src.services.agent import agent_execution_service as execution

    monkeypatch.setattr(
        execution,
        "AsyncSessionLocal",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    await running(db, context, command)
    await ingest_bridge_event(db, context, event(command, 2))
    value = event(
        command,
        3,
        body={
            "kind": "observation",
            "state": "completed",
            "sessionId": "native",
            "turnId": "turn",
        },
    )
    seq = await ingest_bridge_event(db, context, value)
    assert await ingest_bridge_event(db, context, value) == seq
    rows = (
        await db.scalars(
            select(ChatMessage).where(
                ChatMessage.client_message_id == str(command.runId)
            )
        )
    ).all()
    assert len(rows) == 1 and rows[0].content == "answer"


async def test_websocket_reauthenticates_every_frame_and_ack_follows_commit(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from types import SimpleNamespace

    from fastapi import WebSocketDisconnect

    from src.api import harness
    from src.core.websocket_auth import WebSocketAuthenticator

    monkeypatch.setattr(
        harness,
        "AsyncSessionLocal",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    auth = AsyncMock(return_value={"sub": str(USER)})
    monkeypatch.setattr(WebSocketAuthenticator, "authenticate", auth)
    resolve = AsyncMock(return_value=context)
    monkeypatch.setattr(harness, "resolve_integration_context", resolve)

    class Socket:
        url = SimpleNamespace(scheme="wss")
        headers = {"x-nous-integration-grant": "opaque"}
        frames = 0
        acks: list[Any] = []

        async def accept(self, **kwargs: Any) -> None:
            pass

        async def receive_text(self) -> str:
            self.frames += 1
            if self.frames > 2:
                raise WebSocketDisconnect()
            if self.frames == 1:
                return json.dumps({"poll": True, "deviceId": str(DEVICE)})
            return str(event(command).model_dump_json())

        async def send_json(self, value: Any) -> None:
            if "lease" in value:
                assert value["lease"]["runs"] == {
                    str(command.runId): command.generation
                }
                return
            # A separate database session can already see the receipt on ACK.
            async with async_sessionmaker(db.bind, expire_on_commit=False)() as check:
                assert (
                    await check.scalar(select(func.count()).select_from(HarnessReceipt))
                    == 1
                )
            self.acks.append(value)

        async def close(self, **kwargs: Any) -> None:
            raise AssertionError(kwargs)

    socket = Socket()
    await harness.connect(socket)  # type: ignore[arg-type]
    assert auth.await_count == 3 and resolve.await_count == 3
    assert socket.acks[0]["ack"]["canonicalSeq"] == 2


async def test_sealed_projection_rejects_more_output_before_retry(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution

    monkeypatch.setattr(
        execution,
        "_persist_assistant_message_safe",
        AsyncMock(side_effect=RuntimeError("projection failed")),
    )
    value = event(
        command,
        body={
            "kind": "observation",
            "state": "completed",
            "sessionId": "native",
            "turnId": "turn",
        },
    )
    with pytest.raises(RuntimeError):
        await ingest_bridge_event(db, context, value)
    with pytest.raises(ValueError, match="terminal"):
        await ingest_bridge_event(db, context, event(command, 2))


async def test_sweeper_unknown_cannot_erase_committed_terminal_evidence(
    db: AsyncSession,
    context: IntegrationContext,
    command: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution
    from src.services.harness.runs import record_observation

    persist = AsyncMock(side_effect=RuntimeError("projection failed"))
    monkeypatch.setattr(execution, "_persist_assistant_message_safe", persist)
    value = event(
        command,
        body={
            "kind": "observation",
            "state": "completed",
            "sessionId": "native",
            "turnId": "turn",
        },
    )
    with pytest.raises(RuntimeError):
        await ingest_bridge_event(db, context, value)
    await record_observation(db, run_id=command.runId, observation="unknown")
    session = await db.scalar(select(HarnessSession))
    assert session is not None and session.observation == "completed"
    persist.side_effect = None
    persist.return_value = str(uuid4())
    assert await reconcile_pending(db) == 1


async def test_lease_renewal_is_limited_to_authorized_run_scope(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    from src.models.bridge_device import WorkspaceBinding
    from src.models.thread import Thread
    from src.services.harness.delivery import lease_runs
    from tests.unit.services.harness.test_runs import CONVERSATION, PROJECT, request

    other_thread, other_workspace = uuid4(), uuid4()
    db.add_all(
        [
            Thread(
                id=other_thread,
                conversation_id=CONVERSATION,
                source_project_id=PROJECT,
                title="other",
                created_by_id=USER,
            ),
            WorkspaceBinding(
                device_id=DEVICE,
                workspace_id=other_workspace,
                project_id=PROJECT,
                label="other",
            ),
        ]
    )
    await db.commit()
    req = request().model_copy(
        update={"thread_id": str(other_thread), "workspace_id": other_workspace}
    )
    # accept() binds the baseline thread; use the submission service for this one.
    from types import SimpleNamespace

    from src.services.agent.agent_submission_service import accept_submission

    other = await accept_submission(
        db,
        current_user=SimpleNamespace(id=USER, organization_id=ORG),
        request=req,
        thread=await db.get(Thread, other_thread),
        integration_context=context,
    )
    assert set(await lease_runs(db, context, DEVICE)) == {
        str(command.runId),
        other.run_id,
    }
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == context.grant_id)
        .values(run_id=str(command.runId), thread_id=THREAD)
    )
    await db.commit()
    scoped = context.model_copy(update={"run_id": command.runId, "thread_id": THREAD})
    assert await lease_runs(db, scoped, DEVICE) == {
        str(command.runId): command.generation
    }


async def test_poll_skips_session_that_fails_authorization(
    db: AsyncSession, context: IntegrationContext, command: Any
) -> None:
    from types import SimpleNamespace

    from src.models.bridge_device import WorkspaceBinding
    from src.models.thread import Thread
    from src.services.agent.agent_submission_service import accept_submission
    from src.services.harness.delivery import lease_runs
    from tests.unit.services.harness.test_runs import CONVERSATION, PROJECT, request

    other_thread, other_workspace = uuid4(), uuid4()
    db.add_all(
        [
            Thread(
                id=other_thread,
                conversation_id=CONVERSATION,
                source_project_id=PROJECT,
                title="stale",
                created_by_id=USER,
            ),
            WorkspaceBinding(
                device_id=DEVICE,
                workspace_id=other_workspace,
                project_id=PROJECT,
                label="stale",
            ),
        ]
    )
    await db.commit()
    stale = await accept_submission(
        db,
        current_user=SimpleNamespace(id=USER, organization_id=ORG),
        request=request().model_copy(
            update={"thread_id": str(other_thread), "workspace_id": other_workspace}
        ),
        thread=await db.get(Thread, other_thread),
        integration_context=context,
    )
    # A UI flow dropping the thread's project makes its session unauthorizable.
    await db.execute(
        update(Thread).where(Thread.id == other_thread).values(source_project_id=None)
    )
    await db.execute(
        update(HarnessCommand).values(lease_until=None, acknowledged=False)
    )
    await db.commit()
    assert [c.runId for c in await lease_commands(db, context, DEVICE)] == [
        command.runId
    ]
    assert await lease_runs(db, context, DEVICE) == {
        str(command.runId): command.generation
    }
    assert stale.run_id not in await lease_runs(db, context, DEVICE)


async def test_chat_bound_grant_leases_only_runs_in_its_chat(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    """A device connected with --chat runs harness work only in that chat."""
    from src.models.thread import Thread
    from tests.unit.services.harness.test_runs import CONVERSATION, PROJECT

    other_chat = uuid4()
    db.add(
        Thread(
            id=other_chat,
            conversation_id=CONVERSATION,
            source_project_id=PROJECT,
            title="other chat",
            created_by_id=USER,
        )
    )
    await db.commit()
    assert await dispatch_pending(db) == 1  # the run lives in THREAD

    async def bind(thread_id: Any) -> IntegrationContext:
        await db.execute(
            update(IntegrationGrant)
            .where(IntegrationGrant.id == context.grant_id)
            .values(thread_id=thread_id)
        )
        await db.commit()
        return context.model_copy(update={"thread_id": thread_id})

    assert await lease_commands(db, await bind(other_chat), DEVICE) == []
    leased = await lease_commands(db, await bind(THREAD), DEVICE)
    assert [c.runId for c in leased] == [external_run.id]
