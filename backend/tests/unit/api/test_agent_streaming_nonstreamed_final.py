"""Regression: the SSE stream must surface a final answer that was produced
WITHOUT streaming (greeting fast-path, templated/degraded reply, force_synthesis).

Those paths set the AIMessage directly and emit no ``on_chat_model_stream``
chunks, so without the fallback the client receives zero ``token`` events and
renders an empty response ("stream completed without any tokens").
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_stream import frames_of_type, make_stream_request


@pytest.mark.parametrize(
    "node",
    [
        "force_synthesis_node",
        "research_force_synthesis_node",
        "writing_force_synthesis_node",
        "data_force_synthesis_node",
    ],
)
def test_force_synthesis_tokens_are_user_facing(node: str) -> None:
    from src.api.agent.streaming import _is_user_facing_token_event

    assert _is_user_facing_token_event({"metadata": {"langgraph_node": node}})


def test_chunk_reasoning_summary_excludes_raw_reasoning() -> None:
    from src.api.agent.streaming import _chunk_reasoning_summary

    chunk = SimpleNamespace(
        content=[
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Checking sources"}],
                "content": [{"type": "reasoning_text", "text": "private"}],
            }
        ]
    )

    assert _chunk_reasoning_summary(chunk) == "Checking sources"


class _FakeGraphNoStream:
    """Graph that emits NO on_chat_model_stream events (e.g. greeting fast-path)
    but whose final state carries an AI answer."""

    async def astream_events(self, *args, **kwargs):
        # A non-token chain event — no on_chat_model_stream is ever yielded.
        yield {
            "event": "on_chain_end",
            "name": "llm_node",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"output": {}},
        }

    async def aget_state(self, config):
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "messages": [
                    SimpleNamespace(
                        type="ai",
                        content="Hi — how can I help with your research today?",
                    )
                ],
                "tool_executions": [],
            },
            tasks=(),
        )


class _FakeGraphWithRootFinal:
    """Graph whose root event already carries the completed final state."""

    def __init__(self):
        self.aget_state_calls = 0
        self.final_values = {
            "user_id": "user-1",
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content="Root output answer",
                    tool_calls=[],
                )
            ],
            "tool_executions": [],
        }

    async def astream_events(self, *args, **kwargs):
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {"output": self.final_values},
        }

    async def aget_state(self, config):
        self.aget_state_calls += 1
        if self.aget_state_calls == 1:
            # Required pre-run stale-interrupt check.
            return SimpleNamespace(values={}, tasks=())
        return SimpleNamespace(values=self.final_values, tasks=())


class _FakeGraphWithInterruptedRoot:
    """Interrupted roots expose state but not pending-task metadata in events."""

    def __init__(self):
        self.aget_state_calls = 0
        self.interrupted_values = {
            "user_id": "user-1",
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content="",
                    tool_calls=[{"name": "create_project", "args": {}}],
                )
            ],
            "tool_executions": [],
        }

    async def astream_events(self, *args, **kwargs):
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {"output": self.interrupted_values},
        }

    async def aget_state(self, config):
        self.aget_state_calls += 1
        if self.aget_state_calls == 1:
            return SimpleNamespace(values={}, tasks=())
        interrupt = SimpleNamespace(
            id="test-interrupt",
            value={
                "pending_tools": ["create_project"],
                "tools": [{"name": "create_project", "args": {}}],
            },
        )
        task = SimpleNamespace(interrupts=[interrupt])
        return SimpleNamespace(
            values={
                **self.interrupted_values,
                "user_id": config["configurable"]["user_id"],
            },
            tasks=[task],
            config={"configurable": {"checkpoint_id": "saved-interrupt"}},
        )


@pytest.mark.asyncio
async def test_stream_emits_nonstreamed_final_answer_as_token():
    from src.api.agent.streaming import stream_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111601")
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
            "src.services.agent.graph.compile_agent_graph",
            return_value=_FakeGraphNoStream(),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    # Exactly one token event, carrying the non-streamed final answer.
    token_events = frames_of_type(events, "token")
    assert len(token_events) == 1, f"expected one fallback token, got {events}"
    assert "how can I help with your research today" in token_events[0]
    # And it must come before `done`.
    assert "event: done\n" in events[-1]
    done_idx = next(i for i, e in enumerate(events) if "event: done\n" in e)
    token_idx = next(i for i, e in enumerate(events) if "event: token\n" in e)
    assert token_idx < done_idx


@pytest.mark.asyncio
async def test_completed_stream_reuses_root_output_without_final_checkpoint_read():
    from src.api.agent.streaming import stream_event_generator

    graph = _FakeGraphWithRootFinal()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(
        messages=[{"role": "user", "content": "hello"}],
        thread_id="11111111-1111-1111-1111-111111111602",
    )
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
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    assert any(
        "event: token\n" in event and "Root output answer" in event for event in events
    )
    assert any("event: done\n" in event for event in events)
    assert graph.aget_state_calls == 1


@pytest.mark.asyncio
async def test_interrupted_root_still_reads_checkpoint_for_pending_tasks():
    from src.api.agent.streaming import stream_event_generator

    graph = _FakeGraphWithInterruptedRoot()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(
        messages=[{"role": "user", "content": "create a project"}],
        thread_id="11111111-1111-1111-1111-111111111603",
    )
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
            "src.services.agent.graph.compile_agent_graph",
            return_value=graph,
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    assert any("event: confirmation\n" in event for event in events)
    assert not any("event: done\n" in event for event in events)
    assert graph.aget_state_calls == 2
