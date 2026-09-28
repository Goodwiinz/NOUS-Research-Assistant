"""Exact callback ownership and one-time native decisions."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.schemas.harness import BridgeEvent
from src.schemas.integration_context import IntegrationContext
from src.services.harness.approvals import (
    NativeDecision,
    NativeRequestConflict,
    ingest_native_request,
    ingest_native_response_ack,
    resolve_native_request,
)
from tests.unit.services.harness.test_delivery import (
    DEVICE,
    LOCAL,
    context,
    db,
    external_run,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
async def approval_table(db: AsyncSession) -> None:
    connection = await db.connection()
    models: tuple[Any, ...] = (HarnessCommand, HarnessReceipt, HarnessNativeRequest)
    for model in models:
        await connection.run_sync(model.__table__.create)
    await db.commit()


@pytest.fixture
async def live_command(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> Any:
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == str(external_run.id))
    )
    assert session is not None
    run = await db.get(AgentRun, str(external_run.id))
    assert run is not None
    run.status = "running"
    session.provider_session_id = "native-session"
    session.provider_turn_id = "native-turn"
    session.observation = "running"
    command = HarnessCommand(
        id=uuid4(),
        run_id=str(external_run.id),
        generation=session.generation,
        kind="start",
        body={"kind": "start", "input": "hello"},
        expires_at=datetime.now(timezone.utc),
    )
    db.add(command)
    await db.commit()
    return command


@pytest.fixture
async def challenge(
    db: AsyncSession,
    context: IntegrationContext,
    live_command: Any,
) -> UUID:
    values = {
        "deviceId": DEVICE,
        "runId": UUID(live_command.run_id),
        "commandId": live_command.id,
        "workspaceId": LOCAL,
        "generation": live_command.generation,
        "sourceId": "approval-source",
        "sourceSeq": 1,
        "body": {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-1",
            "requestId": 7,
            "approvalId": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-1",
                "approvalId": "approval-1",
                "command": "pytest -q",
                "cwd": "/workspace",
            },
        },
    }
    event = BridgeEvent.model_validate(values)
    return await ingest_native_request(db, context, event)


async def test_duplicate_decision_rejected(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    await resolve_native_request(db, context, challenge, NativeDecision.deny())
    with pytest.raises(NativeRequestConflict):
        await resolve_native_request(db, context, challenge, NativeDecision.allow())


async def test_decision_creates_one_exact_response_command(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    await resolve_native_request(db, context, challenge, NativeDecision.allow())
    row = await db.scalar(
        select(HarnessNativeRequest).where(HarnessNativeRequest.id == challenge)
    )
    assert row is not None and row.consumed_at is not None
    assert row.decision == {"kind": "decision", "allow": True}
    assert row.response_command_id is not None


async def test_response_redelivery_does_not_reopen_browser_decision(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    await resolve_native_request(db, context, challenge, NativeDecision.allow())
    request = await db.get(HarnessNativeRequest, challenge)
    assert request is not None and request.response_command_id is not None
    command = await db.get(HarnessCommand, request.response_command_id)
    assert command is not None
    event = BridgeEvent.model_validate(
        {
            "deviceId": request.device_id,
            "runId": UUID(request.run_id),
            "commandId": command.id,
            "workspaceId": request.workspace_id,
            "generation": request.generation,
            "sourceId": "approval-source",
            "sourceSeq": 2,
            "body": {"kind": "command_ack", "approvalRecordId": str(request.id)},
        }
    )
    seq = await ingest_native_response_ack(db, context, event)
    assert await ingest_native_response_ack(db, context, event) == seq
    command = await db.get(HarnessCommand, command.id)
    assert command is not None and command.acknowledged
    with pytest.raises(NativeRequestConflict):
        await resolve_native_request(db, context, challenge, NativeDecision.allow())


async def test_same_item_can_have_distinct_typed_callback_ids(
    db: AsyncSession,
    context: IntegrationContext,
    live_command: Any,
    challenge: UUID,
) -> None:
    values = {
        "deviceId": DEVICE,
        "runId": UUID(live_command.run_id),
        "commandId": live_command.id,
        "workspaceId": LOCAL,
        "generation": live_command.generation,
        "sourceId": "approval-source",
        "sourceSeq": 2,
        "body": {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-1",
            "requestId": "7",
            "approvalId": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-1",
                "approvalId": "approval-1",
                "command": "git status --short",
                "cwd": "/workspace",
            },
        },
    }
    other = await ingest_native_request(db, context, BridgeEvent.model_validate(values))
    assert other != challenge
    first = await db.get(HarnessNativeRequest, challenge)
    second = await db.get(HarnessNativeRequest, other)
    assert first is not None and second is not None
    assert first.request_id_type == "number" and first.request_id_value == "7"
    assert second.request_id_type == "string" and second.request_id_value == "7"


async def test_displayed_target_hash_is_required_to_match(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    with pytest.raises(NativeRequestConflict, match="target changed"):
        await resolve_native_request(
            db,
            context,
            challenge,
            NativeDecision.allow(target_hash="0" * 64),
        )


async def test_reconnect_invalidates_old_callback(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    request = await db.get(HarnessNativeRequest, challenge)
    assert request is not None
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == request.run_id)
    )
    assert session is not None
    session.generation += 1
    await db.commit()
    with pytest.raises(NativeRequestConflict):
        await resolve_native_request(db, context, challenge, NativeDecision.allow())


async def test_wrong_actor_cannot_decide(
    db: AsyncSession, context: IntegrationContext, challenge: UUID
) -> None:
    wrong_owner = context.model_copy(update={"user_id": uuid4()})
    with pytest.raises(PermissionError):
        await resolve_native_request(db, wrong_owner, challenge, NativeDecision.allow())


async def test_unknown_native_request_kind_rejected(
    db: AsyncSession, context: IntegrationContext, live_command: Any
) -> None:
    values = {
        "deviceId": DEVICE,
        "runId": UUID(live_command.run_id),
        "commandId": live_command.id,
        "workspaceId": LOCAL,
        "generation": live_command.generation,
        "sourceId": "approval-source",
        "sourceSeq": 1,
        "body": {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-1",
            "requestId": 8,
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-1",
            },
        },
    }
    with pytest.raises(ValueError, match="unsupported"):
        await ingest_native_request(db, context, BridgeEvent.model_validate(values))


@pytest.mark.parametrize("grant_root", ["/outside-workspace", ""])
async def test_file_approval_cannot_expand_workspace(
    db: AsyncSession,
    context: IntegrationContext,
    live_command: Any,
    grant_root: str,
) -> None:
    values = {
        "deviceId": DEVICE,
        "runId": UUID(live_command.run_id),
        "commandId": live_command.id,
        "workspaceId": LOCAL,
        "generation": live_command.generation,
        "sourceId": "approval-source",
        "sourceSeq": 1,
        "body": {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-file",
            "requestId": 9,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-file",
                "grantRoot": grant_root,
            },
        },
    }
    with pytest.raises(ValueError, match="unsupported file approval fields"):
        await ingest_native_request(db, context, BridgeEvent.model_validate(values))


async def test_workspace_local_file_approval_remains_supported(
    db: AsyncSession,
    context: IntegrationContext,
    live_command: Any,
) -> None:
    values = {
        "deviceId": DEVICE,
        "runId": UUID(live_command.run_id),
        "commandId": live_command.id,
        "workspaceId": LOCAL,
        "generation": live_command.generation,
        "sourceId": "approval-source",
        "sourceSeq": 1,
        "body": {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-file",
            "requestId": 9,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-file",
            },
        },
    }
    request_id = await ingest_native_request(
        db, context, BridgeEvent.model_validate(values)
    )
    request = await db.get(HarnessNativeRequest, request_id)
    assert request is not None
    assert request.method == "item/fileChange/requestApproval"
