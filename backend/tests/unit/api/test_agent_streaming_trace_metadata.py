"""The real graph invocation receives bounded correlation-only metadata."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.user import User
from tests.utils.agent_thread_access import editable_thread_getter


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


from src.services.agent.agent_submission_service import AcceptedSubmission
from src.services.agent.runtime_snapshot import empty_runtime_snapshot
from tests.utils.agent_stream import make_stream_request

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
THREAD_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CLIENT_MESSAGE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
RUN_ID = "55555555-5555-5555-5555-555555555555"
USER_MESSAGE_ID = "66666666-6666-6666-6666-666666666666"
REQUEST_ID = "request-trace-123"
PROMPT = "synthetic prompt content that must not enter trace metadata"


class _CapturingGraph:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    async def astream_events(
        self, *_args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.config = kwargs["config"]
        if False:
            yield {}

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(values={"messages": []}, tasks=())


async def _capture_stream_config(
    *, durable: bool, accept: bool | None = None
) -> dict[str, Any]:
    from src.api.agent import streaming as streaming_mod

    graph = _CapturingGraph()
    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": PROMPT,
                "client_message_id": str(CLIENT_MESSAGE_ID),
            }
        ],
        thread_id=str(THREAD_ID) if durable else None,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(request_id=REQUEST_ID),
        is_disconnected=AsyncMock(return_value=False),
    )
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    accepted = AcceptedSubmission(
        run_id=RUN_ID,
        thread_id=str(THREAD_ID),
        user_message_id=USER_MESSAGE_ID,
        outbox_id="outbox-1",
        idempotency_key="key-1",
    )
    thread_obj = SimpleNamespace(id=THREAD_ID)

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=AsyncMock()),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(
                return_value=(thread_obj, "conversation-1") if durable else (None, None)
            ),
        ),
        patch.object(
            streaming_mod,
            "_accept_eligible",
            return_value=durable if accept is None else accept,
        ),
        patch.object(
            streaming_mod,
            "accept_submission",
            new=AsyncMock(return_value=accepted),
        ),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "_finalize_run",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
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
            "src.services.agent.fast_path.classify_fast_path_turn",
            return_value=SimpleNamespace(eligible=False),
        ),
        patch(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            new=AsyncMock(return_value=empty_runtime_snapshot()),
        ),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-1"),
        ),
    ):
        async for _frame in streaming_mod.stream_event_generator(
            body, request, current_user
        ):
            pass

    assert graph.config is not None
    return graph.config


@asynccontextmanager
async def _session_context(db: Any) -> AsyncIterator[Any]:
    yield db


@pytest.mark.asyncio
async def test_stream_graph_config_correlates_trace_to_release_and_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")

    config = await _capture_stream_config(durable=True)
    metadata = config["metadata"]

    assert metadata == {
        "trace_source": "graph",
        "user_id": str(USER_ID),
        "org_id": str(ORG_ID),
        "thread_id": str(THREAD_ID),
        "request_id": REQUEST_ID,
        "agent_run_id": RUN_ID,
        "user_message_id": USER_MESSAGE_ID,
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "deployment_sha": "deployment-sha-123",
        "image_tag": "backend-image-456",
    }
    assert config["run_name"] == "agent:stream"
    assert PROMPT not in str(metadata)


@pytest.mark.asyncio
async def test_stream_graph_config_degrades_without_durable_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")

    config = await _capture_stream_config(durable=False)
    metadata = config["metadata"]

    assert metadata == {
        "trace_source": "graph",
        "user_id": str(USER_ID),
        "org_id": str(ORG_ID),
        "request_id": REQUEST_ID,
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "deployment_sha": "deployment-sha-123",
        "image_tag": "backend-image-456",
    }
    assert config["run_name"] == "agent:stream"
    assert (
        not {
            "thread_id",
            "agent_run_id",
            "user_message_id",
        }
        & metadata.keys()
    )


@pytest.mark.asyncio
async def test_stream_graph_config_keeps_owned_thread_without_durable_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")

    config = await _capture_stream_config(durable=True, accept=False)

    assert config["metadata"]["thread_id"] == str(THREAD_ID)
    assert "agent_run_id" not in config["metadata"]
    assert config["run_name"] == "agent:stream"


@pytest.mark.asyncio
async def test_background_graph_config_uses_same_correlation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution_mod

    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")
    job_id = "77777777-7777-7777-7777-777777777777"
    unverified_thread_id = "88888888-8888-8888-8888-888888888888"
    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": PROMPT,
                "client_message_id": str(CLIENT_MESSAGE_ID),
            }
        ],
        thread_id=unverified_thread_id,
    )
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())
    graph.aget_state = AsyncMock(return_value=None)
    db = AsyncMock()

    with (
        patch.object(
            execution_mod,
            "AsyncSessionLocal",
            return_value=_session_context(db),
        ),
        patch.object(
            execution_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
        patch.object(
            execution_mod,
            "_resolve_and_bind_project",
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
            return_value=graph,
        ),
        patch(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            new=AsyncMock(return_value=empty_runtime_snapshot()),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith",
            return_value=None,
        ),
        patch.object(
            execution_mod,
            "_set_job_async",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await execution_mod._run_agent_graph(job_id, body, current_user)

    assert graph.ainvoke.await_args is not None
    config = cast(dict[str, Any], graph.ainvoke.await_args.kwargs["config"])
    metadata = config["metadata"]
    assert config["run_name"] == "agent:background"
    assert metadata == {
        "trace_source": "graph",
        "user_id": str(USER_ID),
        "org_id": str(ORG_ID),
        "agent_run_id": job_id,
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "deployment_sha": "deployment-sha-123",
        "image_tag": "backend-image-456",
    }
    assert config["configurable"]["thread_id"] == unverified_thread_id
    assert unverified_thread_id not in str(metadata)
    assert PROMPT not in str(metadata)


@pytest.mark.asyncio
async def test_luna_producer_does_not_pass_trace_inputs_to_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import streaming as streaming_mod

    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")

    class _CapturingLuna:
        def __init__(self) -> None:
            self.config: dict[str, Any] | None = None

        async def astream(self, _messages: Any) -> AsyncIterator[Any]:
            yield SimpleNamespace(content="ok")

    luna = _CapturingLuna()
    graph = SimpleNamespace(aupdate_state=AsyncMock(return_value=None))
    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": PROMPT,
                "client_message_id": str(CLIENT_MESSAGE_ID),
            }
        ],
        thread_id=str(THREAD_ID),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(request_id=REQUEST_ID),
        is_disconnected=AsyncMock(return_value=False),
    )
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    accepted = AcceptedSubmission(
        run_id=RUN_ID,
        thread_id=str(THREAD_ID),
        user_message_id=USER_MESSAGE_ID,
        outbox_id="outbox-1",
        idempotency_key="key-1",
    )
    settings = SimpleNamespace(
        AGENT_FAST_PATH_ENABLED=True,
        AGENT_FAST_PATH_MAX_INPUT_CHARS=4096,
        AGENT_FAST_PATH_REQUEST_TIMEOUT=30,
    )

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=AsyncMock()),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(
                return_value=(SimpleNamespace(id=THREAD_ID), "conversation-1")
            ),
        ),
        patch.object(
            streaming_mod,
            "accept_submission",
            new=AsyncMock(return_value=accepted),
        ),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod,
            "_finalize_run",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.observability.configure_langsmith", return_value=None
        ),
        patch(
            "src.core.config.get_settings",
            return_value=settings,
        ),
        patch(
            "src.services.agent.fast_path.classify_fast_path_turn",
            return_value=SimpleNamespace(eligible=True),
        ),
        patch(
            "src.services.agent.llm_factory._resolve_fast_path_deployment",
            return_value="luna-test",
        ),
        patch("src.services.agent.llm_factory.build_fast_path_llm", return_value=luna),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-1"),
        ),
    ):
        async for _frame in streaming_mod.stream_event_generator(
            body, request, current_user
        ):
            pass

    assert luna.config is None


class _ResumeCapturingGraph:
    def __init__(self, *, cancel_invoke: bool = False) -> None:
        self.stream_config: dict[str, Any] | None = None
        self.invoke_config: dict[str, Any] | None = None
        self.cancel_invoke = cancel_invoke
        self.snapshot = SimpleNamespace(
            values={
                "user_id": str(USER_ID),
                "page_context": {},
                "messages": [],
                "tool_executions": [],
            },
            tasks=(SimpleNamespace(interrupts=[object()]),),
            config={"configurable": {"checkpoint_id": "checkpoint-1"}},
        )

    async def aget_state(self, _config: Any) -> Any:
        return self.snapshot

    async def astream_events(
        self, *_args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.stream_config = kwargs["config"]
        if False:
            yield {}

    async def ainvoke(self, *_args: Any, **kwargs: Any) -> Any:
        self.invoke_config = kwargs["config"]
        if self.cancel_invoke:
            raise asyncio.CancelledError()
        return {}


@pytest.mark.asyncio
async def test_streaming_confirmation_root_uses_owned_durable_run_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import streaming as streaming_mod

    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")
    graph = _ResumeCapturingGraph()
    graph.snapshot.tasks = ()
    db = SimpleNamespace(close=AsyncMock())
    request = SimpleNamespace(
        state=SimpleNamespace(request_id=REQUEST_ID),
        is_disconnected=AsyncMock(return_value=False),
    )
    body = SimpleNamespace(thread_id=str(THREAD_ID), confirmed=True, model="")
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    durable_run = SimpleNamespace(
        job_id=RUN_ID,
        thread_id=THREAD_ID,
        user_message_id=USER_MESSAGE_ID,
        client_message_id=str(CLIENT_MESSAGE_ID),
    )

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=db),
        patch.object(
            streaming_mod,
            "get_active_run_for_thread",
            new=AsyncMock(return_value=durable_run),
        ),
        patch.object(
            streaming_mod,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod, "_finalize_run_id", new=AsyncMock(return_value=None)
        ),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
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
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        async for _ in streaming_mod.stream_confirm_event_generator(
            body, request, current_user
        ):
            pass

    assert graph.stream_config is not None
    assert graph.stream_config["metadata"] == {
        "trace_source": "graph",
        "user_id": str(USER_ID),
        "org_id": str(ORG_ID),
        "thread_id": str(THREAD_ID),
        "request_id": REQUEST_ID,
        "agent_run_id": RUN_ID,
        "user_message_id": USER_MESSAGE_ID,
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "deployment_sha": "deployment-sha-123",
        "image_tag": "backend-image-456",
    }
    assert graph.stream_config["run_name"] == "agent:stream:resume"


@pytest.mark.asyncio
async def test_job_confirmation_root_uses_owned_durable_run_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import agent_execution_service as execution_mod

    monkeypatch.setenv("GIT_SHA", "deployment-sha-123")
    monkeypatch.setenv("IMAGE_TAG", "backend-image-456")
    graph = _ResumeCapturingGraph(cancel_invoke=True)
    db = AsyncMock()
    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": PROMPT,
                "client_message_id": str(CLIENT_MESSAGE_ID),
            }
        ],
        thread_id=str(THREAD_ID),
    )
    job_id = RUN_ID
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    durable_run = SimpleNamespace(
        job_id=job_id,
        thread_id=THREAD_ID,
        user_message_id=USER_MESSAGE_ID,
        client_message_id=str(CLIENT_MESSAGE_ID),
    )

    with (
        patch.object(
            execution_mod, "AsyncSessionLocal", return_value=_session_context(db)
        ),
        patch.object(
            execution_mod,
            "_get_job",
            return_value={"request": body.model_dump()},
        ),
        patch.object(execution_mod, "get_run", new=AsyncMock(return_value=durable_run)),
        patch.object(execution_mod, "_set_job_async", new=AsyncMock(return_value=None)),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        with pytest.raises(asyncio.CancelledError):
            await execution_mod._resume_agent_graph(job_id, True, current_user)

    assert graph.invoke_config is not None
    assert graph.invoke_config["metadata"] == {
        "trace_source": "graph",
        "user_id": str(USER_ID),
        "org_id": str(ORG_ID),
        "thread_id": str(THREAD_ID),
        "request_id": job_id,
        "agent_run_id": job_id,
        "user_message_id": USER_MESSAGE_ID,
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "deployment_sha": "deployment-sha-123",
        "image_tag": "backend-image-456",
    }
    assert graph.invoke_config["run_name"] == "agent:background:resume"


@pytest.mark.asyncio
async def test_stream_confirmation_binds_buffer_to_durable_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit S2-H1 regression: the confirm/resume emitter must start its
    replay buffer bound to the durable run so reconnecting clients can
    address it via stream_id_for_run."""
    from src.api.agent import streaming as streaming_mod

    # A successful confirm deliberately HOLDS its local CX1 claim for the
    # 330s TTL, so this test must use a thread id no earlier test claimed.
    bind_thread_id = uuid.uuid4()
    graph = _ResumeCapturingGraph()
    graph.snapshot.tasks = ()
    db = SimpleNamespace(close=AsyncMock())
    request = SimpleNamespace(
        state=SimpleNamespace(request_id=REQUEST_ID),
        is_disconnected=AsyncMock(return_value=False),
    )
    body = SimpleNamespace(thread_id=str(bind_thread_id), confirmed=True, model="")
    current_user = cast(User, SimpleNamespace(id=USER_ID, organization_id=ORG_ID))
    durable_run = SimpleNamespace(
        job_id=RUN_ID,
        thread_id=str(bind_thread_id),
        user_message_id=USER_MESSAGE_ID,
        client_message_id=str(CLIENT_MESSAGE_ID),
    )
    start_stream = AsyncMock(return_value="sid-resume")

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=db),
        patch.object(
            streaming_mod,
            "get_active_run_for_thread",
            new=AsyncMock(return_value=durable_run),
        ),
        patch.object(
            streaming_mod,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod, "_finalize_run_id", new=AsyncMock(return_value=None)
        ),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value="assistant-row-1"),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value="sid-original"),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "read_after",
            new=AsyncMock(return_value=[SimpleNamespace(seq=7)]),
        ),
        patch.object(streaming_mod._stream_buffer, "start_stream", new=start_stream),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
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
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        frames = [
            frame
            async for frame in streaming_mod.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    if not start_stream.await_count:
        raise AssertionError(
            "start_stream not awaited; frames=" + repr([f[:200] for f in frames])
        )
    start_stream.assert_awaited_once_with(str(bind_thread_id), run_id=RUN_ID)
