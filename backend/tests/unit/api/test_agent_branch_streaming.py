"""Native branch routing, prompt isolation and canonical persistence binding.

The real stream generator and branch message builder run with provider,
acceptance and persistence boundaries isolated. Checkpoint reducer semantics
are exercised separately in test_agent_branch_history.py.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent import streaming
from src.core.config import get_settings
from src.models.chat_message import ChatMessage, MessageRole
from src.models.user import User
from src.services.agent import agent_execution_service, branch_history, runtime_snapshot
from src.services.agent.agent_submission_service import AcceptedSubmission
from src.services.agent.schemas import AgentExecuteRequest
from src.services.threads import message_branches
from src.shared.enums import JobStatus
from tests.utils.agent_stream import frames_of_type, make_stream_request


class _RecordingGraph:
    def __init__(self) -> None:
        self.initial_state: dict[str, Any] | None = None
        self.config: dict[str, Any] | None = None

    async def astream_events(
        self, state: dict[str, Any], *, config: dict[str, Any], version: str
    ) -> AsyncIterator[dict[str, Any]]:
        self.initial_state = state
        self.config = config
        yield {
            "event": "on_chat_model_stream",
            "name": "llm_node",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"chunk": SimpleNamespace(content="branch answer")},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {"output": self._values()},
        }

    def _values(self) -> dict[str, Any]:
        messages = (
            [AIMessage(content="branch answer", id="new-assistant")]
            if self.initial_state is not None
            else []
        )
        return {"messages": messages, "tool_executions": [], "retrieved_contexts": []}

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(values=self._values(), tasks=())


@dataclass
class _StreamHarness:
    body: AgentExecuteRequest
    user: User
    db: AsyncSession
    request: Any
    acceptance: AcceptedSubmission
    graph: _RecordingGraph
    lookup: AsyncMock
    persist: AsyncMock
    finalize: AsyncMock
    legacy_history: Mock
    server_history: AsyncMock
    stop_requested: bool = False

    def stream(self) -> AsyncIterator[str]:
        return cast(
            AsyncIterator[str],
            streaming.stream_event_generator(self.body, self.request, self.user),
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _StreamHarness:
    thread_id, user_id, org_id, document_id, accepted_id = [uuid4() for _ in range(5)]
    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": "request history must not choose the branch",
                "client_message_id": str(uuid4()),
            }
        ],
        thread_id=str(thread_id),
        branch_document_id=document_id,
        attachment_ids=[document_id],
        parent_message_id=uuid4(),
        document_only=True,
    )
    acceptance = AcceptedSubmission(
        run_id=str(uuid4()),
        thread_id=str(thread_id),
        user_message_id=str(accepted_id),
        outbox_id=str(uuid4()),
        idempotency_key="branch-stream-test",
    )
    db = AsyncMock(spec=AsyncSession)
    graph = _RecordingGraph()
    lookup = AsyncMock(
        return_value=[
            ChatMessage(
                id=accepted_id,
                role=MessageRole.USER,
                content="canonical accepted prompt",
            )
        ]
    )
    legacy = Mock(side_effect=AssertionError("Branch must never use request history"))
    server = AsyncMock(
        side_effect=AssertionError("Branch must never use chronological history")
    )
    state = _StreamHarness(
        body=body,
        user=cast(User, SimpleNamespace(id=user_id, organization_id=org_id)),
        db=db,
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        acceptance=acceptance,
        graph=graph,
        lookup=lookup,
        persist=AsyncMock(return_value=str(uuid4())),
        finalize=AsyncMock(return_value=True),
        legacy_history=legacy,
        server_history=server,
    )

    async def poll_stop() -> bool:
        return state.stop_requested

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_SERVER_SIDE_HISTORY", False)
    # Even a fully enabled fast path must not bypass the bound attachment.
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(streaming, "AsyncSessionLocal", Mock(return_value=db))
    monkeypatch.setattr(
        streaming,
        "_resolve_thread",
        AsyncMock(return_value=(SimpleNamespace(id=thread_id), None)),
    )
    monkeypatch.setattr(streaming, "_accept_eligible", Mock(return_value=True))
    monkeypatch.setattr(
        streaming, "accept_submission", AsyncMock(return_value=acceptance)
    )
    monkeypatch.setattr(
        streaming, "mark_submission_dispatched", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        streaming, "_resolve_and_bind_project", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(streaming, "_durable_stop_poller", Mock(return_value=poll_stop))
    monkeypatch.setattr(streaming, "_finalize_run", state.finalize)
    monkeypatch.setattr(
        streaming, "_cancel_current_task_on_disconnect", Mock(return_value=None)
    )
    monkeypatch.setattr(streaming._SeqEmitter, "start", AsyncMock())
    monkeypatch.setattr(branch_history, "get_branch_path", lookup)
    monkeypatch.setattr(
        branch_history, "load_branch_file_contexts", AsyncMock(return_value={})
    )
    monkeypatch.setattr(agent_execution_service, "build_user_history_messages", legacy)
    monkeypatch.setattr(agent_execution_service, "build_graph_input_messages", server)
    monkeypatch.setattr(
        agent_execution_service, "_persist_assistant_message_safe", state.persist
    )
    monkeypatch.setattr(
        agent_execution_service, "_run_heartbeat", lambda _run_id: nullcontext()
    )
    monkeypatch.setattr(
        runtime_snapshot,
        "create_runtime_snapshot",
        AsyncMock(return_value=runtime_snapshot.empty_runtime_snapshot()),
    )
    monkeypatch.setattr("src.services.agent.observability.configure_langsmith", Mock())
    monkeypatch.setattr(
        "src.services.agent.checkpointer.get_checkpointer",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "src.services.agent.memory.get_memory_store", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        "src.services.agent.graph.compile_agent_graph", Mock(return_value=graph)
    )
    return state


@pytest.mark.parametrize("reasoning_effort", [None, "high"])
async def test_branch_overrides_disabled_server_history_and_resets_identity(
    harness: _StreamHarness,
    monkeypatch: pytest.MonkeyPatch,
    reasoning_effort: str | None,
) -> None:
    if reasoning_effort is not None:
        settings = get_settings()
        monkeypatch.setattr(settings, "AGENT_USE_RESPONSES_API", True)
        monkeypatch.setattr(
            settings, "AZURE_OPENAI_CHAT_ENDPOINT", "https://example.invalid/v1"
        )
        harness.body = AgentExecuteRequest.model_validate(
            {
                **harness.body.model_dump(),
                "model": "gpt-5-mini",
                "reasoning_effort": reasoning_effort,
            }
        )
    frames = [frame async for frame in harness.stream()]
    assert frames_of_type(frames, "done"), frames
    harness.lookup.assert_awaited_once_with(
        harness.db,
        thread_id=UUID(harness.acceptance.thread_id),
        head_message_id=UUID(harness.acceptance.user_message_id),
    )
    harness.legacy_history.assert_not_called()
    harness.server_history.assert_not_awaited()
    state = harness.graph.initial_state
    assert state is not None
    assert isinstance(state["messages"][0], RemoveMessage)
    assert state["messages"][0].id == REMOVE_ALL_MESSAGES
    assert isinstance(state["messages"][1], HumanMessage)
    assert state["messages"][1].content == "canonical accepted prompt"
    assert state["messages"][1].id == harness.acceptance.user_message_id
    assert state["identity_ledger"] == {}
    assert state["identity_current_references"] == []
    assert state["user_memories"] == []
    assert state["project_memories"] == []
    assert state["retrieved_contexts"] == []
    assert state["document_only"] is True
    assert state["reasoning_effort"] == reasoning_effort
    harness.persist.assert_awaited_once()
    assert harness.persist.await_args is not None
    assert (
        harness.persist.await_args.kwargs["parent_message_id"]
        == harness.acceptance.user_message_id
    )
    assert harness.persist.await_args.kwargs["parent_message_id"] != str(
        harness.body.parent_message_id
    )
    assert harness.persist.await_args.kwargs["stopped"] is False


@pytest.mark.parametrize(
    "failure", [RuntimeError("read unavailable"), ValueError("corrupt branch")]
)
async def test_branch_lookup_failure_never_invokes_model_or_legacy_history(
    harness: _StreamHarness, failure: Exception
) -> None:
    harness.lookup.side_effect = failure
    frames = [frame async for frame in harness.stream()]
    assert frames_of_type(frames, "error"), frames
    assert not frames_of_type(frames, "done")
    assert not frames_of_type(frames, "token")
    assert harness.graph.initial_state is None
    harness.legacy_history.assert_not_called()
    harness.server_history.assert_not_awaited()
    harness.persist.assert_not_awaited()
    assert harness.finalize.await_args is not None
    assert harness.finalize.await_args.kwargs["status"] == JobStatus.FAILED


async def test_branch_stop_persists_partial_under_exact_accepted_user(
    harness: _StreamHarness,
) -> None:
    frames = []
    async for frame in harness.stream():
        frames.append(frame)
        if frames_of_type([frame], "token"):
            harness.stop_requested = True
    assert frames_of_type(frames, "token"), frames
    assert not frames_of_type(frames, "done")
    harness.persist.assert_awaited_once()
    assert harness.persist.await_args is not None
    assert harness.persist.await_args.kwargs["content"] == "branch answer"
    assert harness.persist.await_args.kwargs["stopped"] is True
    assert (
        harness.persist.await_args.kwargs["parent_message_id"]
        == harness.acceptance.user_message_id
    )
    assert harness.finalize.await_args is not None
    assert harness.finalize.await_args.kwargs["status"] == JobStatus.CANCELLED


async def test_queued_execute_rejects_branch_before_dispatch(
    harness: _StreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = importlib.import_module("src.api.agent.execute")
    resolve = AsyncMock(
        side_effect=AssertionError("Branch must not reach queued execution")
    )
    monkeypatch.setattr(execute, "_resolve_thread", resolve)
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as caught:
        await execute.execute_agent(harness.body, tasks, harness.user, harness.db)
    assert caught.value.status_code == 422
    assert caught.value.detail == "Message branches require /api/v1/agent/stream."
    resolve.assert_not_awaited()
    assert tasks.tasks == []


@pytest.mark.parametrize("failure", ["revoked_pdf", "missing_ancestor", "foreign_run"])
async def test_branch_confirmation_reauthorizes_before_revealing_saved_context(
    harness: _StreamHarness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Revocation during an approval wait must hide cached PDF excerpts.

    Mutation proof: invert streaming.py's resolved-thread branching guard
    and run this test's [revoked_pdf] case. It fails because authorization
    was never awaited and approval was reached. Restore the guard afterwards.
    """
    snapshot = SimpleNamespace(
        values={
            "user_id": str(harness.user.id),
            "thread_id": harness.body.thread_id,
            "thread_persistence": "durable",
            "messages": [HumanMessage(content="accepted question")],
            "retrieved_contexts": [{"snippet": "revoked PDF private excerpt"}],
        },
        config={"configurable": {"checkpoint_id": "parked-checkpoint"}},
        tasks=(),
    )
    monkeypatch.setattr(harness.graph, "aget_state", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        streaming,
        "_resolve_thread",
        AsyncMock(return_value=(SimpleNamespace(message_branching=True), None)),
    )
    monkeypatch.setattr(
        streaming,
        "get_active_run_for_thread",
        AsyncMock(
            return_value=SimpleNamespace(
                job_id=harness.acceptance.run_id,
                user_message_id=harness.acceptance.user_message_id,
            )
        ),
    )
    reauthorize = AsyncMock(
        side_effect=message_branches.BranchAccessError(404, f"Unavailable: {failure}")
    )
    monkeypatch.setattr(message_branches, "authorize_branch_resume", reauthorize)
    approval = Mock(side_effect=AssertionError("No approval after revoked branch"))
    monkeypatch.setattr(streaming, "require_approval", approval)
    body = SimpleNamespace(
        thread_id=harness.body.thread_id,
        confirmed=True,
        approval_id="a" * 64,
    )

    frames = [
        frame
        async for frame in streaming.stream_confirm_event_generator(
            body, harness.request, harness.user
        )
    ]

    reauthorize.assert_awaited_once_with(
        harness.db,
        actor_id=harness.user.id,
        thread_id=UUID(harness.acceptance.thread_id),
        run_id=harness.acceptance.run_id,
    )
    assert frames_of_type(frames, "error"), frames
    assert not frames_of_type(frames, "rag_context")
    assert not frames_of_type(frames, "token")
    assert "revoked PDF private excerpt" not in "".join(frames)
    assert harness.graph.initial_state is None
    approval.assert_not_called()
    harness.persist.assert_not_awaited()
