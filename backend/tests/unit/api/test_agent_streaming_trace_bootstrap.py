from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_stream import make_stream_request
from tests.utils.agent_thread_access import editable_thread_getter


class _FakeGraph:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or SimpleNamespace(values={"messages": []}, tasks=())

    async def astream_events(self, *args, **kwargs):
        if False:
            yield {}

    async def aget_state(self, config):
        return self._snapshot


@pytest.mark.asyncio
async def test_stream_event_generator_bootstraps_langsmith_before_compile():
    from src.api.agent.streaming import stream_event_generator

    configured = False

    def fake_configure_langsmith():
        nonlocal configured
        configured = True

    def fake_compile_agent_graph(*, checkpointer, store=None):
        assert configured, "configure_langsmith must run before graph compilation"
        return _FakeGraph()

    # Connected client: the (empty) event stream completes normally and the
    # generator must emit `done`. Disconnect now cancels early without a `done`
    # — covered by test_stream_event_generator_cancels_on_disconnect below.
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request()
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        # Force the legacy (no stream buffer) path: these tests cover the
        # Redis-down disconnect behavior.
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=fake_configure_langsmith,
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            side_effect=fake_compile_agent_graph,
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

    assert configured is True
    assert "event: done" in events[-1]


@pytest.mark.asyncio
async def test_stream_event_generator_cancels_on_disconnect():
    """A disconnected client cancels the run: no `done`, no persisted message."""
    from src.api.agent import streaming as streaming_mod
    from src.api.agent.streaming import stream_event_generator

    def fake_compile_agent_graph(*, checkpointer, store=None):
        return _FakeGraph()

    # Client is gone before the first graph event is read.
    expected_request_id = "request-disconnect-123"
    request = SimpleNamespace(
        state=SimpleNamespace(request_id=expected_request_id),
        is_disconnected=AsyncMock(return_value=True),
    )
    body = make_stream_request()
    current_user = Mock(id="user-1", organization_id="org-1")
    persist = AsyncMock(return_value=None)
    finalize = AsyncMock(return_value=None)

    with (
        # Force the legacy (no stream buffer) path: these tests cover the
        # Redis-down disconnect behavior.
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=lambda: None,
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            side_effect=fake_compile_agent_graph,
        ),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=persist,
        ),
        patch.object(streaming_mod, "_finalize_run", new=finalize),
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

    # No completion frame, and no assistant row persisted into a dead socket.
    assert not any("event: done" in e for e in events)
    persist.assert_not_awaited()
    assert finalize.await_args.kwargs["payload"] == {
        "reason": "client_disconnected",
        "request_id": expected_request_id,
    }


@pytest.mark.asyncio
async def test_stream_confirm_event_generator_bootstraps_langsmith_before_compile():
    from src.api.agent.streaming import stream_confirm_event_generator

    configured = False

    def fake_configure_langsmith():
        nonlocal configured
        configured = True

    def fake_compile_agent_graph(*, checkpointer, store=None):
        assert configured, "configure_langsmith must run before graph compilation"
        return _FakeGraph(current_snapshot)

    # Connected client — the confirm loop now routes through
    # _graph_events_with_keepalive (checks is_disconnected() before each
    # event), so True would cancel the resumed run before the `done` frame.
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-4111-8111-111111111623", confirmed=True
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    current_snapshot = SimpleNamespace(
        values={"page_context": {"type": "general"}, "user_id": "user-1"},
        tasks=(),
    )

    with (
        # Force the legacy (no stream buffer) path: these tests cover the
        # Redis-down disconnect behavior.
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=fake_configure_langsmith,
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            side_effect=fake_compile_agent_graph,
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

    assert configured is True
    assert "event: done" in events[-1]
