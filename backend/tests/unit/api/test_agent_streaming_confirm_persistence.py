"""Regression tests for the HITL confirm-path persistence fixes.

Covers three bugs fixed in this PR:
  1. The confirm path used to call ``_persist_thread_messages``, which
     re-inserted the user row every confirm (no client_message_id → no
     dedup). It must now persist ONLY the assistant row.
  2. The resumed assistant row was never idempotent — a double-confirm
     inserted a duplicate. It must now derive a deterministic
     ``client_message_id`` from the original user row and hit the partial
     unique index.
  3. ``done`` must carry the persisted ``assistant_message_id`` in
     canonical mode so the client can reconcile its optimistic bubble.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.agent.stream_buffer import BufferedFrame
from tests.utils.agent_stream import frames_of_type, sse_seq


@pytest.fixture(autouse=True)
def _reset_compiled_graph_cache():
    import src.api.agent.streaming as mod

    mod._COMPILED_GRAPH = None
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
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id="editable-thread"), None)),
        ),
    ):
        yield
    mod._COMPILED_GRAPH = None


def _make_snapshot(*, user_id="user-1", plan=None):
    ai_msg = SimpleNamespace(type="ai", content="Done — confirmed action.")
    return SimpleNamespace(
        values={
            "page_context": {"type": "general"},
            "user_id": user_id,
            "messages": [ai_msg],
            "tool_executions": [],
            "retrieved_contexts": [],
            "plan": plan or [],
        },
        tasks=(),
    )


class _FakeGraph:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.aclosed = False

    async def astream_events(self, *args, **kwargs):
        if False:  # pragma: no cover - never iterated; is_disconnected is True
            yield {}

    async def aget_state(self, config):
        return self._snapshot

    async def aclose(self):
        self.aclosed = True


class _NestedInterruptGraph:
    """Resume once, then park on another approval gate."""

    def __init__(self, thread_id):
        values = {
            "page_context": {"type": "general"},
            "user_id": "user-1",
            "messages": [],
        }
        self._snapshots = [
            SimpleNamespace(
                values=values,
                tasks=(),
                config={"configurable": {"checkpoint_id": "checkpoint-1"}},
            ),
            SimpleNamespace(
                values=values,
                tasks=(
                    SimpleNamespace(
                        interrupts=(
                            SimpleNamespace(
                                value={
                                    "tool_name": "create_note",
                                    "message": "Create this note?",
                                }
                            ),
                        )
                    ),
                ),
            ),
        ]
        self.thread_id = thread_id

    async def astream_events(self, *args, **kwargs):
        if False:  # pragma: no cover - keep this an async generator
            yield {}

    async def aget_state(self, config):
        return self._snapshots.pop(0)


@pytest.mark.asyncio
async def test_confirm_persists_only_assistant_row_not_user_row():
    """The user row was already persisted by the original /stream request;
    the confirm path must not re-insert it."""
    from src.api.agent.streaming import stream_confirm_event_generator

    snapshot = _make_snapshot()
    persist_mock = AsyncMock(return_value="assistant-msg-1")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    # Connected client — the confirm loop now routes events through
    # _graph_events_with_keepalive, which checks is_disconnected() before
    # pulling each event. A True here would (correctly) trigger the
    # cancel/persist-partial branch instead of a normal completion.
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-1111-1111-111111111111",
        confirmed=True,
        model="gpt-5",
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=lambda: None,
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
            return_value=_FakeGraph(snapshot),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=fake_db,
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=persist_mock,
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value=None),
        ),
    ):
        async for _ in stream_confirm_event_generator(body, request, current_user):
            pass

    persist_mock.assert_awaited_once()
    kwargs = persist_mock.await_args.kwargs
    # Must NOT have hit the user-row path at all — only the assistant row.
    assert persist_mock.await_count == 1
    # The role is implicit (always assistant inside _persist_assistant_message),
    # but the call must carry the resumed turn's content + plan + token_usage.
    assert kwargs["thread_id"] == body.thread_id
    assert kwargs["content"] == "Done — confirmed action."
    assert kwargs["model_name"] == "gpt-5"
    assert kwargs["plan"] == [] or kwargs["plan"] is None


@pytest.mark.asyncio
async def test_confirm_derives_idempotent_assistant_cmid_from_user_row():
    """A double-confirm must hit the assistant partial unique index. The
    resumed turn has no fresh cmid, so the assistant key is derived (uuid5)
    from the original user row's client_message_id."""
    import uuid as _uuid

    from src.api.agent.streaming import stream_confirm_event_generator

    user_cmid = "22222222-2222-2222-2222-222222222222"
    expected_assistant_cmid = str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, f"nous-assistant:{user_cmid}")
    )

    snapshot = _make_snapshot()
    persist_mock = AsyncMock(return_value="assistant-msg-1")
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    # Connected client — the confirm loop now routes events through
    # _graph_events_with_keepalive, which checks is_disconnected() before
    # pulling each event. A True here would (correctly) trigger the
    # cancel/persist-partial branch instead of a normal completion.
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-1111-1111-111111111111",
        confirmed=True,
        model="gpt-5",
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=lambda: None,
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
            return_value=_FakeGraph(snapshot),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=fake_db,
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=persist_mock,
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value=user_cmid),
        ),
    ):
        async for _ in stream_confirm_event_generator(body, request, current_user):
            pass

    kwargs = persist_mock.await_args.kwargs
    assert kwargs["client_message_id"] == expected_assistant_cmid


@pytest.mark.asyncio
async def test_legacy_cursor_confirm_receives_nested_approval_gate():
    """A no-``?stream=`` cursor from the pre-confirm stream must not skip the
    resumed stream's early frames, especially a nested approval gate.
    """
    from src.api.agent import streaming as streaming_mod

    thread_id = "11111111-1111-1111-1111-111111111111"
    old_stream_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    old_cursor = 7
    run_stream = {"run-1": old_stream_id}

    async def stream_id_for_run(run_id):
        return run_stream.get(run_id)

    async def start_stream(_thread_id, *, run_id=None):
        new_stream_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        if run_id is not None:
            run_stream[run_id] = new_stream_id
        return new_stream_id

    async def read_after(stream_id, after, *, start_index=None):
        if stream_id != old_stream_id or after >= old_cursor:
            return []
        return [
            BufferedFrame(
                seq=old_cursor,
                frame=(f"id: {old_cursor}\nevent: confirmation\n" "data: {}\n\n"),
            )
        ]

    fake_db = AsyncMock()
    fake_db.close = AsyncMock()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(thread_id=thread_id, confirmed=True, model="gpt-5")
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=lambda: None,
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
            return_value=_NestedInterruptGraph(thread_id),
        ),
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_db),
        patch.object(
            streaming_mod._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(side_effect=stream_id_for_run),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "read_after",
            new=AsyncMock(side_effect=read_after),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=start_stream),
        ),
        patch.object(
            streaming_mod._stream_buffer, "append", new=AsyncMock(return_value=None)
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "finish_stream",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "process_local_confirmation_coordination_allowed",
            return_value=True,
        ),
        patch.object(streaming_mod, "_acquire_local_confirm_claim", return_value=True),
    ):
        events = [
            event
            async for event in streaming_mod.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    legacy_visible = [event for event in events if (sse_seq(event) or 0) > old_cursor]
    assert frames_of_type(legacy_visible, "confirmation"), (
        "the active confirm stream restarted below the legacy cursor and hid "
        f"its approval gate: {events!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_id", "terminal_event"),
    [("assistant-msg-1", "done"), (None, "error")],
)
async def test_confirm_canonical_completion_requires_persisted_assistant(
    persisted_id, terminal_event
):
    """In canonical mode the done event must carry assistant_message_id so
    the client can reconcile its optimistic bubble (legacy mode omits it)."""
    from src.api.agent.streaming import stream_confirm_event_generator

    snapshot = _make_snapshot(
        plan=[{"step": 1, "description": "Ingest", "tool": "ingest_arxiv"}]
    )
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    # Connected client — the confirm loop now routes events through
    # _graph_events_with_keepalive, which checks is_disconnected() before
    # pulling each event. A True here would (correctly) trigger the
    # cancel/persist-partial branch instead of a normal completion.
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-1111-1111-111111111111",
        confirmed=True,
        model="gpt-5",
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        patch(
            "src.services.agent.observability.configure_langsmith",
            side_effect=lambda: None,
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
            return_value=_FakeGraph(snapshot),
        ),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=fake_db,
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=AsyncMock(return_value=persisted_id),
        ),
        patch(
            "src.api.agent.streaming._latest_user_client_message_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.streaming._canonical_persistence_enabled",
            return_value=True,
        ),
    ):
        events = []
        async for event in stream_confirm_event_generator(body, request, current_user):
            events.append(event)

    terminal_events = [e for e in events if f"event: {terminal_event}" in e]
    assert len(terminal_events) == 1
    if persisted_id is None:
        assert not [e for e in events if "event: done" in e]
    else:
        assert "assistant_message_id" in terminal_events[0]
        assert persisted_id in terminal_events[0]
