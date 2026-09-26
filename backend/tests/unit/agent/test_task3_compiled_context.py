"""Capture prompts and bindings from the compiled production agent graph."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

pytestmark = pytest.mark.unit


class _CapturingModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []
        self.bound_tool_names: list[list[str]] = []

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _CapturingModel:
        self.bound_tool_names.append([str(getattr(tool, "name", "")) for tool in tools])
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.calls.append({"messages": list(messages), "config": kwargs.get("config")})
        if not self.responses:
            raise AssertionError("compiled driver made an unexpected model call")
        return self.responses.popleft()


def _dynamic_context(call: dict[str, Any]) -> str:
    system_messages = [
        message for message in call["messages"] if isinstance(message, SystemMessage)
    ]
    assert system_messages
    if len(system_messages) == 1:
        # Specialist forced synthesis combines its static and dynamic text in
        # one SystemMessage to preserve the existing no-tools driver behavior.
        return str(system_messages[0].content)
    return str(system_messages[-1].content)


def _final_ai_text(result: dict[str, Any]) -> str:
    return next(
        str(message.content)
        for message in reversed(result["messages"])
        if isinstance(message, AIMessage) and message.content
    )


def _compiled_driver_state() -> dict:
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    return {
        "messages": [HumanMessage(content="Please inspect my project sources.")],
        "page_context": {"type": "project"},
        "retrieved_contexts": [],
        "attachment_ids": [],
        "attachment_status": [],
        "tool_executions": [],
        "thread_id": "",
        "thread_persistence": "ephemeral",
        "turn_index": 0,
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "pending_confirmation": {},
        "user_confirmed": False,
        "intent": "",
        "user_memories": [],
        "project_memories": [],
        "plan": [],
        "plan_reasoning": "",
        "reflection_count": 0,
        "compaction_count": 0,
        "intent_confidence": 0.0,
        "last_error_info": {},
        "user_id": "",
        "current_project_id": "",
        "model": "",
        "use_rag": False,
        "runtime_snapshot_id": "",
        "runtime_tool_names": list(TOOL_REGISTRY.available_descriptor_names()),
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "runtime_projection_unavailable": False,
        "project_skill_catalog": [],
        "loaded_skill_versions": [],
        "capability_limitation": {},
    }


def _wire_compiled_graph(
    monkeypatch: pytest.MonkeyPatch, intent: str, model: _CapturingModel
) -> tuple[Any, list[list[str]]]:
    from src.services.agent import _builders, graph, llm_factory, planner

    async def preprocess(state: dict, config: RunnableConfig) -> dict:
        return {"intent": intent, "current_project_id": "server-project-001"}

    planner_names: list[list[str]] = []

    async def empty_plan(
        query: str, tool_names: list[str], page_context: dict
    ) -> planner.AgentPlan:
        planner_names.append(list(tool_names))
        return planner.AgentPlan(steps=[])

    monkeypatch.setattr(_builders, "preprocessing_node", preprocess)
    monkeypatch.setattr(planner, "generate_plan", empty_plan)
    monkeypatch.setattr(graph, "_build_llm", lambda model_override=None: model)
    monkeypatch.setattr(
        llm_factory,
        "resolve_chat_deployment",
        lambda model_override=None: model_override or "selected-main-deployment",
    )
    monkeypatch.setattr(
        llm_factory, "get_synthesis_model_name", lambda: "selected-synthesis"
    )
    monkeypatch.setattr(llm_factory, "build_synthesis_llm", lambda **kwargs: model)
    return _builders.build_agent_graph().compile(), planner_names


@pytest.mark.parametrize(
    "intent", ["general", "research", "writing", "knowledge_graph"]
)
async def test_compiled_normal_drivers_share_projection_and_resolved_model(
    monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    model = _CapturingModel([AIMessage(content="A bounded answer for this request.")])
    graph, planner_names = _wire_compiled_graph(monkeypatch, intent, model)
    state = _compiled_driver_state()
    state.update(
        {
            "intent": intent,
            "model": "request-model-override",
            "current_project_id": "server-project-001",
            "page_context": {
                "type": "project",
                "project_name": "page-project-marker",
                "project_id": "forged-page-id",
                "metadata": {"description": "page-context-marker"},
            },
            "user_memories": [{"value": {"query": "user-memory-marker"}}],
            "project_memories": ["project-memory-marker"],
            "retrieved_contexts": [
                {
                    "document_id": "doc-1",
                    "title": "retrieval-marker",
                    "content": "Retrieved evidence marker.",
                    "score": 0.9,
                }
            ],
        }
    )
    state["messages"] = [
        HumanMessage(
            content=(
                "Search these project sources, compare them, prepare a briefing, "
                "and describe the evidence limits clearly."
            )
        )
    ]

    result = await graph.ainvoke(state, {"recursion_limit": 40})

    assert _final_ai_text(result) == "A bounded answer for this request."
    assert len(model.calls) == 1
    assert len(model.bound_tool_names) == 1
    bound_names = model.bound_tool_names[0]
    context = _dynamic_context(model.calls[0])
    capability_line = next(
        line
        for line in context.splitlines()
        if line.startswith("Available registered tools for this branch")
    )
    displayed_names = capability_line.split(": ", 1)[1].removesuffix(".").split(", ")
    assert displayed_names == bound_names
    assert planner_names == [bound_names]
    assert "Runtime model: selected deployment `request-model-override`." in context
    assert "server-project-001" in context
    assert "page-project-marker" in context
    assert "forged-page-id" not in context
    assert "user-memory-marker" in context
    assert "project-memory-marker" in context
    assert "Retrieved evidence marker." in context
    if intent == "writing":
        assert "search_documents" in bound_names
        assert "do_kb_retrieve" in bound_names


async def test_compiled_main_forced_synthesis_has_closed_context_and_selected_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _CapturingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "main-forced-call",
                        "name": "search_documents",
                        "args": {"query": "source"},
                    }
                ],
            ),
            AIMessage(content="Main forced answer."),
        ]
    )
    graph, planner_names = _wire_compiled_graph(monkeypatch, "general", model)
    state = _compiled_driver_state()
    state.update(
        {
            "intent": "general",
            "tool_loop_count": 6,
            "model": "request-model-override",
            "current_project_id": "server-project-001",
            "page_context": {"type": "project", "project_name": "forced-page-marker"},
        }
    )
    state["messages"] = [
        HumanMessage(
            content=(
                "Find sources, compare their findings, prepare a briefing, and "
                "report the result with clear evidence limits."
            )
        )
    ]

    result = await graph.ainvoke(state, {"recursion_limit": 40})

    assert _final_ai_text(result) == "Main forced answer."
    assert len(model.calls) == 2
    # Only the normal tool decision is bound; forced synthesis binds none.
    assert len(model.bound_tool_names[0]) > 0
    context = _dynamic_context(model.calls[1])
    assert "Runtime model: selected deployment `selected-synthesis`." in context
    assert "Execution is closed for this pass." in context
    assert "forced-page-marker" in context
    assert "Execute the next incomplete step now" not in "\n".join(
        str(message.content) for message in model.calls[1]["messages"]
    )
    assert "ACTIVE PLAN" not in context
    assert planner_names == [model.bound_tool_names[0]]


async def test_compiled_specialist_forced_synthesis_uses_unbound_shared_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _CapturingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "research-forced-call",
                        "name": "search_documents",
                        "args": {"query": "source"},
                    }
                ],
            ),
            AIMessage(content="Research forced answer."),
        ]
    )
    graph, planner_names = _wire_compiled_graph(monkeypatch, "research", model)
    state = _compiled_driver_state()
    state.update(
        {
            "intent": "research",
            "tool_loop_count": 5,
            "model": "request-model-override",
            "current_project_id": "server-project-001",
            "page_context": {"type": "project", "project_name": "forced-page-marker"},
        }
    )
    state["messages"] = [
        HumanMessage(
            content=(
                "Search these sources, compare their findings, prepare a briefing, "
                "and report the result with clear evidence limits."
            )
        )
    ]

    result = await graph.ainvoke(state, {"recursion_limit": 40})

    assert _final_ai_text(result) == "Research forced answer."
    assert len(model.calls) == 2
    assert len(model.bound_tool_names) == 1
    assert len(model.bound_tool_names[0]) > 0
    forced_request = model.calls[1]["messages"]
    assert not any(
        isinstance(message, SystemMessage) and "ACTIVE PLAN" in str(message.content)
        for message in forced_request
    )
    context = _dynamic_context(model.calls[1])
    assert "Runtime model: selected deployment `request-model-override`." in context
    assert "Execution is closed for this pass." in context
    assert "forced-page-marker" in context
    assert "load_project_skill" not in context
    assert planner_names == [model.bound_tool_names[0]]
