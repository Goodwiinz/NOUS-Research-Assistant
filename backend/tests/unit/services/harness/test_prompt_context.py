"""Canonical bounded Codex context and immutable first delivery."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_run import AgentRun
from src.models.chat_message import ChatMessage, MessageRole
from src.models.harness_session import HarnessCommand
from src.schemas.harness import Start
from src.schemas.integration_context import IntegrationContext
from src.services.harness.delivery import dispatch_pending, lease_commands
from tests.unit.services.harness.test_delivery import delivery_tables  # noqa: F401
from tests.unit.services.harness.test_runs import (  # noqa: F401
    DEVICE,
    THREAD,
    USER,
    accept,
    context,
    db,
    external_run,
    request,
)

pytestmark = pytest.mark.unit


async def history(db: AsyncSession, content: str, **kwargs: Any) -> ChatMessage:
    values = dict(
        thread_id=THREAD,
        user_id=USER,
        role=MessageRole.ASSISTANT,
        content=content,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    values.update(kwargs)
    row = ChatMessage(**values)
    db.add(row)
    await db.commit()
    return row


async def test_second_codex_turn_receives_existing_conversation(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    await history(db, "The chosen label is violet-otter.")
    assert await dispatch_pending(db) == 1
    command = (await lease_commands(db, context, DEVICE))[0]
    assert isinstance(command.body, Start)
    assert "violet-otter" in command.body.input
    assert command.body.input.count("hello") == 1
    assert command.body.sessionId is None
    run: Any = await db.get(AgentRun, str(external_run.id))
    assert run.run_metadata["harness_prompt"]["source_digest"]


@pytest.mark.parametrize("budget", ["messages", "tokens", "wire"])
async def test_prompt_bounds_preserve_latest_message(
    db: AsyncSession,
    context: IntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    from src.services.harness.prompt_context import build_harness_prompt

    await history(db, "old fact " * 50, token_count=0)
    if budget == "messages":
        monkeypatch.setattr(settings, "THREAD_DEFAULT_MAX_MESSAGES", 1)
    if budget == "tokens":
        monkeypatch.setattr(settings, "THREAD_DEFAULT_MAX_TOKENS", 10)
    req = request()
    req.messages[-1].content = "界" * 65000 if budget == "wire" else "current question"
    accepted = await accept(db, context, req)
    prompt = await build_harness_prompt(db, run_id=accepted.run_id)
    assert req.messages[-1].content in prompt.input
    assert len(prompt.input) <= 65536
    assert prompt.history_truncated
    assert "old fact" not in prompt.input


async def test_prompt_scope_and_visible_messages(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    from src.services.harness.prompt_context import build_harness_prompt

    await history(db, "visible fact")
    await history(db, "deleted secret", is_deleted=True)
    await history(db, "superseded secret", superseded_by_message_id=uuid4())
    await history(db, "system secret", role=MessageRole.SYSTEM)
    await history(db, "foreign secret", thread_id=uuid4())
    await history(
        db, "later answer", created_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    prompt = await build_harness_prompt(db, run_id=str(external_run.id))
    assert "visible fact" in prompt.input
    assert "secret" not in prompt.input and "later answer" not in prompt.input


async def test_edit_before_first_lease_retires_stale_prompt(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    from src.models.harness_session import HarnessSession

    fact = await history(db, "prior fact")
    assert await dispatch_pending(db) == 1
    await db.execute(
        update(ChatMessage).where(ChatMessage.id == fact.id).values(is_deleted=True)
    )
    await db.commit()
    assert await lease_commands(db, context, DEVICE) == []
    db.expire_all()
    run: Any = await db.get(AgentRun, str(external_run.id))
    session = await db.scalar(select(HarnessSession))
    assert session is not None
    assert run.status == "cancelled" and not session.workspace_locked


async def test_codex_retry_preserves_prompt_identity(
    db: AsyncSession, context: IntegrationContext
) -> None:
    req = request()
    accepted = await accept(db, context, req)
    await history(db, "stable prior context")
    assert await dispatch_pending(db) == 1
    first = (await lease_commands(db, context, DEVICE))[0]
    run: Any = await db.get(AgentRun, accepted.run_id)
    provenance = run.run_metadata["harness_prompt"]
    assert (await accept(db, context, req)).run_id == accepted.run_id
    assert await dispatch_pending(db) == 0
    await db.execute(
        update(HarnessCommand).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    replay = (await lease_commands(db, context, DEVICE))[0]
    assert replay.commandId == first.commandId and replay.body == first.body
    assert run.run_metadata["harness_prompt"] == provenance


@pytest.mark.parametrize("regenerate", [False, True])
async def test_codex_edit_resend_excludes_superseded_answer(
    db: AsyncSession, context: IntegrationContext, regenerate: bool
) -> None:
    from src.services.harness.runs import record_observation

    await history(db, "unaffected earlier fact")
    original = request()
    original.messages[-1].content = "old question"
    first = await accept(db, context, original)
    await history(db, "superseded answer", created_at=datetime.now(timezone.utc))
    await record_observation(db, run_id=UUID(first.run_id), observation="completed")
    replacement = request(
        supersedes_client_message_id=original.messages[-1].client_message_id
    )
    replacement.messages[-1].content = (
        "old question" if regenerate else "edited question"
    )
    second = await accept(db, context, replacement)
    assert second.tombstoned
    assert await dispatch_pending(db) == 1
    command = (await lease_commands(db, context, DEVICE))[0]
    assert isinstance(command.body, Start)
    value = command.body.input
    assert "unaffected earlier fact" in value
    assert "superseded answer" not in value
    assert value.count(replacement.messages[-1].content) == 1
    if not regenerate:
        assert "old question" not in value


async def test_equal_timestamp_uses_canonical_id_cutoff(
    db: AsyncSession, context: IntegrationContext, external_run: Any
) -> None:
    from src.services.harness.prompt_context import build_harness_prompt

    run: Any = await db.get(AgentRun, str(external_run.id))
    current: Any = await db.get(ChatMessage, run.user_message_id)
    await history(db, "before tied", id=UUID(int=1), created_at=current.created_at)
    await history(
        db, "after tied", id=UUID(int=2**128 - 1), created_at=current.created_at
    )
    prompt = await build_harness_prompt(db, run_id=run.job_id)
    assert "before tied" in prompt.input
    assert "after tied" not in prompt.input


async def test_completed_codex_turn_projects_context_into_followup(
    db: AsyncSession,
    context: IntegrationContext,
    external_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services.agent import agent_execution_service as execution
    from src.services.harness.delivery import ingest_bridge_event
    from tests.unit.services.harness.test_delivery import event

    monkeypatch.setattr(
        execution,
        "AsyncSessionLocal",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    assert await dispatch_pending(db) == 1
    first = (await lease_commands(db, context, DEVICE))[0]
    await ingest_bridge_event(
        db,
        context,
        event(
            first,
            body={
                "kind": "observation",
                "state": "running",
                "sessionId": "old-native-session",
                "turnId": "old-turn",
            },
        ),
    )
    await ingest_bridge_event(
        db,
        context,
        event(
            first,
            2,
            body={
                "kind": "event",
                "eventType": "assistant.delta",
                "payload": {"text": "The chosen label is violet-otter."},
            },
        ),
    )
    await ingest_bridge_event(
        db,
        context,
        event(
            first,
            3,
            body={
                "kind": "observation",
                "state": "completed",
                "sessionId": "old-native-session",
                "turnId": "old-turn",
            },
        ),
    )
    followup = request()
    followup.messages[-1].content = "What label did you just choose?"
    await accept(db, context, followup)
    assert await dispatch_pending(db) == 1
    second = (await lease_commands(db, context, DEVICE))[0]
    assert isinstance(second.body, Start)
    assert "violet-otter" in second.body.input
    assert second.body.input.count(followup.messages[-1].content) == 1
    assert second.body.sessionId is None


async def test_supported_edit_during_active_run_is_rejected_without_rewriting_command(
    db: AsyncSession, context: IntegrationContext
) -> None:
    from src.services.agent.agent_run_service import ActiveRunConflict

    req = request()
    first = await accept(db, context, req)
    assert await dispatch_pending(db) == 1
    stored = await db.scalar(select(HarnessCommand))
    assert stored is not None
    before = stored.body.copy()
    replacement = request(
        supersedes_client_message_id=req.messages[-1].client_message_id
    )
    replacement.messages[-1].content = "replacement while active"
    with pytest.raises(ActiveRunConflict):
        await accept(db, context, replacement)
    command = await db.scalar(select(HarnessCommand))
    assert command is not None
    assert command.run_id == first.run_id and command.body == before
    assert len(await lease_commands(db, context, DEVICE)) == 1
