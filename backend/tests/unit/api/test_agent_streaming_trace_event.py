"""The ``trace`` frame must precede every WORKFLOW frame on both stream
generators.

``status`` progress frames are deliberately outside that contract: the first
one (``phase: accepted``) is emitted before the thread id has passed the
ownership check, i.e. before a trace payload can honestly name a thread. The
assertions below therefore run over ``workflow_frames`` rather than raw
indices, so adding another progress frame cannot re-break them.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from tests.utils.agent_stream import (
    make_stream_request,
    sse_event_name,
    workflow_frames,
)
from tests.utils.agent_thread_access import editable_thread_getter


class _FakeGraph:
    async def astream_events(self, *args, **kwargs):
        yield {
            "event": "on_chat_model_stream",
            "name": "llm_node",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"chunk": SimpleNamespace(content="hello")},
        }

    async def aget_state(self, config):
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "messages": [SimpleNamespace(type="ai", content="hello")],
                "tool_executions": [],
            },
            tasks=(),
        )


@pytest.mark.asyncio
async def test_stream_event_generator_emits_trace_event_before_workflow_events():
    from src.api.agent.streaming import stream_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111501")
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
            return_value=_FakeGraph(),
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

    # The very first frame on the main stream is the pre-ownership progress
    # signal; trace still leads everything the client renders as workflow.
    assert sse_event_name(events[0]) == "status"
    workflow = workflow_frames(events)
    assert sse_event_name(workflow[0]) == "trace"
    assert sse_event_name(workflow[1]) == "token"
    trace_payload = workflow[0].split("data: ", 1)[1].strip()
    # `_resolve_thread` is mocked to miss, which is the degraded path: nothing
    # about the client's thread id was verified, so it is discarded and the
    # turn runs under a fresh ephemeral id. The trace must therefore NOT echo
    # the id back — doing so would advertise a checkpoint the caller does not
    # own. (This assertion previously required the opposite, encoding the
    # pre-fix behaviour.)
    assert '"thread_id": "11111111-1111-1111-1111-111111111501"' not in trace_payload
    assert '"cli_session_id": ""' in trace_payload
    run_id = trace_payload.split('"langsmith_run_id": "', 1)[1].split('"', 1)[0]
    UUID(run_id)
    assert run_id
    assert '"langsmith_url": ""' in trace_payload
    assert "event: done\n" in events[-1]


@pytest.mark.asyncio
async def test_stream_confirm_event_generator_emits_trace_event_before_workflow_events():
    from src.api.agent.streaming import stream_confirm_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    thread_id = "11111111-1111-4111-8111-111111111624"
    body = SimpleNamespace(thread_id=thread_id, confirmed=True)
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
            return_value=_FakeGraph(),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
        patch(
            "src.api.agent.streaming.get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-1", user_message_id=None, client_message_id=None
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
            "src.api.agent.streaming._jobs_mod._persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-row-1"),
        ),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=editable_thread_getter(),
        ),
    ):
        events = []
        async for event in stream_confirm_event_generator(body, request, current_user):
            events.append(event)

    workflow = workflow_frames(events)
    assert sse_event_name(workflow[0]) == "trace"
    assert sse_event_name(workflow[1]) == "token"
    trace_payload = workflow[0].split("data: ", 1)[1].strip()
    assert f'"thread_id": "{thread_id}"' in trace_payload
    assert '"cli_session_id": ""' in trace_payload
    run_id = trace_payload.split('"langsmith_run_id": "', 1)[1].split('"', 1)[0]
    UUID(run_id)
    assert run_id
    assert '"langsmith_url": ""' in trace_payload
    assert "event: done\n" in events[-1]
