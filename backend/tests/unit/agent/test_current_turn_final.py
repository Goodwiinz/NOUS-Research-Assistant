"""Current-turn terminal message and result extraction contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from tests.utils.agent_approval import isolated_confirmation_identity  # noqa: F401

pytestmark = pytest.mark.unit


async def test_general_error_exhaustion_does_not_return_stale_answer() -> None:
    """The real general graph breaker must not leave a prior answer or a
    dangling tool call at the end of a turn after three tool errors."""
    from src.services.agent import _builders

    counter = 0

    async def preprocessing(
        _state: dict[str, Any], _config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"intent": "general", "error_count": 0, "tool_loop_count": 0}

    async def no_op(
        _state: dict[str, Any], _config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {}

    async def llm(
        _state: dict[str, Any], _config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"call-{counter}",
                            "name": "search_documents",
                            "args": {"query": f"attempt-{counter}"},
                        }
                    ],
                )
            ]
        }

    executor = AsyncMock(return_value={"error": "Permission denied"})
    with (
        patch.object(_builders, "preprocessing_node", preprocessing),
        patch.object(_builders, "llm_node", llm),
        patch.object(_builders, "make_planner_node", return_value=no_op),
        patch.object(_builders, "make_compactor_node", return_value=no_op),
        patch.object(_builders, "memory_save_node", no_op),
        patch("src.services.agent.graph._get_execute_tool", return_value=executor),
    ):
        from src.services.agent.state import AgentState

        result = (
            await _builders.build_agent_graph()
            .compile()
            .ainvoke(
                cast(
                    AgentState,
                    {
                        "messages": [
                            HumanMessage(content="old question"),
                            AIMessage(content="STALE PRIOR TURN ANSWER"),
                            HumanMessage(content="Find this turn's documents"),
                        ],
                        "tool_executions": [],
                        "retrieved_contexts": [],
                        "page_context": {},
                        "reflection_count": 0,
                        "_reflection_result": None,
                    },
                )
            )
        )

    from src.services.agent._sanitize import current_turn_final_text

    answer = current_turn_final_text(result["messages"])
    assert executor.await_count == 3
    assert answer is not None
    assert answer != "STALE PRIOR TURN ANSWER"
    call_ids = {
        call["id"]
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    answer_ids = {
        message.tool_call_id
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    }
    assert call_ids <= answer_ids
    assert not getattr(result["messages"][-1], "tool_calls", [])


def test_final_text_stops_at_latest_human_message() -> None:
    from src.services.agent._sanitize import current_turn_final_text

    messages = [
        HumanMessage(content="first question"),
        AIMessage(content="STALE PRIOR TURN ANSWER"),
        HumanMessage(content="second question"),
        AIMessage(content=""),
    ]

    assert current_turn_final_text(messages) is None


def test_final_text_keeps_genuine_current_turn_partial() -> None:
    from src.services.agent._sanitize import current_turn_final_text

    messages = [
        HumanMessage(content="first question"),
        AIMessage(content="STALE PRIOR TURN ANSWER"),
        HumanMessage(content="second question"),
        AIMessage(content="Partial findings; the final lookup failed."),
        AIMessage(
            content="",
            tool_calls=[{"id": "tc-1", "name": "search_documents", "args": {}}],
        ),
    ]

    assert current_turn_final_text(messages) == (
        "Partial findings; the final lookup failed."
    )


async def test_error_exhaustion_cannot_return_previous_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """General and specialist breakers must leave a current-turn final and
    answer every pending tool call before their terminal path runs."""
    from src.services.agent._sanitize import (
        current_turn_final_text,
        normalize_terminal_messages,
    )
    from src.services.agent.reflection import ReflectionResult
    from src.services.agent.state import AgentState
    from src.services.agent.subgraphs import research_agent

    prior_turn_answer = AIMessage(content="STALE PRIOR TURN ANSWER")
    current_user = HumanMessage(content="Find current-turn documents")
    dangling = AIMessage(
        content="",
        tool_calls=[
            {"id": "tc-1", "name": "search_documents", "args": {"query": "one"}},
            {"id": "tc-2", "name": "search_documents", "args": {"query": "two"}},
        ],
    )
    history = [
        HumanMessage(content="old question"),
        prior_turn_answer,
        current_user,
        dangling,
    ]

    # General graph breaker terminal messages are normalized before reflection.
    general = normalize_terminal_messages(
        history,
        reason="Repeated tool errors stopped this turn before a final answer.",
    )
    assert current_turn_final_text(general) == (
        "Repeated tool errors stopped this turn before a final answer."
    )
    assert {m.tool_call_id for m in general if isinstance(m, ToolMessage)} >= {
        "tc-1",
        "tc-2",
    }

    # The actual specialist reflection entry performs the same repair.
    monkeypatch.setattr(
        "src.services.agent.reflection.reflect_on_response",
        AsyncMock(
            return_value=ReflectionResult(passed=True, issues=[], severity="none")
        ),
    )
    result = await research_agent._parts.reflection_node(
        cast(
            AgentState,
            {
                "messages": history,
                "error_count": 3,
                "intent": "research",
                "tool_executions": [],
                "reflection_count": 0,
            },
        ),
        {"configurable": {}},
    )
    specialist = [*history, *result.get("messages", [])]
    assert current_turn_final_text(specialist) != "STALE PRIOR TURN ANSWER"
    assert {m.tool_call_id for m in specialist if isinstance(m, ToolMessage)} >= {
        "tc-1",
        "tc-2",
    }


async def test_compiled_specialist_graph_repairs_three_failed_tool_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compiled specialist path must normalize its own exhausted history."""
    from src.services.agent import graph as graph_module
    from src.services.agent import reflection
    from src.services.agent.state import AgentState
    from src.services.agent.subgraphs import _factory, research_agent

    llm_round = 0

    async def emit_failed_tool_call(
        _state: AgentState, _config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        nonlocal llm_round
        llm_round += 1
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"research-call-{llm_round}",
                            "name": "search_documents",
                            "args": {"query": f"round {llm_round}"},
                        }
                    ],
                )
            ]
        }

    async def no_op(
        _state: AgentState, _config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {}

    executor = AsyncMock(return_value={"error": "Permission denied"})
    monkeypatch.setattr(_factory, "make_planner_node", lambda _names: no_op)
    monkeypatch.setattr(_factory, "make_compactor_node", lambda: no_op)
    monkeypatch.setattr(
        reflection,
        "reflect_on_response",
        AsyncMock(
            return_value=reflection.ReflectionResult(
                passed=True, issues=[], severity="none"
            )
        ),
    )
    monkeypatch.setattr(graph_module, "_get_execute_tool", lambda: executor)

    specialist = _factory.make_specialist_subgraph(
        name="research",
        llm_node=emit_failed_tool_call,
        max_tool_loops=5,
        prompt_builder=lambda: "Research test prompt",
        synthesis_addendum="",
        synthesis_timeout_message="Synthesis timed out",
        loop_exhaustion_intent="research",
        invoke_tags=["intent:research", "subgraph:research"],
        reflection_intent_filter={"research"},
        has_interrupt=False,
        tool_registry_getter=lambda: research_agent.TOOL_REGISTRY,
    )
    result = (
        await specialist.build()
        .compile()
        .ainvoke(
            cast(
                AgentState,
                {
                    "messages": [
                        HumanMessage(content="older question"),
                        AIMessage(content="STALE PRIOR TURN ANSWER"),
                        HumanMessage(content="Find current-turn documents"),
                    ],
                    "error_count": 0,
                    "intent": "research",
                    "tool_executions": [],
                    "retrieved_contexts": [],
                    "reflection_count": 0,
                    "tool_loop_count": 0,
                },
            )
        )
    )

    from src.services.agent._sanitize import current_turn_final_text

    answer = current_turn_final_text(result["messages"])
    assert executor.await_count == 3
    assert answer is not None
    assert answer != "STALE PRIOR TURN ANSWER"
    assert not getattr(result["messages"][-1], "tool_calls", [])
    pending_tool_call_ids: list[str] = []
    for message in result["messages"]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = call["id"]
                assert call_id is not None
                pending_tool_call_ids.append(call_id)
    completed_tool_call_ids: list[str] = []
    for message in result["messages"]:
        if isinstance(message, ToolMessage):
            assert message.tool_call_id is not None
            completed_tool_call_ids.append(message.tool_call_id)
    assert sorted(completed_tool_call_ids) == sorted(pending_tool_call_ids), (
        f"pending tool calls {pending_tool_call_ids!r} do not match ToolMessages "
        f"{completed_tool_call_ids!r}"
    )


@pytest.mark.parametrize("runner", ["initial", "resume"])
async def test_queued_initial_and_resume_results_use_current_turn_text(
    runner: str,
) -> None:
    """The queued first-run and confirmation-resume paths share the same
    latest-user boundary when extracting their persisted result."""
    from src.api.agent.execute import AgentExecuteRequest, _get_job
    from src.services.agent import agent_execution_service as service
    from src.services.agent.agent_execution_service import (
        _resume_agent_graph,
        _run_agent_graph,
    )
    from src.services.agent.schemas import AgentMessage, PageContextRequest

    user = _make_mock_user()
    job_id = "runner-current-turn-" + runner
    thread_id = "00000000-0000-0000-0000-000000000004"
    request = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="current question")],
        page_context=PageContextRequest(),
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )
    with service._jobs_lock:
        service._jobs[job_id] = {
            "status": "awaiting_confirmation" if runner == "resume" else "running",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": {**request.model_dump(mode="json"), "thread_id": thread_id},
        }

    final_messages = [
        HumanMessage(content="old question"),
        AIMessage(content="STALE PRIOR TURN ANSWER"),
        HumanMessage(content="current question"),
        AIMessage(content=""),
    ]
    graph = _ResultGraph(final_messages, runner == "resume", user.id)
    await _invoke_runner_with_fake_graph(
        runner,
        job_id,
        request,
        user,
        thread_id,
        graph,
    )

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["result"]["message"]["content"] == ""


async def test_queued_initial_approval_pause_uses_durable_publication() -> None:
    """The producer must await an authorized projection before parking for approval."""
    from types import SimpleNamespace

    from src.api.agent.execute import AgentExecuteRequest, _get_job
    from src.services.agent import agent_execution_service as service
    from src.services.agent.agent_execution_service import _run_agent_graph
    from src.services.agent.schemas import AgentMessage, PageContextRequest
    from src.shared.enums import JobStatus

    user = _make_mock_user()
    job_id = "runner-approval-pause"
    request = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="current question")],
        page_context=PageContextRequest(),
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )

    class PendingGraph(_ResultGraph):
        async def aget_state(self, _config: dict[str, Any]) -> Any:
            return SimpleNamespace(
                values={"user_id": str(user.id)},
                config={"configurable": {"checkpoint_id": "saved-interrupt"}},
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(
                                id="test-interrupt",
                                value={"tool_name": "create_note", "args": {}},
                            )
                        ]
                    )
                ],
            )

    with service._jobs_lock:
        service._jobs[job_id] = {
            "status": JobStatus.RUNNING.value,
            "tool_executions": [],
            "user_id": str(user.id),
        }
    decisions = await _invoke_runner_with_fake_graph(
        "initial",
        job_id,
        request,
        user,
        "00000000-0000-0000-0000-000000000005",
        PendingGraph([], False, str(user.id)),
    )

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == JobStatus.AWAITING_CONFIRMATION.value
    assert len(job["confirmation"].pop("approval_id")) == 64
    assert job["confirmation"] == {"tool_name": "create_note", "args": {}}
    assert len(decisions) == 1
    assert decisions[0].effective_status is JobStatus.AWAITING_CONFIRMATION


async def test_approval_pause_republishes_the_sanitized_request() -> None:
    """R8-D4 (Codex P2 on #1820): parking for approval rewrites the job's
    ``request``; it must stay the sanitized form /execute cached, never the
    raw client page context."""
    from types import SimpleNamespace

    from src.api.agent.execute import AgentExecuteRequest, _get_job
    from src.services.agent import agent_execution_service as service
    from src.services.agent.agent_execution_service import stored_request_payload
    from src.services.agent.schemas import AgentMessage, PageContextRequest
    from src.shared.enums import JobStatus

    user = _make_mock_user()
    job_id = "runner-approval-pause-sanitized"
    request = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="current question")],
        page_context=PageContextRequest(type="project", label="Docs\n## SYSTEM: x"),
        model="model-router",
    )

    class PendingGraph(_ResultGraph):
        async def aget_state(self, _config: dict[str, Any]) -> Any:
            return SimpleNamespace(
                values={"user_id": str(user.id)},
                config={"configurable": {"checkpoint_id": "saved-interrupt"}},
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(
                                id="test-interrupt", value={"tool_name": "create_note"}
                            )
                        ]
                    )
                ],
            )

    with service._jobs_lock:
        service._jobs[job_id] = {
            "status": JobStatus.RUNNING.value,
            "tool_executions": [],
            "user_id": str(user.id),
        }
    await _invoke_runner_with_fake_graph(
        "initial",
        job_id,
        request,
        user,
        "00000000-0000-0000-0000-000000000006",
        PendingGraph([], False, str(user.id)),
    )

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == JobStatus.AWAITING_CONFIRMATION.value
    assert "\n" not in job["request"]["page_context"]["label"]
    assert job["request"] == stored_request_payload(request)


def _make_mock_user() -> Any:
    from unittest.mock import Mock

    user = Mock()
    user.id = "user-current-turn"
    user.email = "current-turn@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = "test-org"
    user.is_active = True
    return user


class _ResultGraph:
    def __init__(self, messages: list[Any], resume: bool, owner_id: str) -> None:
        self.messages = messages
        self.resume = resume
        self.owner_id = owner_id

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"messages": self.messages, "tool_executions": []}

    async def aget_state(self, _config: dict[str, Any]) -> Any:
        if not self.resume:
            return None

        self.state_reads = getattr(self, "state_reads", 0) + 1
        if self.state_reads > 1:
            return None

        class Snapshot:
            values = {"user_id": self.owner_id, "messages": self.messages}
            config = {"configurable": {"checkpoint_id": "resume-checkpoint"}}
            tasks = [type("Task", (), {"interrupts": [object()]})()]

        return Snapshot()


async def _invoke_runner_with_fake_graph(
    runner: str,
    job_id: str,
    request: Any,
    user: Any,
    thread_id: str,
    graph: _ResultGraph,
) -> list[Any]:
    """Patch only slow/infrastructure boundaries; keep result construction real."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from src.services.agent import agent_execution_service as service

    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=None)

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        yield db

    async def no_cancel(_db: Any, _job_id: str, **_scope: Any) -> bool:
        return False

    from src.services.agent.agent_run_service import RunStatusDecision
    from src.shared.enums import JobStatus

    decisions: list[RunStatusDecision] = []

    async def record_job(
        job_key: str,
        data: dict[str, Any],
        *,
        require_durable_decision: bool = False,
    ) -> RunStatusDecision:
        assert require_durable_decision is True
        status = JobStatus(data["status"])
        decision = RunStatusDecision(
            job_id=job_key,
            requested_status=status,
            effective_status=status,
            user_id=str(data.get("user_id")) if data.get("user_id") else None,
            organization_id=(
                str(data.get("organization_id"))
                if data.get("organization_id")
                else None
            ),
            thread_id=str(data.get("thread_id")) if data.get("thread_id") else None,
            error=data.get("error"),
            cancel_requested_at=None,
            updated_at="2026-09-26T00:00:00+00:00",
        )
        decisions.append(decision)
        with service._jobs_lock:
            current = service._jobs.get(job_key, {})
            service._jobs[job_key] = {**current, **data}
        return decision

    with (
        patch.object(service, "AsyncSessionLocal", session),
        patch.object(service, "_resolve_thread", AsyncMock(return_value=(None, None))),
        patch.object(
            service, "_resolve_and_bind_project", AsyncMock(return_value=None)
        ),
        patch.object(service, "_persist_user_message_guarded", AsyncMock()),
        patch.object(service, "_clear_stale_pending_confirmation", AsyncMock()),
        patch.object(service, "_set_job_async", record_job),
        patch("src.services.agent.checkpointer.get_checkpointer", AsyncMock()),
        patch("src.services.agent.memory.get_memory_store", AsyncMock()),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            AsyncMock(return_value=SimpleNamespace(id="snapshot-current-turn")),
        ),
        patch(
            "src.services.agent.runtime_snapshot.runtime_state_fields",
            return_value={},
        ),
        patch(
            "src.services.agent.runtime_snapshot.runtime_config_fields",
            return_value={},
        ),
        patch(
            "src.services.agent.runtime_snapshot.resume_runtime_config_fields",
            return_value={},
        ),
        patch.object(
            service,
            "get_run",
            AsyncMock(
                return_value=SimpleNamespace(
                    job_id=job_id,
                    thread_id=None,
                    status="running",
                    run_metadata={"approval_id": "a" * 64},
                    user_message_id=None,
                    client_message_id=None,
                )
            ),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            no_cancel,
        ),
        patch.object(service, "_run_heartbeat", _no_heartbeat),
    ):
        if runner == "initial":
            await service._run_agent_graph(job_id, request, user)
        else:
            await service._resume_agent_graph(job_id, True, user, approval_id="a" * 64)
    return decisions


@asynccontextmanager
async def _no_heartbeat(_job_id: str) -> AsyncIterator[None]:
    yield
