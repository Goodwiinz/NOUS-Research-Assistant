"""Behavioral checks for one frozen registry projection and validated plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytestmark = pytest.mark.unit


def _compiled_driver_state() -> dict:
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    return {
        "messages": [
            HumanMessage(
                content="Search several papers, analyze them, and create a draft with notes."
            )
        ],
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


def _guard_model_and_reflection_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Keep compiled terminal-path tests offline and prove they stay terminal."""
    from langgraph.graph import StateGraph

    from src.services.agent import _builders, graph, llm_factory

    calls = {"model": 0, "reflection": 0}

    def unexpected_model(*args: Any, **kwargs: Any) -> NoReturn:
        calls["model"] += 1
        raise AssertionError("unsupported plan must not build a model client")

    original_add_node = StateGraph.add_node

    def guard_compiled_reflection_node(
        graph_instance: Any,
        node: Any,
        action: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(node, str) and node.endswith("reflection_gate"):

            async def unexpected_reflection(state: Any) -> NoReturn:
                calls["reflection"] += 1
                raise AssertionError("unsupported plan must not enter reflection")

            action = unexpected_reflection
        return original_add_node(graph_instance, node, action, *args, **kwargs)

    monkeypatch.setattr(graph, "_build_llm", unexpected_model)
    monkeypatch.setattr(llm_factory, "build_synthesis_llm", unexpected_model)
    # Intercept the actual node passed into each compiled graph. Patching the
    # specialist factory here would affect its import-time cached graph parts
    # and leak the sentinel into unrelated tests.
    monkeypatch.setattr(StateGraph, "add_node", guard_compiled_reflection_node)
    return calls


def test_runtime_projection_is_frozen_deny_only_and_branch_scoped() -> None:
    from src.services.agent.tool_registry import runtime_tool_descriptors
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    frozen = {
        "runtime_snapshot_id": "snapshot-17",
        "runtime_tool_names": ["search_documents", "load_project_skill"],
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "project_skill_catalog": [{"name": "frozen-skill"}],
    }
    writing = runtime_tool_descriptors(
        "writing", frozen, TOOL_REGISTRY, skill_runtime_enabled=True
    )
    assert {item.name for item in writing} == {
        "search_documents",
        "load_project_skill",
    }

    enabled_loader = runtime_tool_descriptors(
        "main",
        {**frozen, "intent": "general"},
        TOOL_REGISTRY,
        skill_runtime_enabled=True,
    )
    assert "load_project_skill" in {item.name for item in enabled_loader}

    denied_loader = runtime_tool_descriptors(
        "main",
        {**frozen, "intent": "general"},
        TOOL_REGISTRY,
        skill_runtime_enabled=False,
    )
    assert "load_project_skill" not in {item.name for item in denied_loader}

    # A live catalog or newly enabled descriptor cannot expand this turn.
    assert (
        runtime_tool_descriptors(
            "writing",
            {**frozen, "runtime_tool_names": ["execute_code"]},
            TOOL_REGISTRY,
            skill_runtime_enabled=True,
        )
        == ()
    )
    assert (
        runtime_tool_descriptors(
            "writing",
            {"runtime_snapshot_id": "snapshot-17", "project_skill_catalog": []},
            TOOL_REGISTRY,
            skill_runtime_enabled=True,
        )
        == ()
    )


def test_unknown_main_intent_uses_general_projection_not_all_tools() -> None:
    from src.services.agent.tool_registry import runtime_tool_descriptors
    from src.services.agent.tools import ALL_TOOLS, TOOL_REGISTRY

    projected = runtime_tool_descriptors(
        "main", {"intent": "untrusted-new-intent"}, TOOL_REGISTRY
    )
    projected_names = {item.name for item in projected}
    assert projected_names == {
        item.name for item in TOOL_REGISTRY.descriptors_for_intent("general")
    }
    assert projected_names != {item.name for item in ALL_TOOLS}


async def test_main_batch_cannot_forge_conditional_skill_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools

    execute = AsyncMock()
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)
    result = await _nodes_tools.tool_node(
        {
            "intent": "general",
            "runtime_snapshot_id": "",
            "runtime_tool_names": ["load_project_skill"],
            "project_skill_catalog": [{"name": "not-authorized"}],
            "messages": [
                HumanMessage(content="Load a project skill"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "forged-loader",
                            "name": "load_project_skill",
                            "args": {"skill_name": "not-authorized"},
                        }
                    ],
                ),
            ],
            "page_context": {},
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
        },
        {"configurable": {}},
    )

    execute.assert_not_awaited()
    assert result["capability_limitation"]["branch"] == "main"
    assert result["messages"][0].tool_call_id == "forged-loader"


def test_plan_validation_rejects_unavailable_short_plan_and_bad_edges() -> None:
    from src.services.agent.planner import AgentPlan, PlanStep, validate_plan

    unsupported = validate_plan(
        AgentPlan(
            steps=[
                PlanStep(step=1, description="Run code", tool="execute_code"),
                PlanStep(step=2, description="Save a draft", tool="create_draft"),
            ]
        ),
        {"execute_code"},
    )
    assert unsupported.unavailable_tools == ("create_draft",)
    assert unsupported.plan is None

    malformed = validate_plan(
        AgentPlan(
            steps=[
                PlanStep(step=1, description="A", tool="search_documents"),
                PlanStep(
                    step=3, description="B", tool="search_documents", depends_on=[2]
                ),
            ]
        ),
        {"search_documents"},
    )
    assert malformed.malformed
    assert malformed.plan is None


def test_plan_validation_signals_unregistered_tool_without_echoing_its_name() -> None:
    from src.services.agent.planner import validate_plan

    result = validate_plan(
        [
            {
                "step": 1,
                "description": "Use an unregistered tool",
                "tool": "private_tool_xyz",
            }
        ],
        {"search_documents"},
    )

    assert result.unsupported is True
    assert result.unavailable_tools == ()
    assert result.plan is None


def test_plan_validation_rejects_boolean_step_before_integer_coercion() -> None:
    from src.services.agent.planner import validate_plan

    result = validate_plan(
        [{"step": True, "description": "Search", "tool": "search_documents"}],
        {"search_documents"},
    )

    assert result.malformed is True
    assert result.plan is None


async def test_planner_validates_stored_plan_before_short_plan_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import planner

    generate = AsyncMock()
    monkeypatch.setattr(planner, "generate_plan", generate)
    node = planner.make_planner_node(["search_documents"])
    result = await node(
        {
            "messages": [HumanMessage(content="Find sources")],
            "page_context": {"type": "project"},
            "plan": [
                {"step": 1, "description": "Run code", "tool": "execute_code"},
            ],
        },
        {},
    )

    generate.assert_not_awaited()
    assert result["plan"] == []
    assert result["capability_limitation"]["unavailable_tools"] == ["execute_code"]


async def test_old_checkpoint_projection_hydrates_only_from_authorized_snapshot() -> (
    None
):
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    user_id = uuid4()
    thread_id = uuid4()
    metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        project_id=None,
        thread_id=thread_id,
        job_id=None,
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
        tool_metadata={"descriptors": TOOL_REGISTRY.frozen_descriptor_metadata()},
        skill_catalog=[],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db = AsyncMock()
    db.get.return_value = row
    values = {
        "runtime_snapshot_id": str(row.id),
        "current_project_id": "",
        "thread_id": str(thread_id),
        "page_context": {},
    }

    hydrated = await hydrate_runtime_state_from_snapshot(
        db, values, user_id=user_id, thread_id=thread_id
    )

    assert hydrated["runtime_tool_names"] == [
        item["name"] for item in row.tool_metadata["descriptors"]
    ]
    assert hydrated["tool_registry_hash"] == metadata["hash"]
    assert hydrated["runtime_projection_unavailable"] is False
    assert hydrated["project_skill_catalog"] == ()


async def test_old_checkpoint_projection_fails_closed_on_scope_mismatch() -> None:
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    row = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        project_id=None,
        thread_id=uuid4(),
        job_id=None,
        tool_registry_hash=TOOL_REGISTRY.metadata_snapshot()["hash"],
        tool_registry_version=TOOL_REGISTRY.metadata_snapshot()["version"],
        tool_metadata={"descriptors": TOOL_REGISTRY.frozen_descriptor_metadata()},
        skill_catalog=[],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db = AsyncMock()
    db.get.return_value = row

    hydrated = await hydrate_runtime_state_from_snapshot(
        db,
        {
            "runtime_snapshot_id": str(row.id),
            "current_project_id": "",
            "thread_id": str(uuid4()),
            "page_context": {},
        },
        user_id=row.user_id,
        thread_id=uuid4(),
    )

    assert hydrated["runtime_projection_unavailable"] is True
    assert hydrated["runtime_tool_names"] == []
    assert hydrated["capability_limitation"]["branch"] == "runtime"


@pytest.mark.parametrize(
    ("intent", "missing_capabilities", "expected_branch"),
    [
        ("general", ["execute_code", "create_draft"], "main"),
        ("writing", ["execute_code"], "writing"),
    ],
)
async def test_compiled_driver_terminates_unsupported_plan_without_llm_retry(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    missing_capabilities: list[str],
    expected_branch: str,
) -> None:
    from src.services.agent import _builders, planner

    planner_calls = 0
    llm_calls = 0
    guarded_calls = _guard_model_and_reflection_calls(monkeypatch)

    async def preprocess(state: dict[str, Any]) -> dict[str, Any]:
        return {"intent": intent}

    async def unsupported_plan(
        query: str, tool_names: list[str], page_context: dict[str, Any]
    ) -> planner.AgentPlan:
        nonlocal planner_calls
        planner_calls += 1
        return planner.AgentPlan(
            steps=[],
            outcome="unsupported",
            missing_capabilities=missing_capabilities,
        )

    async def unexpected_llm(state: Any) -> NoReturn:
        nonlocal llm_calls
        llm_calls += 1
        raise AssertionError("unsupported plan must not reach the executor model")

    monkeypatch.setattr(_builders, "preprocessing_node", preprocess)
    monkeypatch.setattr(_builders, "llm_node", unexpected_llm)
    monkeypatch.setattr(planner, "generate_plan", unsupported_plan)
    graph = _builders.build_agent_graph().compile()

    result = await graph.ainvoke(_compiled_driver_state(), {"recursion_limit": 30})

    assert planner_calls == 1
    assert llm_calls == 0
    assert guarded_calls == {"model": 0, "reflection": 0}
    final_message = result["messages"][-1]
    assert isinstance(final_message, AIMessage)
    assert "cannot represent" in str(final_message.content).lower()
    for name in missing_capabilities:
        assert name in final_message.content
    assert result["capability_limitation"]["branch"] == expected_branch


@pytest.mark.parametrize(
    ("intent", "stored_plan"),
    [("general", False), ("writing", True)],
)
async def test_compiled_driver_terminates_unregistered_tool_plan_without_echo_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    stored_plan: bool,
) -> None:
    from src.services.agent import _builders, planner

    planner_calls = 0
    llm_calls = 0
    guarded_calls = _guard_model_and_reflection_calls(monkeypatch)

    async def preprocess(state: dict[str, Any]) -> dict[str, Any]:
        return {"intent": intent}

    async def generated_plan(
        query: str, tool_names: list[str], page_context: dict[str, Any]
    ) -> planner.AgentPlan:
        nonlocal planner_calls
        planner_calls += 1
        return planner.AgentPlan(
            steps=[
                planner.PlanStep(
                    step=1,
                    description="Use a capability absent from the registry",
                    tool="private_tool_xyz",
                )
            ]
        )

    async def unexpected_llm(state: Any) -> NoReturn:
        nonlocal llm_calls
        llm_calls += 1
        raise AssertionError("unregistered plan must not reach the executor model")

    monkeypatch.setattr(_builders, "preprocessing_node", preprocess)
    monkeypatch.setattr(_builders, "llm_node", unexpected_llm)
    monkeypatch.setattr(planner, "generate_plan", generated_plan)
    state = _compiled_driver_state()
    state["intent"] = intent
    if stored_plan:
        state["plan"] = [
            {
                "step": 1,
                "description": "Use a capability absent from the registry",
                "tool": "private_tool_xyz",
            }
        ]
    graph = _builders.build_agent_graph().compile()

    result = await graph.ainvoke(state, {"recursion_limit": 30})

    assert planner_calls == (0 if stored_plan else 1)
    assert llm_calls == 0
    assert guarded_calls == {"model": 0, "reflection": 0}
    assert result["plan"] == []
    assert result["capability_limitation"]["branch"] == (
        "writing" if intent == "writing" else "main"
    )
    final_message = result["messages"][-1]
    assert isinstance(final_message, AIMessage)
    assert "cannot represent" in str(final_message.content).lower()
    assert "private_tool_xyz" not in final_message.content
    terminal_answers = [
        message
        for message in result["messages"]
        if isinstance(message, AIMessage)
        and "cannot represent" in str(message.content).lower()
    ]
    assert terminal_answers == [final_message]
    assert [type(message) for message in result["messages"]] == [
        HumanMessage,
        AIMessage,
    ]
