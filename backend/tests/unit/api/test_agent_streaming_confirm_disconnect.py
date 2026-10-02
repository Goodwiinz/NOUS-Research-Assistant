"""Regression: stream/confirm cancels the resumed run on client disconnect.

stream_confirm_event_generator previously `break`-ed out of the
astream_events loop on disconnect WITHOUT aclose()-ing the iterator, leaving
the resumed graph run (which may execute destructive tools) running into a dead
socket. It must aclose() the iterator and stop without emitting further events.

AA-1 mutation verification (2026-10-02), from backend/:
    python -m pytest tests/unit/api/test_agent_streaming_confirm_disconnect.py \
        -k 'unclaimed or preclaim' -q --no-cov
Removing streaming.py:4493's cleanup ownership guard fails all four cases.
Removing its ownership release at :4244 fails both '-k "finished and confirmation"'
cases; removing the release at :4453 fails both '-k "finished and done"' cases.
The latter mutations cancel a parked run / mark a completed answer stopped.
Restore the source byte-for-byte, then run this whole file to verify all guards.
"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from src.services.agent.confirmation_service import pending_approval
from tests.utils.agent_thread_access import editable_thread_getter

THREAD_ID = "11111111-1111-4111-8111-111111111626"


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
            "src.api.agent.streaming.is_run_cancellation_requested",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "src.api.agent.streaming._finalize_run_id",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=editable_thread_getter(),
        ),
    ):
        yield


class _DisconnectGraph:
    """astream_events yields one event; on client disconnect the generator must
    aclose() the iterator (cancelling the resumed run) and stop without emitting
    the confirmation/done events."""

    def __init__(self, *, summary_only: bool = False):
        self.checkpoint_id = str(uuid4())
        self.aclosed = False
        self._sent = False
        self.summary_only = summary_only

    def astream_events(self, *args, **kwargs):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return {
                "event": "on_chat_model_stream",
                "name": "llm_node",
                "metadata": {"langgraph_node": "llm_node"},
                "data": {
                    "chunk": SimpleNamespace(
                        content=(
                            [
                                {
                                    "type": "reasoning",
                                    "summary": [
                                        {
                                            "type": "summary_text",
                                            "text": "Before the answer, I compared evidence.",
                                        }
                                    ],
                                }
                            ]
                            if self.summary_only
                            else "x"
                        )
                    )
                },
            }
        raise StopAsyncIteration

    async def aclose(self):
        self.aclosed = True

    async def aget_state(self, config):
        # Populated + owned by the requesting user so the pre-loop ownership
        # snapshot passes and execution reaches the stream loop.
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "page_context": {},
                "messages": [],
                "tool_executions": [],
            },
            tasks=(
                (
                    SimpleNamespace(
                        interrupts=(
                            SimpleNamespace(
                                id="test-interrupt", value={"message": "Approve?"}
                            ),
                        )
                    ),
                )
                if not self._sent
                else ()
            ),
            config={"configurable": {"checkpoint_id": self.checkpoint_id}},
        )


@pytest.mark.asyncio
async def test_confirm_stream_acloses_graph_on_disconnect():
    from src.api.agent.streaming import stream_confirm_event_generator

    graph = _DisconnectGraph()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    body = SimpleNamespace(
        thread_id=THREAD_ID,
        confirmed=True,
        model="",
        approval_id=pending_approval(
            await graph.aget_state({}),
            thread_id=THREAD_ID,
            run_id="run-1",
            user_id="user-1",
        ).approval_id,
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        # Force the legacy (no stream buffer) path: these tests cover the
        # Redis-down disconnect behavior.
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith", return_value=None
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
    ):
        events = []
        async for event in stream_confirm_event_generator(body, request, current_user):
            events.append(event)

    assert graph.aclosed is True  # resumed run cancelled
    # Early-returned before the post-loop snapshot/emit path.
    assert not any("event: done" in e or "event: confirmation" in e for e in events)


@pytest.mark.asyncio
async def test_confirm_stream_persists_partial_on_disconnect():
    """A destructive HITL tool may have already committed; the partial
    assistant answer streamed before the disconnect must be persisted
    (stopped=True), mirroring the main stream's disconnect branch.

    NOTE: unlike the aclose-only test above, ``is_disconnected`` returns
    False on the first poll (so the "x" token is streamed + accumulated),
    then True — modelling the real scenario (a token was delivered, THEN
    the client hung up). A permanently-disconnected client never receives a
    token to persist, so this ordering is required to exercise the persist
    path.
    """
    from src.api.agent.streaming import stream_confirm_event_generator

    graph = _DisconnectGraph()
    request = SimpleNamespace(
        is_disconnected=AsyncMock(side_effect=[False, True, True])
    )
    body = SimpleNamespace(
        thread_id=THREAD_ID,
        confirmed=True,
        model="",
        approval_id=pending_approval(
            await graph.aget_state({}),
            thread_id=THREAD_ID,
            run_id="run-1",
            user_id="user-1",
        ).approval_id,
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    persist = AsyncMock(return_value="assistant-row-1")
    with (
        # Force the legacy (no stream buffer) path: these tests cover the
        # Redis-down disconnect behavior.
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
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
            new=persist,
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value="user-cmid-1"),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
    ):
        async for _ in stream_confirm_event_generator(body, request, current_user):
            pass

    persist.assert_awaited_once()
    kwargs = persist.await_args.kwargs
    assert kwargs["content"] == "x"
    assert kwargs["stopped"] is True
    assert kwargs["thread_id"] == THREAD_ID
    # idempotency key derived from the original user turn's client_message_id
    assert kwargs["client_message_id"] is not None


@pytest.fixture
def confirm_lifecycle(monkeypatch):
    """Drive the real confirmation generator with isolated external services."""
    from src.api.agent import streaming
    from src.core import caching
    from src.services.agent import checkpointer
    from src.services.agent import graph as graph_mod
    from src.services.agent import job_store, memory, runtime_snapshot
    from src.shared.enums import JobStatus

    graph = _DisconnectGraph()
    snapshot = SimpleNamespace(
        values={
            "user_id": "user-1",
            "page_context": {},
            "messages": [],
            "tool_executions": [],
        },
        tasks=(
            SimpleNamespace(
                interrupts=(
                    SimpleNamespace(id="test-interrupt", value={"message": "Approve?"}),
                )
            ),
        ),
        config={"configurable": {"checkpoint_id": "owner-checkpoint"}},
    )
    graph.aget_state = AsyncMock(
        side_effect=lambda _: (
            snapshot
            if not graph._sent
            else SimpleNamespace(
                values=snapshot.values, tasks=(), config=snapshot.config
            )
        )
    )
    graph.astream_events = Mock(wraps=graph.astream_events)
    db = AsyncMock()
    run = SimpleNamespace(
        job_id="run-1",
        user_message_id=None,
        client_message_id=None,
        status=JobStatus.RUNNING,
    )

    async def finalize(_db, run_id, _user, **kwargs):
        assert run_id == run.job_id
        if run.status in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
            return False
        run.status = kwargs["status"]
        return True

    ctx = SimpleNamespace(
        graph=graph,
        snapshot=snapshot,
        db=db,
        run=run,
        claim=AsyncMock(return_value=True),
        local_claim=Mock(return_value=True),
        redis_claim=AsyncMock(return_value=True),
        redis=AsyncMock(return_value=None),
        finalize=AsyncMock(side_effect=finalize),
        persist=AsyncMock(return_value="assistant-row-1"),
        mark_stopped=AsyncMock(),
        finish=AsyncMock(),
    )
    monkeypatch.setattr(streaming, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(streaming, "_bootstrap_langsmith", lambda: None)
    monkeypatch.setattr(streaming, "_cancel_current_task_on_disconnect", lambda _: None)
    monkeypatch.setattr(
        streaming, "get_active_run_for_thread", AsyncMock(return_value=run)
    )
    monkeypatch.setattr(streaming, "claim_awaiting_run_for_confirmation", ctx.claim)
    monkeypatch.setattr(streaming, "_acquire_local_confirm_claim", ctx.local_claim)
    monkeypatch.setattr(streaming, "_release_local_confirm_claim", Mock())
    monkeypatch.setattr(streaming, "_finalize_run_id", ctx.finalize)
    monkeypatch.setattr(
        streaming, "process_local_confirmation_coordination_allowed", lambda: True
    )
    monkeypatch.setattr(streaming._SeqEmitter, "finish", ctx.finish)
    monkeypatch.setattr(
        streaming._stream_buffer, "stream_id_for_run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        streaming._stream_buffer,
        "start_stream",
        AsyncMock(side_effect=RuntimeError("redis down")),
    )
    monkeypatch.setattr(
        streaming._jobs_mod, "_persist_assistant_message_safe", ctx.persist
    )
    monkeypatch.setattr(
        streaming._jobs_mod, "_mark_assistant_message_stopped_safe", ctx.mark_stopped
    )
    monkeypatch.setattr(streaming._jobs_mod, "_run_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(
        checkpointer, "get_checkpointer", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(memory, "get_memory_store", AsyncMock(return_value=object()))
    monkeypatch.setattr(graph_mod, "compile_agent_graph", lambda **_: graph)
    monkeypatch.setattr(job_store, "get_redis", ctx.redis)
    monkeypatch.setattr(caching, "_acquire_lock", ctx.redis_claim)
    monkeypatch.setattr(caching, "_release_lock", AsyncMock())
    monkeypatch.setattr(
        runtime_snapshot,
        "hydrate_runtime_state_from_snapshot",
        AsyncMock(return_value={}),
    )
    ctx.stream = streaming.stream_confirm_event_generator(
        SimpleNamespace(
            thread_id=THREAD_ID,
            confirmed=True,
            model="",
            approval_id=pending_approval(
                snapshot, thread_id=THREAD_ID, run_id="run-1", user_id="user-1"
            ).approval_id,
        ),
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        Mock(id="user-1", organization_id="org-1"),
    )
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("loser", ["local", "redis", "durable"])
async def test_unclaimed_confirm_close_preserves_winning_run(confirm_lifecycle, loser):
    """AA-1: closing a rejection must not mutate the producer it rejected."""
    from src.shared.enums import JobStatus

    ctx = confirm_lifecycle
    if loser == "local":
        ctx.local_claim.return_value = False
    elif loser == "redis":
        ctx.redis.return_value = object()
        ctx.redis_claim.return_value = False
    else:
        ctx.claim.return_value = False
    frame = await anext(ctx.stream)
    assert "event: error" in frame
    await ctx.stream.aclose()
    assert ctx.run.status == JobStatus.RUNNING, "claim loser cancelled the winning run"
    ctx.finalize.assert_not_awaited()
    ctx.persist.assert_not_awaited()
    ctx.graph.astream_events.assert_not_called()
    ctx.finish.assert_awaited_once()
    ctx.db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_preclaim_confirm_cancel_preserves_winning_run(confirm_lifecycle):
    from src.shared.enums import JobStatus

    ctx = confirm_lifecycle
    entered = asyncio.Event()

    async def wait_for_redis():
        entered.set()
        await asyncio.Event().wait()

    ctx.redis.side_effect = wait_for_redis
    task = asyncio.create_task(anext(ctx.stream))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (
        ctx.run.status == JobStatus.RUNNING
    ), "pre-claim disconnect cancelled the winner"
    ctx.claim.assert_not_awaited()
    ctx.finalize.assert_not_awaited()
    ctx.persist.assert_not_awaited()
    ctx.graph.astream_events.assert_not_called()
    ctx.db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_postclaim_snapshot_change_releases_without_cancelling_new_approval(
    confirm_lifecycle, monkeypatch
):
    from src.api.agent import streaming

    ctx = confirm_lifecycle
    changed = SimpleNamespace(
        values=ctx.snapshot.values,
        tasks=ctx.snapshot.tasks,
        config={"configurable": {"checkpoint_id": "newer-checkpoint"}},
    )
    ctx.graph.aget_state.side_effect = [ctx.snapshot, changed]
    release = AsyncMock(return_value=False)  # A newer approval owns the row now.
    monkeypatch.setattr(streaming, "release_confirmation_claim", release)
    frame = await anext(ctx.stream)
    assert '"category": "conflict"' in frame
    await ctx.stream.aclose()
    release.assert_awaited_once()
    ctx.finalize.assert_not_awaited()
    ctx.persist.assert_not_awaited()
    ctx.graph.astream_events.assert_not_called()


@pytest.mark.asyncio
async def test_claimed_confirm_close_still_cancels_and_persists_partial(
    confirm_lifecycle,
):
    from src.shared.enums import JobStatus

    ctx = confirm_lifecycle
    async for frame in ctx.stream:
        if "event: token" in frame:
            break
    else:
        pytest.fail("resumed producer never delivered its token")
    await ctx.stream.aclose()
    assert ctx.run.status == JobStatus.CANCELLED
    assert ctx.graph.aclosed
    ctx.persist.assert_awaited_once()
    assert ctx.persist.await_args.kwargs["content"] == "x"
    assert ctx.persist.await_args.kwargs["stopped"] is True
    ctx.finalize.assert_awaited_once()
    assert (
        ctx.finalize.await_args.kwargs["payload"]["assistant_message_id"]
        == "assistant-row-1"
    )
    ctx.db.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "confirmation"])
@pytest.mark.parametrize("during_emit", [False, True])
async def test_finished_confirm_close_preserves_committed_disposition(
    confirm_lifecycle, monkeypatch, terminal, during_emit
):
    from src.api.agent import streaming
    from src.shared.enums import JobStatus

    ctx = confirm_lifecycle
    if terminal == "confirmation":
        nested = SimpleNamespace(
            values=ctx.snapshot.values,
            config={"configurable": {"checkpoint_id": "nested-checkpoint"}},
            tasks=(
                SimpleNamespace(
                    interrupts=(
                        SimpleNamespace(
                            id="nested-interrupt", value={"message": "Next action?"}
                        ),
                    )
                ),
            ),
        )
        ctx.graph.aget_state.side_effect = [ctx.snapshot, ctx.snapshot, nested]
    if during_emit:
        original_emit = streaming._SeqEmitter.emit

        async def interrupt_terminal(self, event, *args, **kwargs):
            if event.value == terminal:
                raise asyncio.CancelledError()
            return await original_emit(self, event, *args, **kwargs)

        monkeypatch.setattr(streaming._SeqEmitter, "emit", interrupt_terminal)
        with pytest.raises(asyncio.CancelledError):
            async for _ in ctx.stream:
                pass
    else:
        async for frame in ctx.stream:
            if f"event: {terminal}" in frame:
                break
        else:
            pytest.fail(f"missing terminal {terminal} frame")
        await ctx.stream.aclose()
    expected = (
        JobStatus.COMPLETED if terminal == "done" else JobStatus.AWAITING_CONFIRMATION
    )
    assert (
        ctx.run.status == expected
    ), "closed transport overwrote the committed disposition"
    ctx.mark_stopped.assert_not_awaited()
    ctx.finalize.assert_awaited_once()
    assert all(not call.kwargs.get("stopped") for call in ctx.persist.await_args_list)
    ctx.db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_stream_persists_summary_only_stop_before_answer() -> None:
    """A public provider summary survives a stop even when no answer token
    arrived yet; it must not fabricate answer content."""
    from src.api.agent.streaming import stream_confirm_event_generator

    graph = _DisconnectGraph(summary_only=True)
    request = SimpleNamespace(
        is_disconnected=AsyncMock(side_effect=[False, True, True])
    )
    body = SimpleNamespace(
        thread_id=THREAD_ID,
        confirmed=True,
        model="",
        approval_id=pending_approval(
            await graph.aget_state({}),
            thread_id=THREAD_ID,
            run_id="run-1",
            user_id="user-1",
        ).approval_id,
    )
    current_user = Mock(id="user-1", organization_id="org-1")
    persist = AsyncMock(return_value="assistant-row-summary")

    with (
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith", return_value=None
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
            new=persist,
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value="user-cmid-summary"),
        ),
        patch("src.api.agent.streaming.AsyncSessionLocal", return_value=AsyncMock()),
    ):
        async for _ in stream_confirm_event_generator(body, request, current_user):
            pass

    persist.assert_awaited_once()
    kwargs = persist.await_args.kwargs
    assert kwargs["content"] == ""
    assert kwargs["reasoning_summary"] == "Before the answer, I compared evidence."
    assert kwargs["stopped"] is True
