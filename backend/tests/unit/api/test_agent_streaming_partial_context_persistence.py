"""Stopped assistant rows retain the last cumulative citation snapshot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.api.agent import streaming
from tests.utils.agent_stream import frames_of_type, make_stream_request, sse_data

pytestmark = pytest.mark.unit


def _contexts() -> list[dict[str, Any]]:
    return [
        {
            "document_id": "11111111-1111-4111-8111-111111111111",
            "title": "First source",
            "content": "Canonical first evidence",
            "score": 0.9,
        },
        {
            "document_id": "22222222-2222-4222-8222-222222222222",
            "title": "Second source",
            "content": "Canonical second evidence",
            "score": 0.8,
        },
    ]


def _context_event(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "retrieval_node",
        "data": {"output": {"retrieved_contexts": contexts}},
    }


def _token_event() -> dict[str, Any]:
    return {
        "event": "on_chat_model_stream",
        "name": "llm_node",
        "metadata": {"langgraph_node": "llm_node"},
        "data": {"chunk": SimpleNamespace(content="partial answer")},
    }


def _reasoning_event() -> dict[str, Any]:
    return {
        "event": "on_chat_model_stream",
        "name": "llm_node",
        "metadata": {"langgraph_node": "llm_node"},
        "data": {
            "chunk": SimpleNamespace(
                content=[
                    {
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "Comparing evidence."}
                        ],
                    }
                ]
            )
        },
    }


class _InitialClearThenFailGraph:
    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        yield _context_event(_contexts())
        yield _context_event([])
        yield _token_event()
        raise TimeoutError("stop after the clear snapshot")


class _InitialNonemptyThenFailGraph:
    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        yield _context_event(_contexts())
        yield _reasoning_event()
        yield _token_event()
        raise TimeoutError("stop after the latest nonempty snapshot")


class _ConfirmCarriedThenFailGraph:
    def __init__(self, *, updated: list[dict[str, Any]] | None = None) -> None:
        self.carried = _contexts()
        self.updated = updated

    async def aget_state(self, _config: Any) -> SimpleNamespace:
        return SimpleNamespace(
            values={
                "thread_persistence": "durable",
                "user_id": "user-1",
                "page_context": {"type": "general"},
                "messages": [],
                "tool_executions": [],
                "retrieved_contexts": self.carried,
                "plan": [],
            },
            tasks=(
                SimpleNamespace(
                    interrupts=(SimpleNamespace(value={"message": "Approve?"}),)
                ),
            ),
            config={"configurable": {"checkpoint_id": "checkpoint-contexts"}},
        )

    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        if self.updated is not None:
            yield _context_event(self.updated)
        yield _token_event()
        raise TimeoutError("stop after carried contexts")


def _rag_contexts(frames: list[str]) -> list[list[dict[str, Any]]]:
    return [
        sse_data(frame)["contexts"] for frame in frames_of_type(frames, "rag_context")
    ]


@pytest.mark.asyncio
async def test_initial_partial_persistence_follows_empty_streamed_snapshot() -> None:
    """A cumulative empty snapshot clears earlier sources before a stopped row."""
    graph = _InitialClearThenFailGraph()
    persist = AsyncMock(return_value="assistant-row")
    body = make_stream_request(
        thread_id="33333333-3333-4333-8333-333333333333",
        use_rag=True,
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")

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
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch.object(streaming, "AsyncSessionLocal", return_value=AsyncMock()),
        patch.object(
            streaming,
            "_resolve_thread",
            new=AsyncMock(
                return_value=(SimpleNamespace(id="thread-context-clear"), None)
            ),
        ),
        patch.object(
            streaming,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming._jobs_mod,
            "_persist_assistant_message_safe",
            new=persist,
        ),
        patch.object(
            streaming._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
    ):
        frames = [
            frame
            async for frame in streaming.stream_event_generator(
                body, request, current_user
            )
        ]

    assert [
        [item["source_position"] for item in snapshot]
        for snapshot in _rag_contexts(frames)
    ] == [
        [1, 2],
        [],
    ]
    persist_call = persist.await_args
    assert persist_call is not None
    assert persist_call.kwargs["content"] == "partial answer"
    assert persist_call.kwargs["retrieved_contexts"] == []
    assert persist_call.kwargs["stopped"] is True


@pytest.mark.asyncio
async def test_initial_partial_persistence_retains_latest_nonempty_snapshot() -> None:
    """The exact cumulative source list shown to the client reaches storage."""
    graph = _InitialNonemptyThenFailGraph()
    persist = AsyncMock(return_value="assistant-row")
    body = make_stream_request(
        thread_id="33333333-3333-4333-8333-333333333333",
        use_rag=True,
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")

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
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch.object(streaming, "AsyncSessionLocal", return_value=AsyncMock()),
        patch.object(
            streaming,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id="thread-contexts"), None)),
        ),
        patch.object(
            streaming,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming._jobs_mod,
            "_persist_assistant_message_safe",
            new=persist,
        ),
        patch.object(
            streaming._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
    ):
        frames = [
            frame
            async for frame in streaming.stream_event_generator(
                body, request, current_user
            )
        ]

    streamed = _rag_contexts(frames)
    assert [
        [item["source_position"] for item in snapshot] for snapshot in streamed
    ] == [[1, 2]]
    persist_call = persist.await_args
    assert persist_call is not None
    assert persist_call.kwargs["retrieved_contexts"] == _contexts()
    assert persist_call.kwargs["reasoning_summary"] == "Comparing evidence."


@pytest.mark.asyncio
async def test_confirm_partial_persistence_retains_carried_context_order() -> None:
    """Disconnect/error persistence uses the carried interrupted-turn sources."""
    graph = _ConfirmCarriedThenFailGraph()
    persist = AsyncMock(return_value="assistant-row")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()
    body = SimpleNamespace(
        thread_id="44444444-4444-4444-8444-444444444444",
        confirmed=True,
        model="gpt-5",
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")

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
        patch.object(
            streaming,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id=body.thread_id), None)),
        ),
        patch.object(
            streaming,
            "get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-contexts",
                    user_message_id=None,
                    client_message_id=None,
                    run_metadata={"reasoning_summary": "Before approval."},
                )
            ),
        ),
        patch.object(
            streaming,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
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
            new=persist,
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
        patch.object(streaming, "_acquire_local_confirm_claim", return_value=True),
        patch.object(
            streaming._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value=None),
        ),
    ):
        frames = [
            frame
            async for frame in streaming.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    streamed = _rag_contexts(frames)
    assert [
        [item["source_position"] for item in snapshot] for snapshot in streamed
    ] == [[1, 2]]
    assert [item["content"] for item in streamed[0]] == [
        "Canonical first evidence",
        "Canonical second evidence",
    ]
    assert persist.await_args.kwargs["reasoning_summary"] == "Before approval."
    persist_call = persist.await_args
    assert persist_call is not None
    assert persist_call.kwargs["content"] == "partial answer"
    assert persist_call.kwargs["retrieved_contexts"] == graph.carried
    assert persist_call.kwargs["stopped"] is True


@pytest.mark.asyncio
async def test_confirm_partial_persistence_uses_latest_resumed_context_snapshot() -> (
    None
):
    """A post-confirm retrieval snapshot supersedes carried interrupt state."""
    updated = [
        {
            "document_id": "55555555-5555-4555-8555-555555555555",
            "title": "Replacement source",
            "content": "New canonical evidence",
            "score": 0.95,
        }
    ]
    graph = _ConfirmCarriedThenFailGraph(updated=updated)
    persist = AsyncMock(return_value="assistant-row")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()
    body = SimpleNamespace(
        thread_id="44444444-4444-4444-8444-444444444444",
        confirmed=True,
        model="gpt-5",
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = Mock(id="user-1", organization_id="org-1")

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
        patch.object(
            streaming,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id=body.thread_id), None)),
        ),
        patch.object(
            streaming,
            "get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-contexts",
                    user_message_id=None,
                    client_message_id=None,
                )
            ),
        ),
        patch.object(
            streaming,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
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
            new=persist,
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
        patch.object(streaming, "_acquire_local_confirm_claim", return_value=True),
        patch.object(
            streaming._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value=None),
        ),
    ):
        frames = [
            frame
            async for frame in streaming.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    streamed = _rag_contexts(frames)
    assert [
        [item["source_position"] for item in snapshot] for snapshot in streamed
    ] == [
        [1, 2],
        [1],
    ]
    assert streamed[-1][0]["content"] == "New canonical evidence"
    persist_call = persist.await_args
    assert persist_call is not None
    assert persist_call.kwargs["retrieved_contexts"] == updated
