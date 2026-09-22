"""Authorization regressions for threadless SSE confirmation checkpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.api.agent import streaming
from src.services.agent.agent_execution_service import AgentThreadResolutionError
from tests.utils.agent_stream import frames_of_type

pytestmark = pytest.mark.unit

CHECKPOINT_THREAD_ID = "11111111-1111-4111-8111-111111111111"


def _snapshot(
    *,
    user_id: str | None = "user-1",
    thread_persistence: str | None = "ephemeral",
    pending: bool = True,
    checkpoint_id: str | None = "ephemeral-checkpoint-1",
    state_thread_id: str = "",
    answer: str = "",
) -> SimpleNamespace:
    values: dict[str, Any] = {
        "page_context": {"type": "general"},
        "user_id": user_id,
        "thread_id": state_thread_id,
        "messages": ([SimpleNamespace(type="ai", content=answer)] if answer else []),
        "tool_executions": [],
        "retrieved_contexts": [],
        "plan": [],
    }
    if thread_persistence is not None:
        values["thread_persistence"] = thread_persistence
    tasks = (
        (SimpleNamespace(interrupts=(SimpleNamespace(value={"message": "Approve?"}),)),)
        if pending
        else ()
    )
    config = (
        {"configurable": {"checkpoint_id": checkpoint_id}}
        if checkpoint_id is not None
        else {}
    )
    return SimpleNamespace(values=values, tasks=tasks, config=config)


class _EphemeralConfirmGraph:
    def __init__(self, initial: SimpleNamespace, final: SimpleNamespace) -> None:
        self._snapshots = [initial, final]
        self.resumed = False

    async def aget_state(self, _config: Any) -> SimpleNamespace:
        return (
            self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]
        )

    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.resumed = True
        yield {
            "event": "on_chat_model_stream",
            "name": "llm_node",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"chunk": SimpleNamespace(content="confirmed")},
        }


async def _run_confirm(
    graph: _EphemeralConfirmGraph, *, claim_winner: bool = True
) -> list[str]:
    body = SimpleNamespace(
        thread_id=CHECKPOINT_THREAD_ID,
        confirmed=True,
        model="gpt-5",
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    with (
        patch(
            "src.services.agent.observability.configure_langsmith",
            return_value=None,
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.checkpointer.reset_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch.object(streaming, "AsyncSessionLocal", return_value=fake_db),
        # A proven ephemeral checkpoint has no durable Thread or AgentRun. Any
        # attempt to use either boundary must fail this flow, not be hidden by
        # a permissive double.
        patch.object(
            streaming,
            "_resolve_thread",
            new=AsyncMock(side_effect=AgentThreadResolutionError("Thread not found")),
        ),
        patch.object(
            streaming,
            "get_active_run_for_thread",
            new=AsyncMock(side_effect=AssertionError("ephemeral run is not durable")),
        ),
        patch.object(
            streaming,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(side_effect=AssertionError("ephemeral run has no DB claim")),
        ),
        patch.object(streaming, "_finalize_run_id", new=AsyncMock(return_value=True)),
        patch.object(
            streaming,
            "_latest_user_client_message_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(
                side_effect=AssertionError("ephemeral answer has no durable thread")
            ),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming,
            "process_local_confirmation_coordination_allowed",
            return_value=True,
        ),
        patch.object(
            streaming,
            "_acquire_local_confirm_claim",
            return_value=claim_winner,
        ),
        patch.object(
            streaming._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value=None),
        ),
    ):
        return [
            frame
            async for frame in streaming.stream_confirm_event_generator(
                body, request, current_user
            )
        ]


@pytest.mark.asyncio
async def test_owned_pending_ephemeral_checkpoint_can_resume_without_db_thread() -> (
    None
):
    """Only the server-authored ephemeral marker admits a threadless resume."""
    graph = _EphemeralConfirmGraph(
        _snapshot(),
        _snapshot(pending=False, answer="confirmed"),
    )

    frames = await _run_confirm(graph)

    assert graph.resumed is True
    assert frames_of_type(frames, "token")
    assert frames_of_type(frames, "done")
    assert not frames_of_type(frames, "error")


@pytest.mark.asyncio
async def test_ephemeral_confirmation_loser_cannot_double_resume() -> None:
    """The checkpoint-scoped claim still serializes threadless confirmations.

    Mutation verified 2026-09-12 against
    ``streaming.py:3204 if not _acquire_local_confirm_claim(...)`` with::

        pytest -q backend/tests/unit/api/test_agent_streaming_ephemeral_confirm.py::test_ephemeral_confirmation_loser_cannot_double_resume

    Neutralizing that branch fails because ``graph.resumed`` becomes true.
    """
    graph = _EphemeralConfirmGraph(
        _snapshot(),
        _snapshot(pending=False, answer="wrong"),
    )

    frames = await _run_confirm(graph, claim_winner=False)

    assert graph.resumed is False
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    assert "already in progress" in errors[0]


@pytest.mark.asyncio
async def test_ephemeral_nested_interrupt_remains_confirmable() -> None:
    """A resumed ephemeral graph can rotate to another pending checkpoint."""
    graph = _EphemeralConfirmGraph(
        _snapshot(),
        _snapshot(pending=True, checkpoint_id="nested-checkpoint"),
    )

    frames = await _run_confirm(graph)

    assert graph.resumed is True
    assert frames_of_type(frames, "confirmation")
    assert not frames_of_type(frames, "done")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "expected_error"),
    [
        (_snapshot(user_id=None), "Thread not found"),
        (_snapshot(user_id="foreign-user"), "Thread not found"),
        (_snapshot(pending=False), "Run is not awaiting confirmation"),
        (_snapshot(checkpoint_id=None), "Run is not awaiting confirmation"),
    ],
    ids=["ownerless", "foreign-owner", "consumed", "unclaimable"],
)
async def test_ephemeral_checkpoint_fails_closed_without_all_provenance(
    initial: SimpleNamespace,
    expected_error: str,
) -> None:
    graph = _EphemeralConfirmGraph(initial, _snapshot(pending=False, answer="wrong"))

    frames = await _run_confirm(graph)

    assert graph.resumed is False
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    assert expected_error in errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_persistence", [None, "durable", "unknown"])
async def test_missing_or_non_ephemeral_marker_cannot_bypass_revoked_thread(
    thread_persistence: str | None,
) -> None:
    """A deleted/revoked durable thread remains denied after checkpoint lookup."""
    graph = _EphemeralConfirmGraph(
        _snapshot(thread_persistence=thread_persistence),
        _snapshot(pending=False, answer="wrong"),
    )

    frames = await _run_confirm(graph)

    assert graph.resumed is False
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    assert "Thread not found" in errors[0]


@pytest.mark.asyncio
async def test_copied_ephemeral_marker_cannot_bypass_revoked_durable_thread() -> None:
    """The marker is valid only when the checkpoint itself is threadless."""
    graph = _EphemeralConfirmGraph(
        _snapshot(
            thread_persistence="ephemeral",
            state_thread_id=CHECKPOINT_THREAD_ID,
        ),
        _snapshot(pending=False, answer="wrong"),
    )

    frames = await _run_confirm(graph)

    assert graph.resumed is False
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    assert "Thread not found" in errors[0]
