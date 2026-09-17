"""Confirm stream must emit heartbeat frames during long silent resume phases,
like the main /stream path — otherwise a proxy idle-timeout cuts the connection
with no done/error and the client hangs."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.fixture(autouse=True)
def _allow_durable_confirm():
    with (
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
            "src.api.agent.streaming.is_run_cancellation_requested",
            new=AsyncMock(return_value=False),
        ),
    ):
        yield


class _SilentThenDoneGraph:
    def __init__(self):
        self._items = iter(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "llm_node",
                    "metadata": {"langgraph_node": "llm_node"},
                    "data": {"chunk": SimpleNamespace(content="hi")},
                },
            ]
        )

    def astream_events(self, *a, **k):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        pass

    async def aget_state(self, config):
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "page_context": {},
                "messages": [SimpleNamespace(type="ai", content="hi")],
                "tool_executions": [],
            },
            tasks=(),
        )


class _TokenThenHangIterator:
    def __init__(self):
        self.calls = 0
        self.cancelled = asyncio.Event()
        self._never = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.calls += 1
        if self.calls == 1:
            return {"event": "token"}
        try:
            await self._never.wait()
        finally:
            self.cancelled.set()
        raise StopAsyncIteration


async def _token_then_hang(cleaned: asyncio.Event):
    yield {"event": "token"}
    try:
        await asyncio.Event().wait()
    finally:
        cleaned.set()


async def _token_then_blocked_cleanup(
    pull_started: asyncio.Event,
    cleanup_started: asyncio.Event,
    allow_cleanup: asyncio.Event,
    cleanup_complete: asyncio.Event,
):
    yield {"event": "token"}
    try:
        pull_started.set()
        await asyncio.Event().wait()
    finally:
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_complete.set()


@pytest.mark.asyncio
async def test_graph_keepalive_polls_disconnect_while_next_event_is_pending(
    monkeypatch,
):
    import src.api.agent.streaming as st

    cleaned = asyncio.Event()
    graph = _token_then_hang(cleaned)
    request = SimpleNamespace(
        # Disconnect immediately after the check that starts the hanging pull.
        is_disconnected=AsyncMock(side_effect=[False, False, True])
    )
    monkeypatch.setattr(st, "_SSE_KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(st, "_SSE_DISCONNECT_POLL_SECONDS", 0.01, raising=False)

    events = st._graph_events_with_keepalive(graph, request)
    assert await anext(events) == {"type": "event", "event": {"event": "token"}}

    started = time.monotonic()
    assert await asyncio.wait_for(anext(events), timeout=0.1) == {"type": "disconnect"}
    assert time.monotonic() - started < 0.1
    await events.aclose()
    assert cleaned.is_set()
    await graph.aclose()


@pytest.mark.asyncio
async def test_graph_keepalive_polls_durable_stop_while_next_event_is_pending(
    monkeypatch,
):
    import src.api.agent.streaming as st

    cleaned = asyncio.Event()
    graph = _token_then_hang(cleaned)
    request = SimpleNamespace(
        is_disconnected=AsyncMock(side_effect=[False, False]),
    )
    stop_requested = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr(st, "_SSE_KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(st, "_SSE_DISCONNECT_POLL_SECONDS", 0.01, raising=False)

    events = st._graph_events_with_keepalive(
        graph,
        request,
        stop_requested=stop_requested,
    )
    assert await anext(events) == {"type": "event", "event": {"event": "token"}}

    assert await asyncio.wait_for(anext(events), timeout=0.1) == {"type": "stop"}
    await events.aclose()
    assert stop_requested.await_count == 3


@pytest.mark.asyncio
async def test_graph_disconnect_cleanup_preserves_concurrent_asgi_cancellation(
    monkeypatch,
):
    import src.api.agent.streaming as st

    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_complete = asyncio.Event()
    pull_started = asyncio.Event()
    graph = _token_then_blocked_cleanup(
        pull_started, cleanup_started, allow_cleanup, cleanup_complete
    )
    request = SimpleNamespace(
        is_disconnected=AsyncMock(side_effect=[False, False, True])
    )
    monkeypatch.setattr(st, "_SSE_KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(st, "_SSE_DISCONNECT_POLL_SECONDS", 0.1, raising=False)

    events = st._graph_events_with_keepalive(graph, request)
    yielded = [await anext(events)]
    assert await anext(events) == {"type": "disconnect"}

    close_task = asyncio.create_task(events.aclose())
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.1)
    close_task.cancel("original ASGI cancellation")
    await asyncio.sleep(0)
    close_task.cancel("repeated ASGI cancellation")
    await asyncio.sleep(0)
    assert not close_task.done()

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await close_task

    assert cleanup_complete.is_set()
    assert caught.value.args
    assert yielded == [{"type": "event", "event": {"event": "token"}}]


@pytest.mark.asyncio
async def test_graph_finally_cleanup_preserves_initial_asgi_cancellation(monkeypatch):
    import src.api.agent.streaming as st

    pull_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_complete = asyncio.Event()
    graph = _token_then_blocked_cleanup(
        pull_started, cleanup_started, allow_cleanup, cleanup_complete
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(st, "_SSE_KEEPALIVE_SECONDS", 0.2)
    monkeypatch.setattr(st, "_SSE_DISCONNECT_POLL_SECONDS", 0.01, raising=False)

    events = st._graph_events_with_keepalive(graph, request)
    assert await anext(events) == {"type": "event", "event": {"event": "token"}}

    next_item = asyncio.create_task(anext(events))
    await asyncio.wait_for(pull_started.wait(), timeout=0.1)
    next_item.cancel("original ASGI cancellation")
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.1)
    next_item.cancel("repeated ASGI cancellation")
    await asyncio.sleep(0)
    assert not next_item.done()

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await next_item

    assert cleanup_complete.is_set()
    assert caught.value.args == ("original ASGI cancellation",)


@pytest.mark.asyncio
async def test_graph_keepalive_does_not_emit_heartbeat_at_disconnect_poll_cadence(
    monkeypatch,
):
    import src.api.agent.streaming as st

    graph = _TokenThenHangIterator()
    graph.calls = 1
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(st, "_SSE_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(st, "_SSE_DISCONNECT_POLL_SECONDS", 0.005, raising=False)

    events = st._graph_events_with_keepalive(graph, request)
    next_item = asyncio.create_task(anext(events))
    await asyncio.sleep(0.02)
    assert not next_item.done()
    assert (await asyncio.wait_for(next_item, timeout=0.1))["type"] == "keepalive"
    await events.aclose()
    assert graph.cancelled.is_set()


@pytest.mark.asyncio
async def test_confirm_stream_emits_heartbeat_from_keepalive():
    import src.api.agent.streaming as st

    async def fake_keepalive(event_iter, request, **kwargs):
        # One keepalive, then forward the real event, then stop.
        yield {"type": "keepalive", "elapsed_ms": 123}
        async for e in event_iter:
            yield {"type": "event", "event": e}

    graph = _SilentThenDoneGraph()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(thread_id="t-1", confirmed=True, model="")
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch.object(st, "_graph_events_with_keepalive", fake_keepalive),
        patch.object(
            st,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id="editable-thread"), None)),
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
            "src.api.agent.streaming._jobs_mod._persist_assistant_message_safe",
            new=AsyncMock(return_value="a1"),
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
    ):
        events = [
            e
            async for e in st.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    assert any("event: heartbeat" in e for e in events)
