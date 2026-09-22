from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessageChunk

pytestmark = pytest.mark.integration


class _FakeLuna:
    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="hel")
        yield AIMessageChunk(
            content="lo",
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 2,
                "total_tokens": 14,
            },
        )


class _NoGraphExecution:
    def __init__(self):
        self.astream_called = False
        self.aupdate_state = AsyncMock(return_value=None)

    async def astream_events(self, *_args, **_kwargs):
        self.astream_called = True
        raise AssertionError("eligible fast-path turn entered LangGraph execution")
        yield  # pragma: no cover


class _FailingLuna:
    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="partial")
        raise RuntimeError("model stream broke")


class _CancelledLuna:
    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="partial")
        raise asyncio.CancelledError()


class _TokenThenBlockedLuna:
    def __init__(self):
        self.pull_started = asyncio.Event()
        self.closed = asyncio.Event()

    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="partial")
        self.pull_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _CancellationResistantLuna:
    def __init__(self):
        self.pull_started = asyncio.Event()
        self.cancel_received = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="partial")
        self.pull_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_received.set()
            await self.release.wait()
        finally:
            self.closed.set()


async def test_fast_chunks_wait_for_user_persistence_before_release():
    from src.services.agent.fast_path import stream_fast_path_chunks

    model_started = asyncio.Event()
    allow_persist = asyncio.Event()

    class ImmediateModel:
        async def astream(self, _messages, *, config=None):
            model_started.set()
            yield AIMessageChunk(content="first")

    async def persist_user():
        await allow_persist.wait()

    chunks = stream_fast_path_chunks(
        llm=ImmediateModel(),
        messages=[],
        persist_user=persist_user,
    )
    first = asyncio.create_task(anext(chunks))

    await asyncio.wait_for(model_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert first.done() is False

    allow_persist.set()
    assert (await asyncio.wait_for(first, timeout=1)).content == "first"


async def test_fast_path_root_stays_open_through_completion_without_network(
    monkeypatch,
):
    from langsmith import run_helpers
    from langsmith.run_helpers import get_current_run_tree

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory

    class _NoopClient:
        def __init__(self):
            self.created = []
            self.updated = []

        def create_run(self, **kwargs):
            self.created.append(kwargs)

        def update_run(self, **kwargs):
            self.updated.append(kwargs)

    class _TracingLuna:
        async def astream(self, _messages, *, config=None):
            root_run = get_current_run_tree()
            observed["root"] = (root_run, root_run.end_time)
            async with run_helpers.trace("fake_llm", run_type="llm"):
                model_run = get_current_run_tree()
                observed["model"] = (model_run, model_run.end_time)
                yield AIMessageChunk(content="ok")

    client = _NoopClient()
    observed = {}
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    body = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="Explain the sky")],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: _TracingLuna())
    fake_graph = _NoGraphExecution()
    fake_session = SimpleNamespace(close=AsyncMock())

    async def capture_checkpoint(*_args, **_kwargs):
        run = get_current_run_tree()
        observed["checkpoint"] = (run, run.end_time)

    async def capture_finalize(*_args, **_kwargs):
        run = get_current_run_tree()
        observed["finalize"] = (run, run.end_time)

    real_emit = streaming_mod._SeqEmitter.emit

    async def capture_emit(self, event_type, data, *args, **kwargs):
        if event_type is streaming_mod.AgentStreamEvent.DONE:
            run = get_current_run_tree()
            observed["done"] = (run, run.end_time)
        return await real_emit(self, event_type, data, *args, **kwargs)

    real_close = streaming_mod._close_async_iterator

    async def capture_close(iterator):
        run = get_current_run_tree()
        observed["cleanup"] = (run, run.end_time)
        await real_close(iterator)

    with run_helpers.tracing_context(enabled=True, client=client):
        with (
            patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
            patch.object(streaming_mod, "_accept_eligible", return_value=False),
            patch.object(
                streaming_mod,
                "_resolve_thread",
                new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
            ),
            patch.object(
                streaming_mod,
                "_persist_user_message_guarded",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                jobs_mod,
                "_persist_assistant_message_safe",
                new=AsyncMock(return_value="assistant-row-id"),
            ),
            patch.object(
                streaming_mod._stream_buffer,
                "start_stream",
                new=AsyncMock(return_value=None),
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
                new=lambda **_kwargs: fake_graph,
            ),
            patch.object(fake_graph, "aupdate_state", side_effect=capture_checkpoint),
            patch.object(streaming_mod, "_finalize_run", new=capture_finalize),
            patch.object(streaming_mod._SeqEmitter, "emit", new=capture_emit),
            patch.object(streaming_mod, "_close_async_iterator", new=capture_close),
        ):
            events = [
                event
                async for event in streaming_mod.stream_event_generator(
                    body, request, user
                )
            ]

    model_run, model_end_time = observed["model"]
    root_run, root_end_time = observed["root"]
    assert root_run.name == "luna_fast_path"
    assert model_run.parent_run_id == root_run.id
    assert root_run.metadata["trace_source"] == "non_graph"
    assert model_run.metadata["thread_id"] == str(thread.id)
    assert "Explain the sky" not in str(root_run.metadata)
    assert root_end_time is None
    assert model_end_time is None
    for kind in ("checkpoint", "finalize", "done", "cleanup"):
        observed_run, observed_end_time = observed[kind]
        assert observed_run is root_run
        assert observed_end_time is None
    assert any("event: done" in event for event in events)
    root_updates = [
        update
        for update in client.updated
        if str(update.get("run_id")) == str(root_run.id)
    ]
    assert root_updates


async def test_eligible_turn_streams_luna_and_reconciles_checkpoint_before_done(
    monkeypatch,
):
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    user_cmid = uuid4()

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why the sky appears blue",
                client_message_id=user_cmid,
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: _FakeLuna())
    monkeypatch.setenv("AGENT_CANONICAL_PERSISTENCE", "true")

    fake_graph = _NoGraphExecution()
    fake_session = SimpleNamespace(close=AsyncMock())
    persist_user = AsyncMock(return_value=True)
    persist_assistant = AsyncMock(return_value="assistant-row-id")

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=persist_user,
        ),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=persist_assistant,
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
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
            new=lambda **_kwargs: fake_graph,
        ),
    ):
        events = [
            event
            async for event in streaming_mod.stream_event_generator(body, request, user)
        ]

    assert fake_graph.astream_called is False
    persist_user.assert_awaited_once()
    token_frames = [event for event in events if "event: token" in event]
    assert '"content": "hel"' in token_frames[0]
    assert '"content": "lo"' in token_frames[1]

    update_args = fake_graph.aupdate_state.await_args.args
    checkpoint_messages = update_args[1]["messages"]
    assert [message.content for message in checkpoint_messages] == [
        "Explain why the sky appears blue",
        "hello",
    ]
    assert checkpoint_messages[0].id == str(user_cmid)
    assert checkpoint_messages[1].id

    done_index = next(i for i, event in enumerate(events) if "event: done" in event)
    assert fake_graph.aupdate_state.await_count == 1
    assert done_index == len(events) - 1
    persist_kwargs = persist_assistant.await_args.kwargs
    assert persist_kwargs["content"] == "hello"
    assert persist_kwargs["model_name"] == "gpt-5.6-luna"
    fake_session.close.assert_awaited_once()


async def test_ineligible_turn_keeps_langgraph_path(
    monkeypatch,
):
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4(), source_project=None)

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod

    body = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="Search my documents")],
        page_context={"type": "documents"},
        use_rag=True,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)

    class GraphPath:
        def __init__(self):
            self.astream_called = False
            self._snapshot = SimpleNamespace(values={"messages": []}, tasks=())

        async def astream_events(self, *_args, **_kwargs):
            self.astream_called = True
            if False:
                yield

        async def aget_state(self, _config):
            return self._snapshot

    graph = GraphPath()
    fake_session = SimpleNamespace(close=AsyncMock())
    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "_clear_stale_pending_confirmation",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            new=lambda: None,
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
            new=lambda **_kwargs: graph,
        ),
        patch(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    id="",
                    tool_registry_hash="",
                    tool_registry_version="",
                    tool_metadata={},
                    project_skill_catalog=[],
                    loaded_skill_versions=[],
                )
            ),
        ),
    ):
        _events = [
            event
            async for event in streaming_mod.stream_event_generator(body, request, user)
        ]

    assert graph.astream_called is True


@pytest.mark.parametrize(
    ("model", "cancelled"),
    [(_FailingLuna(), False), (_CancelledLuna(), True)],
)
async def test_luna_failure_persists_streamed_partial_as_stopped(
    monkeypatch, model, cancelled
):
    from langsmith import run_helpers

    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    user_cmid = uuid4()

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why rainbows form",
                client_message_id=user_cmid,
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: model)

    fake_session = SimpleNamespace(close=AsyncMock())
    persist_assistant = AsyncMock(return_value="partial-row-id")

    class _NoopClient:
        def __init__(self):
            self.created = []
            self.updated = []

        def create_run(self, **kwargs):
            self.created.append(kwargs)

        def update_run(self, **kwargs):
            self.updated.append(kwargs)

    client = _NoopClient()
    with (
        run_helpers.tracing_context(enabled=True, client=client),
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=persist_assistant,
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
    ):
        events = []
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                async for event in streaming_mod.stream_event_generator(
                    body, request, user
                ):
                    events.append(event)
        else:
            events = [
                event
                async for event in streaming_mod.stream_event_generator(
                    body, request, user
                )
            ]

    assert any("event: token" in event for event in events)
    assert any("event: error" in event for event in events) is (not cancelled)
    persist_assistant.assert_awaited_once()
    assert persist_assistant.await_args.kwargs["content"] == "partial"
    assert persist_assistant.await_args.kwargs["stopped"] is True
    if not cancelled:
        root_id = next(
            created["id"]
            for created in client.created
            if created["name"] == "luna_fast_path"
        )
        root_updates = [
            update
            for update in client.updated
            if str(update.get("run_id")) == str(root_id)
        ]
        assert any(
            "model stream broke" in (update.get("error") or "")
            for update in root_updates
        )


async def test_fast_path_disconnect_terminalizes_before_resistant_pull_cleanup(
    monkeypatch, caplog
):
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory
    from src.services.agent.run_event_types import RunEventType
    from src.shared.enums import JobStatus

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why rainbows form",
                client_message_id=uuid4(),
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(
        is_disconnected=AsyncMock(side_effect=[False, False, True])
    )
    model = _CancellationResistantLuna()
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: model)
    monkeypatch.setattr(streaming_mod, "_SSE_DISCONNECT_POLL_SECONDS", 0.05)
    caplog.set_level("WARNING", logger=streaming_mod.__name__)

    fake_session = SimpleNamespace(close=AsyncMock())
    persist_assistant = AsyncMock(return_value="partial-row-id")
    finish_stream = AsyncMock()
    finalize_calls: list[dict] = []
    finalized = asyncio.Event()

    async def spy_finalize_run(*_args, **kwargs):
        finalize_calls.append(kwargs)
        finalized.set()

    events: list[str] = []
    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            jobs_mod, "_persist_assistant_message_safe", new=persist_assistant
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value="stream-id"),
        ),
        patch.object(streaming_mod._stream_buffer, "append", new=AsyncMock()),
        patch.object(streaming_mod._stream_buffer, "finish_stream", new=finish_stream),
        patch.object(streaming_mod, "_finalize_run", spy_finalize_run),
    ):
        generator = streaming_mod.stream_event_generator(body, request, user)

        async def drain_stream():
            async for event in generator:
                events.append(event)

        drain_task = asyncio.create_task(drain_stream())
        await asyncio.wait_for(model.pull_started.wait(), timeout=1)
        await asyncio.wait_for(
            model.cancel_received.wait(),
            timeout=streaming_mod._SSE_DISCONNECT_POLL_SECONDS + 0.25,
        )
        await asyncio.wait_for(
            finalized.wait(), timeout=streaming_mod._SSE_DISCONNECT_POLL_SECONDS
        )

        assert not model.closed.is_set()
        assert sum("event: token" in event for event in events) == 1
        assert not any("event: done" in event for event in events)
        persist_assistant.assert_awaited_once()
        assert persist_assistant.await_args.kwargs["content"] == "partial"
        assert persist_assistant.await_args.kwargs["stopped"] is True
        finish_stream.assert_awaited_once_with(str(thread.id), "stream-id")
        assert len(finalize_calls) == 1

        await asyncio.wait_for(
            drain_task,
            timeout=streaming_mod._SSE_DISCONNECT_POLL_SECONDS + 0.25,
        )
        assert not model.closed.is_set()
        warning = next(
            record
            for record in caplog.records
            if record.getMessage() == "Timed out settling cancelled agent stream pull"
        )
        assert warning.event == "agent_stream_pull_cleanup_timeout"

        model.release.set()
        await asyncio.wait_for(model.closed.wait(), timeout=0.1)
        await asyncio.sleep(0)

    assert len(finalize_calls) == 1
    finalized = finalize_calls[0]
    assert finalized["status"] is JobStatus.CANCELLED
    assert finalized["event_type"] is RunEventType.RUN_CANCELLED
    assert finalized["payload"]["reason"] == "client_disconnected"
    assert finalized["payload"]["assistant_message_id"] == "partial-row-id"


async def test_fast_path_durable_stop_terminalizes_without_transport_disconnect(
    monkeypatch,
):
    """A durable Stop closes Luna and records user_requested independently of SSE disconnect."""
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory
    from src.services.agent.run_event_types import RunEventType
    from src.shared.enums import JobStatus

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why rainbows form",
                client_message_id=uuid4(),
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    model = _TokenThenBlockedLuna()
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: model)

    acceptance = streaming_mod.AcceptedSubmission(
        run_id=str(uuid4()),
        thread_id=str(thread.id),
        user_message_id=str(uuid4()),
        outbox_id=str(uuid4()),
        idempotency_key="durable-stop",
    )
    persist_assistant = AsyncMock(return_value="partial-row-id")
    finalize = AsyncMock(return_value=True)
    stop_requested = AsyncMock(side_effect=[False, False, False, False, True])
    fake_session = SimpleNamespace(close=AsyncMock())

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=True),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod, "accept_submission", new=AsyncMock(return_value=acceptance)
        ),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=True),
        ),
        patch.object(streaming_mod, "is_run_cancellation_requested", stop_requested),
        patch.object(
            jobs_mod,
            "_persist_assistant_message_safe",
            new=persist_assistant,
        ),
        patch.object(streaming_mod, "_finalize_run", finalize),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value="stream-id"),
        ),
        patch.object(streaming_mod._stream_buffer, "append", new=AsyncMock()),
        patch.object(
            streaming_mod._stream_buffer,
            "finish_stream",
            new=AsyncMock(),
        ),
    ):
        events = [
            event
            async for event in streaming_mod.stream_event_generator(body, request, user)
        ]

    assert model.closed.is_set()
    assert any("event: token" in event for event in events)
    assert not any("event: done" in event for event in events)
    assert not any("event: error" in event for event in events)
    persist_assistant.assert_awaited_once()
    assert persist_assistant.await_args.kwargs["content"] == "partial"
    assert persist_assistant.await_args.kwargs["stopped"] is True
    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["status"] is JobStatus.CANCELLED
    assert finalize.await_args.kwargs["event_type"] is RunEventType.RUN_CANCELLED
    assert finalize.await_args.kwargs["payload"]["reason"] == "user_requested"


class _CancelDuringEmitLuna:
    """Second chunk's emit is where the cancel lands (see emit patch below)."""

    async def astream(self, _messages, *, config=None):
        yield AIMessageChunk(content="The ar")
        yield AIMessageChunk(content="X")


@pytest.mark.parametrize(
    "close_at", ["routing", "trace", "token", "heartbeat", "usage"]
)
async def test_fast_path_generator_close_finalizes_each_window(monkeypatch, close_at):
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory
    from src.services.agent.run_event_types import RunEventType

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why rainbows form",
                client_message_id=uuid4(),
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    model = _TokenThenBlockedLuna() if close_at == "heartbeat" else _FakeLuna()
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: model)
    if close_at == "heartbeat":
        monkeypatch.setattr(streaming_mod, "_SSE_KEEPALIVE_SECONDS", 0.01)

    fake_graph = _NoGraphExecution()
    fake_session = SimpleNamespace(close=AsyncMock())
    persist_assistant = AsyncMock(return_value="partial-row-id")
    finalize_calls: list[dict] = []

    async def spy_finalize_run(*_args, **kwargs):
        finalize_calls.append(kwargs)

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            jobs_mod, "_persist_assistant_message_safe", new=persist_assistant
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
        patch.object(streaming_mod, "_finalize_run", spy_finalize_run),
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
            new=lambda **_kwargs: fake_graph,
        ),
    ):
        generator = streaming_mod.stream_event_generator(body, request, user)
        async for event in generator:
            if (
                (close_at == "routing" and "Using the direct Luna path" in event)
                or (close_at == "trace" and "event: trace" in event)
                or (close_at == "token" and "event: token" in event)
                or (close_at == "heartbeat" and "event: heartbeat" in event)
                or (close_at == "usage" and "event: usage" in event)
            ):
                break
        if close_at == "heartbeat":
            assert model.pull_started.is_set()
            await asyncio.wait_for(generator.aclose(), timeout=0.1)
            assert model.closed.is_set()
        else:
            await generator.aclose()

    if close_at in {"routing", "trace"}:
        persist_assistant.assert_not_awaited()
    else:
        persist_assistant.assert_awaited_once()
        assert persist_assistant.await_args.kwargs["stopped"] is (
            close_at in {"token", "heartbeat"}
        )
    assert finalize_calls[0]["event_type"] is RunEventType.RUN_CANCELLED
    if close_at in {"token", "heartbeat", "usage"}:
        assert finalize_calls[0]["payload"]["assistant_message_id"] == "partial-row-id"
    else:
        assert "assistant_message_id" not in finalize_calls[0]["payload"]


async def test_fast_path_cancel_links_partial_and_keeps_prefix(monkeypatch):
    """Cancel inside the 2nd token's emit: persisted partial must stop at the
    1st token (prefix invariant), and the run.cancelled payload must name the
    stopped row (linkage invariant). Mirrors the graph-path guarantees from
    PR #1350 (audit 2026-08-07, gap 2)."""
    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())

    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why rainbows form",
                client_message_id=uuid4(),
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(
        llm_factory, "build_fast_path_llm", lambda: _CancelDuringEmitLuna()
    )

    fake_session = SimpleNamespace(close=AsyncMock())
    persist_assistant = AsyncMock(return_value="partial-row-id")
    finalize_calls: list[dict] = []

    async def spy_finalize_run(*_args, **kwargs):
        finalize_calls.append(kwargs)

    real_emit = streaming_mod._SeqEmitter.emit

    async def emit_then_cancel(self, event_type, data, *args, **kwargs):
        if (
            event_type == streaming_mod.AgentStreamEvent.TOKEN
            and (data or {}).get("content") == "X"
        ):
            raise asyncio.CancelledError()
        return await real_emit(self, event_type, data, *args, **kwargs)

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=False),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            jobs_mod, "_persist_assistant_message_safe", new=persist_assistant
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value=None),
        ),
        patch.object(streaming_mod._SeqEmitter, "emit", emit_then_cancel),
        patch.object(streaming_mod, "_finalize_run", spy_finalize_run),
    ):
        with pytest.raises(asyncio.CancelledError):
            async for _event in streaming_mod.stream_event_generator(
                body, request, user
            ):
                pass

    # Prefix invariant: the chunk whose emit was cancelled must NOT be
    # persisted — only the first, fully-buffered token.
    persist_assistant.assert_awaited_once()
    assert persist_assistant.await_args.kwargs["content"] == "The ar"
    assert persist_assistant.await_args.kwargs["stopped"] is True

    # Linkage invariant: the run.cancelled payload names the stopped row.
    from src.services.agent.run_event_types import RunEventType, validate_payload

    cancelled = [
        c for c in finalize_calls if c.get("event_type") is RunEventType.RUN_CANCELLED
    ]
    assert cancelled, f"expected run.cancelled finalize, got {finalize_calls!r}"
    # cancel_fast_path's own _finalize_run call is first and authoritative:
    # its CancelledError re-raise also trips stream_event_generator's outer,
    # route-agnostic CancelledError handler, which issues its own (unlinked)
    # finalize call second. In production that second call is a no-op —
    # finalize_submission's terminal-status guard and append_event's
    # RunAlreadyTerminalError absorb it — but this spy bypasses that
    # idempotency layer, so assert on the first, actually-persisted call.
    payload = cancelled[0]["payload"]
    assert payload.get("assistant_message_id") == "partial-row-id"
    validate_payload(RunEventType.RUN_CANCELLED.value, payload)


@pytest.mark.asyncio
async def test_fast_path_binds_buffer_to_durable_run_id(monkeypatch):
    """Audit S2-H1 regression: with a committed acceptance, the fast-path
    emitter must start its replay buffer bound to the durable run id so an
    idempotent cmid retry can attach via stream_id_for_run."""
    from src.api.agent import streaming as streaming_mod
    from src.api.agent.execute import AgentExecuteRequest, AgentMessage
    from src.core.config import get_settings
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent import llm_factory
    from src.services.agent.agent_submission_service import AcceptedSubmission

    user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
    thread = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    run_id = str(uuid4())

    body = AgentExecuteRequest(
        messages=[
            AgentMessage(
                role="user",
                content="Explain why the sky appears blue",
                client_message_id=uuid4(),
            )
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(thread.id),
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: _FakeLuna())
    monkeypatch.setenv("AGENT_CANONICAL_PERSISTENCE", "true")

    acceptance = AcceptedSubmission(
        run_id=run_id,
        thread_id=str(thread.id),
        user_message_id=None,
        outbox_id=None,
        idempotency_key=None,
    )
    fake_graph = _NoGraphExecution()
    fake_session = SimpleNamespace(close=AsyncMock())
    start_stream = AsyncMock(return_value="sid-fast")

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(streaming_mod, "_accept_eligible", return_value=True),
        patch.object(
            streaming_mod, "accept_submission", new=AsyncMock(return_value=acceptance)
        ),
        patch.object(streaming_mod, "mark_submission_dispatched", new=AsyncMock()),
        patch.object(
            streaming_mod, "_finalize_run_id", new=AsyncMock(return_value=None)
        ),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, str(thread.conversation_id))),
        ),
        patch.object(streaming_mod._stream_buffer, "start_stream", new=start_stream),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-row-id"),
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
            new=lambda **_kwargs: fake_graph,
        ),
    ):
        events = [
            event
            async for event in streaming_mod.stream_event_generator(body, request, user)
        ]

    start_stream.assert_awaited_once_with(str(thread.id), run_id=run_id)
    assert any("event: done" in event for event in events)
    assert fake_graph.astream_called is False
