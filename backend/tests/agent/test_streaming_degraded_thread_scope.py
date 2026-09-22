"""Tenant isolation on the degraded (`_resolve_thread` -> None) stream path.

``_resolve_thread`` returns ``None`` when the ownership join matched no thread
AND the caller has no live workspace to create one under (e.g. they deleted
their only workspace). Nothing about the client's ``thread_id`` has been
verified at that point, yet everything downstream keys the LangGraph checkpoint
off it — ``configurable.thread_id`` IS the checkpoint key, and
``astream_events`` against a checkpointer merges the turn into whatever
checkpoint that key names.

So the id must not survive resolution. ``/execute`` already nulls it on this
exact miss (``execute.py``); these tests pin the same contract for ``/stream``,
and the control case pins that an owned thread still reaches the config.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from src.api.agent import streaming
from tests.utils.agent_stream import make_stream_request

# A thread id the streaming user does NOT own.
VICTIM_THREAD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _ConfigCapturingGraph:
    """Records the (state, config) the generator would run the turn under."""

    def __init__(self) -> None:
        self.initial_state: Any = None
        self.config: Any = None

    async def astream_events(
        self, initial_state: Any, *, config: Any = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.initial_state = initial_state
        self.config = config
        yield {
            "event": "on_chat_model_stream",
            "name": "llm_node",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"chunk": SimpleNamespace(content="hello")},
        }

    async def aget_state(self, config: Any) -> SimpleNamespace:
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "messages": [SimpleNamespace(type="ai", content="hello")],
                "tool_executions": [],
            },
            tasks=(),
        )


async def _run_stream(
    thread_obj: Any, *, started_streams: list[str]
) -> tuple[_ConfigCapturingGraph, Any]:
    """Drive the generator with ``_resolve_thread`` returning ``thread_obj``."""
    graph = _ConfigCapturingGraph()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id=VICTIM_THREAD_ID, use_rag=False)
    current_user = Mock(id="user-1", organization_id="org-1")

    async def start_stream(thread_id: str, *, run_id: str | None = None) -> str:
        started_streams.append(thread_id)
        return f"sid-{thread_id}"

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
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(thread_obj, None)),
        ),
        patch(
            "src.api.agent.streaming.accept_submission",
            new=AsyncMock(
                return_value=streaming.AcceptedSubmission(
                    run_id="55555555-5555-5555-5555-555555555555",
                    thread_id=str(getattr(thread_obj, "id", "")),
                    user_message_id=None,
                    outbox_id=None,
                    idempotency_key="turn-1",
                    replayed=False,
                )
            ),
        ),
        patch(
            "src.api.agent.streaming.mark_submission_dispatched",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.streaming._persist_user_message_guarded",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.streaming._resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.streaming._jobs_mod._persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-row-1"),
        ),
        patch.object(streaming._stream_buffer, "start_stream", start_stream),
        patch.object(streaming._stream_buffer, "append", AsyncMock()),
        patch.object(streaming._stream_buffer, "finish_stream", AsyncMock()),
    ):
        async for _frame in streaming.stream_event_generator(
            body, request, current_user
        ):
            pass

    return graph, body


@pytest.mark.asyncio
async def test_unverified_thread_id_never_becomes_the_checkpoint_key() -> None:
    """No owned thread + no workspace => the client's id is discarded."""
    started_streams: list[str] = []

    graph, body = await _run_stream(None, started_streams=started_streams)

    # The request object itself is scrubbed, so no later reader can pick it up.
    assert body.thread_id is None

    # The checkpoint key is a fresh ephemeral id, NOT the victim's thread.
    checkpoint_key = graph.config["configurable"]["thread_id"]
    assert checkpoint_key != VICTIM_THREAD_ID
    UUID(checkpoint_key)  # a real uuid4, not "" or "unknown"

    # Graph state carries no thread identity either.
    assert graph.initial_state["thread_id"] == ""
    assert graph.initial_state["thread_persistence"] == "ephemeral"

    # And the victim's resumable-stream pointer is never hijacked.
    assert VICTIM_THREAD_ID not in started_streams


@pytest.mark.asyncio
async def test_owned_thread_id_still_reaches_the_graph_config() -> None:
    """Control: the ownership-verified id is exactly what the turn runs under."""
    started_streams: list[str] = []
    owned = SimpleNamespace(id=VICTIM_THREAD_ID)

    graph, body = await _run_stream(owned, started_streams=started_streams)

    assert body.thread_id == VICTIM_THREAD_ID
    assert graph.config["configurable"]["thread_id"] == VICTIM_THREAD_ID
    assert graph.initial_state["thread_id"] == VICTIM_THREAD_ID
    assert graph.initial_state["thread_persistence"] == "durable"
    assert VICTIM_THREAD_ID in started_streams
