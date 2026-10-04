"""R8-A3: cross-turn state survives the per-turn ``initial_state`` input.

The node-level tests (``test_memory_save_gating``, ``test_preprocessing_timing``)
call nodes directly and cannot see the input dict clobbering the checkpoint,
so this drives the compiled production graph across two turns on one
checkpointed thread with the same input shape both builders send.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

pytestmark = pytest.mark.unit

THREAD_ID = "00000000-0000-4000-8000-0000000000a3"


class _StaticModel:
    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _StaticModel:
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content="A bounded answer for this turn.")


def _turn_input(text: str) -> dict[str, Any]:
    """The request-independent shape of both production ``initial_state``s."""
    from src.services.agent.runtime_snapshot import (
        empty_runtime_snapshot,
        runtime_state_fields,
        turn_reset_fields,
    )

    return {
        "messages": [HumanMessage(content=text)],
        "page_context": {"type": "chat"},
        **turn_reset_fields(),
        "attachment_ids": [],
        "thread_id": THREAD_ID,
        "thread_persistence": "durable",
        "project_memories": [],
        "user_id": "",
        "model": "",
        "use_rag": False,
        **runtime_state_fields(empty_runtime_snapshot(), None),
    }


def _compile(monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.services.agent import (
        _builders,
        _nodes_classify,
        graph,
        llm_factory,
        planner,
    )

    async def no_retrieval(state: dict, config: RunnableConfig) -> dict:
        return {"retrieved_contexts": []}

    async def general(state: dict, config: RunnableConfig) -> dict:
        return {"intent": "general", "intent_confidence": 1.0}

    async def no_memories(state: dict, config: RunnableConfig) -> dict:
        return {"user_memories": []}

    async def empty_plan(
        query: str, tool_names: list[str], page_context: dict
    ) -> planner.AgentPlan:
        return planner.AgentPlan(steps=[])

    # Real preprocessing_node (owns the turn_index increment); only its three
    # network-bound subtasks are stubbed.
    monkeypatch.setattr(_nodes_classify, "rag_node", no_retrieval)
    monkeypatch.setattr(_nodes_classify, "_classify_core", general)
    monkeypatch.setattr(_nodes_classify, "memory_retrieval_node", no_memories)
    monkeypatch.setattr(planner, "generate_plan", empty_plan)
    model = _StaticModel()
    monkeypatch.setattr(graph, "_build_llm", lambda model_override=None: model)
    monkeypatch.setattr(
        llm_factory,
        "resolve_chat_deployment",
        lambda model_override=None: model_override or "main-deployment",
    )
    monkeypatch.setattr(llm_factory, "get_synthesis_model_name", lambda: "synth")
    monkeypatch.setattr(llm_factory, "build_synthesis_llm", lambda **kwargs: model)
    return _builders.build_agent_graph().compile(checkpointer=MemorySaver())


async def test_turn_index_advances_across_checkpointed_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _compile(monkeypatch)
    config = {"configurable": {"thread_id": THREAD_ID}, "recursion_limit": 40}

    first = await compiled.ainvoke(
        _turn_input("Summarise the main argument of my notes."), config
    )
    second = await compiled.ainvoke(
        _turn_input("Now list the open questions from those notes."), config
    )

    assert first["turn_index"] == 1
    # Seeding turn_index in the turn input pinned this to 1 forever, so
    # AGENT_INSIGHT_EVERY_N_TURNS never fired and memory keys collided.
    assert second["turn_index"] == 2


def test_turn_input_never_seeds_cross_turn_state() -> None:
    """Guard both builders, not just the shared helper, against re-seeding."""
    from src.api.agent import streaming
    from src.services.agent import agent_execution_service
    from src.services.agent.runtime_snapshot import turn_reset_fields

    assert "turn_index" not in turn_reset_fields()
    for module in (streaming, agent_execution_service):
        assert '"turn_index"' not in inspect.getsource(module), module.__name__
