"""Transport regressions for cumulative citation snapshots.

These tests drive both public SSE generators and parse their real formatted
frames.  The graph is the only substantial double: it stands in for external
model/tool execution while preserving the LangGraph event and checkpoint
shapes consumed by the transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_stream import frames_of_type, make_stream_request, sse_data

pytestmark = pytest.mark.unit


def _contexts(count: int) -> list[dict[str, Any]]:
    return [
        {
            "document_id": f"document-{position:02d}",
            "chunk_id": f"chunk-{position:02d}",
            "chunk_index": position - 1,
            "page_number": position,
            "title": f"Source {position}",
            "content": f"Evidence from source {position}",
        }
        for position in range(1, count + 1)
    ]


def _context_event(name: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": name,
        "data": {"output": {"retrieved_contexts": contexts}},
    }


class _InitialCitationGraph:
    def __init__(self) -> None:
        self.contexts = _contexts(20)

    async def astream_events(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        first_four = self.contexts[:4]
        first_six = self.contexts[:6]
        yield _context_event("search_documents", first_four)
        # A wrapper commonly republishes its child's state. It must not create
        # a duplicate wire snapshot.
        yield _context_event("search_documents_wrapper", first_four)
        yield _context_event("research_synthesis", first_six)
        yield _context_event("final_source_projection", self.contexts)

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "messages": [SimpleNamespace(type="ai", content="Answer.")],
                "tool_executions": [],
                "retrieved_contexts": self.contexts,
            },
            tasks=(),
        )


class _ConfirmCitationGraph:
    def __init__(self) -> None:
        self.carried = _contexts(4)
        self.resumed = _contexts(6)
        self.state_reads = 0

    async def astream_events(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        # The first resumed wrapper repeats the checkpoint snapshot; only the
        # later cumulative six-source state should add a wire frame.
        yield _context_event("resume_wrapper", self.carried)
        yield _context_event("resumed_search", self.resumed)

    async def aget_state(self, config: Any) -> Any:
        self.state_reads += 1
        contexts = self.carried if self.state_reads == 1 else self.resumed
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "page_context": {"type": "general"},
                "messages": [SimpleNamespace(type="ai", content="Confirmed answer.")],
                "tool_executions": [],
                "retrieved_contexts": contexts,
            },
            tasks=(),
            config={},
        )


def _rag_snapshots(frames: list[str]) -> list[list[dict[str, Any]]]:
    return [
        sse_data(frame)["contexts"] for frame in frames_of_type(frames, "rag_context")
    ]


async def _run_initial(graph: _InitialCitationGraph) -> list[str]:
    from src.api.agent.streaming import stream_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(
        thread_id="11111111-1111-1111-1111-111111111622",
        use_rag=True,
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch(
            "src.services.agent.job_store._get_redis",
            new=AsyncMock(return_value=None),
        ),
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
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        return [
            frame async for frame in stream_event_generator(body, request, current_user)
        ]


async def _run_confirm(graph: _ConfirmCitationGraph) -> list[str]:
    from src.api.agent.streaming import stream_confirm_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="22222222-2222-2222-2222-222222221622",
        confirmed=True,
        model="gpt-5",
    )
    current_user = Mock(id="user-1", organization_id="org-1")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    with (
        patch(
            "src.services.agent.job_store._get_redis",
            new=AsyncMock(return_value=None),
        ),
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
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=fake_db,
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id=body.thread_id), None)),
        ),
        patch(
            "src.api.agent.streaming.get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-1",
                    user_message_id=None,
                    client_message_id=None,
                )
            ),
        ),
        patch(
            "src.api.agent.streaming.claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.streaming._finalize_run_id",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.streaming.process_local_confirmation_coordination_allowed",
            return_value=True,
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-message-1"),
        ),
    ):
        return [
            frame
            async for frame in stream_confirm_event_generator(
                body, request, current_user
            )
        ]


@pytest.mark.asyncio
async def test_initial_stream_emits_changed_cumulative_snapshots_once() -> None:
    """Removing node-agnostic fingerprint dedup emits [4, 4, 6, 20].

    Mutation-verified against ``streaming.py:1010`` with::

        PATH=/tmp/ci-venv/bin:/home/clawdbot/actions-runner/externals.2.335.1/node24/bin:$PATH pytest -q backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py::test_initial_stream_emits_changed_cumulative_snapshots_once

    Neutralizing the equality guard failed with ``[4, 4, 6, 20]``; restoring
    it made this named test pass again.
    """
    snapshots = _rag_snapshots(await _run_initial(_InitialCitationGraph()))

    assert [len(snapshot) for snapshot in snapshots] == [4, 6, 20]
    assert [context["document_id"] for context in snapshots[0]] == [
        "document-01",
        "document-02",
        "document-03",
        "document-04",
    ]
    assert [context["source_position"] for context in snapshots[-1]] == list(
        range(1, 21)
    )


@pytest.mark.asyncio
async def test_confirm_stream_replaces_carried_snapshot_with_resumed_snapshot() -> None:
    """Concatenating carried and resumed sources emits ten, not six, sources."""
    snapshots = _rag_snapshots(await _run_confirm(_ConfirmCitationGraph()))

    assert [len(snapshot) for snapshot in snapshots] == [4, 6]
    assert [context["document_id"] for context in snapshots[-1]] == [
        "document-01",
        "document-02",
        "document-03",
        "document-04",
        "document-05",
        "document-06",
    ]
    assert [context["source_position"] for context in snapshots[-1]] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
