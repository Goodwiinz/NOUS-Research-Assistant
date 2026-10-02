"""Behavioral checks for one frozen registry projection and validated plans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn, cast
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


@pytest.mark.parametrize(
    ("tool_loop_count", "error_count"),
    [(0, 0), (8, 0), (0, 3)],
)
def test_specialist_router_sends_unavailable_batch_to_preflight_first(
    tool_loop_count: int, error_count: int
) -> None:
    """Unavailable batches must be paired before approval or terminal routing."""
    from src.services.agent.state import AgentState
    from src.services.agent.subgraphs.writing_agent import writing_should_continue

    state = _compiled_driver_state()
    state.update(
        {
            "intent": "writing",
            "tool_loop_count": tool_loop_count,
            "error_count": error_count,
            "messages": [
                HumanMessage(content="Create this project note"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "task3-router-supported-mutation",
                            "name": "create_project_note",
                            "args": {
                                "title": "Review notes",
                                "content": "A grounded note.",
                                "project_id": "00000000-0000-0000-0000-000000000001",
                            },
                        },
                        {
                            "id": "task3-router-unregistered",
                            "name": "not_a_registered_tool",
                            "args": {},
                        },
                    ],
                ),
            ],
        }
    )

    assert writing_should_continue(cast(AgentState, state)) == "writing_tool_node"


@pytest.mark.parametrize("branch", ["writing", "research", "data"])
@pytest.mark.parametrize(
    "terminal_pressure", ["under_budget", "at_ceiling", "at_error_limit"]
)
async def test_compiled_specialist_rejects_unavailable_batch_before_terminal_branches(
    monkeypatch: pytest.MonkeyPatch, branch: str, terminal_pressure: str
) -> None:
    """The real parent path rejects once and saves one final answer."""
    from langchain_core.messages import ToolMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import StateGraph

    from src.services.agent import _builders, _nodes_tools
    from src.services.agent import graph as graph_module
    from src.services.agent import llm_factory, planner
    from src.services.agent.subgraphs import _factory
    from src.services.agent.subgraphs.data_agent import MAX_DATA_TOOL_LOOPS
    from src.services.agent.subgraphs.research_agent import MAX_RESEARCH_TOOL_LOOPS
    from src.services.agent.subgraphs.writing_agent import MAX_WRITING_TOOL_LOOPS

    calls_by_branch = {
        "writing": {
            "id": "task3-compiled-supported-write",
            "name": "create_project_note",
            "args": {
                "title": "Review notes",
                "content": "A grounded note.",
                "project_id": "00000000-0000-0000-0000-000000000001",
            },
        },
        "research": {
            "id": "task3-compiled-supported-ingest",
            "name": "ingest_arxiv_papers",
            "args": {"paper_ids": ["2401.12345"]},
        },
        "data": {
            "id": "task3-compiled-supported-search",
            "name": "search_documents",
            "args": {"query": "attention mechanism", "max_results": 5},
        },
    }
    intent_by_branch = {
        "writing": "writing",
        "research": "research",
        "data": "knowledge_graph",
    }
    supported_call = calls_by_branch[branch]
    batch = [
        supported_call,
        {
            "id": "task3-compiled-unregistered",
            "name": "not_a_registered_tool",
            "args": {},
        },
    ]

    forbidden_nodes = {"interrupt": 0, "forced": 0, "reflection": 0}
    memory_saves: list[dict[str, Any]] = []

    class CapturedModel:
        def __init__(self) -> None:
            self.calls = 0
            self.binds: list[list[str]] = []

        def bind_tools(self, tools: list[Any], **_kwargs: Any) -> CapturedModel:
            self.binds.append([tool.name for tool in tools])
            return self

        async def ainvoke(self, _messages: list[Any], **_kwargs: Any) -> AIMessage:
            self.calls += 1
            return AIMessage(content="", tool_calls=batch)

    model = CapturedModel()
    execute = AsyncMock(side_effect=AssertionError("rejected batch executed"))
    claims = AsyncMock(
        side_effect=AssertionError("rejected batch claimed an operation")
    )
    original_add_node = StateGraph.add_node
    original_memory_save = _builders.memory_save_node

    def intercept_forbidden_nodes(
        graph_instance: Any,
        node: Any,
        action: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(node, str) and node.endswith("_interrupt_node"):

            async def forbidden_interrupt(
                _state: Any, config: RunnableConfig
            ) -> dict[str, Any]:
                _ = config
                forbidden_nodes["interrupt"] += 1
                return {"user_confirmed": True}

            action = forbidden_interrupt
        elif isinstance(node, str) and node.endswith("_force_synthesis_node"):

            async def forbidden_forced(
                _state: Any, config: RunnableConfig
            ) -> dict[str, Any]:
                _ = config
                forbidden_nodes["forced"] += 1
                return {"messages": [AIMessage(content="Forced branch ran.")]}

            action = forbidden_forced
        elif isinstance(node, str) and node.endswith("_reflection_gate"):

            async def forbidden_reflection(
                _state: Any, _config: Any = None
            ) -> dict[str, Any]:
                forbidden_nodes["reflection"] += 1
                return {"messages": []}

            action = forbidden_reflection
        return original_add_node(graph_instance, node, action, *args, **kwargs)

    async def counted_memory_save(state: Any, config: RunnableConfig) -> dict[str, Any]:
        memory_saves.append(state)
        return cast(dict[str, Any], await original_memory_save(state, config))

    async def preprocess(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent": intent_by_branch[branch],
            "current_project_id": state["current_project_id"],
        }

    async def empty_plan(
        _query: str, _tool_names: list[str], _page_context: dict[str, Any]
    ) -> Any:
        return planner.AgentPlan(steps=[])

    monkeypatch.setattr(StateGraph, "add_node", intercept_forbidden_nodes)
    monkeypatch.setattr(_builders, "memory_save_node", counted_memory_save)
    monkeypatch.setattr(_builders, "preprocessing_node", preprocess)
    monkeypatch.setattr(planner, "generate_plan", empty_plan)
    monkeypatch.setattr(graph_module, "_build_llm", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(llm_factory, "build_synthesis_llm", lambda **_kwargs: model)
    monkeypatch.setattr(
        llm_factory,
        "resolve_chat_deployment",
        lambda override=None: override or "task3-round2-model",
    )
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)
    monkeypatch.setattr(_factory, "interrupt", lambda _details: {"confirmed": True})
    monkeypatch.setattr("src.services.agent.tool_operations.claim_operation", claims)
    monkeypatch.setattr(
        "src.services.agent.iteration_ledger.write_iteration", lambda *_args: None
    )

    state = _compiled_driver_state()
    loop_ceiling = {
        "writing": MAX_WRITING_TOOL_LOOPS,
        "research": MAX_RESEARCH_TOOL_LOOPS,
        "data": MAX_DATA_TOOL_LOOPS,
    }[branch]
    loop_count = loop_ceiling if terminal_pressure == "at_ceiling" else 0
    error_count = 3 if terminal_pressure == "at_error_limit" else 0
    state.update(
        {
            "intent": intent_by_branch[branch],
            "tool_loop_count": loop_count,
            "error_count": error_count,
            "thread_id": "task3-valid-operation-thread",
            "user_id": "00000000-0000-0000-0000-000000000010",
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "task3-valid-operation-turn",
            "current_project_id": "00000000-0000-0000-0000-000000000001",
            "messages": [HumanMessage(content="Create this project note")],
        }
    )
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "user_id": "00000000-0000-0000-0000-000000000010",
                "organization_id": "00000000-0000-0000-0000-000000000020",
                "thread_id": "task3-valid-operation-thread",
            }
        },
    )

    compiled = _builders.build_agent_graph().compile()
    result = await compiled.ainvoke(cast(Any, state), config | {"recursion_limit": 40})

    assert forbidden_nodes == {"interrupt": 0, "forced": 0, "reflection": 0}
    assert model.calls == 1
    execute.assert_not_awaited()
    claims.assert_not_awaited()
    assert len(memory_saves) == 1
    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        supported_call["id"],
        "task3-compiled-unregistered",
    ]
    assert all(message.status == "error" for message in tool_messages)
    assert result["tool_executions"] == []
    assert result["capability_limitation"]["branch"] == branch
    final_answers = [
        message
        for message in result["messages"]
        if isinstance(message, AIMessage) and message.content
    ]
    assert len(final_answers) == 1
    assert "did not run" in str(final_answers[0].content).lower()
    assert supported_call["name"] in model.binds[0]


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


def test_plan_validation_treats_unregistered_tool_as_malformed() -> None:
    """R8-A5: a name unknown to the registry is planner noise, not a missing
    capability. The plan is dropped (continue without a plan) instead of
    ending the turn with a capability limitation, and the name never reaches
    the terminal renderer."""
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

    assert result.unsupported is False
    assert result.malformed is True
    assert result.unavailable_tools == ()
    assert result.plan is None


def test_plan_validation_accepts_na_tool_as_a_no_tool_step() -> None:
    """R8-A5: "N/A" is the model's no-tool marker (summarise / present step);
    the plan stays valid and the marker is normalised to an empty tool."""
    from src.services.agent.planner import validate_plan

    result = validate_plan(
        [
            {"step": 1, "description": "Search arXiv", "tool": "search_arxiv"},
            {
                "step": 2,
                "description": "Ingest the top papers",
                "tool": "ingest_arxiv_papers",
                "depends_on": [1],
            },
            {
                "step": 3,
                "description": "Summarise the findings",
                "tool": "N/A",
                "depends_on": [2],
            },
        ],
        {"search_arxiv", "ingest_arxiv_papers"},
    )

    assert result.unsupported is False
    assert result.malformed is False
    assert result.plan is not None
    assert [step["tool"] for step in result.plan] == [
        "search_arxiv",
        "ingest_arxiv_papers",
        "",
    ]


def test_plan_validation_registered_unavailable_tool_wins_over_noise() -> None:
    """A registered tool outside this branch is still a real capability gap,
    even when another step carries planner noise."""
    from src.services.agent.planner import validate_plan

    result = validate_plan(
        [
            {"step": 1, "description": "Hallucinated", "tool": "private_tool_xyz"},
            {"step": 2, "description": "Save a draft", "tool": "create_draft"},
        ],
        {"search_documents"},
    )

    assert result.unsupported is True
    assert result.unavailable_tools == ("create_draft",)
    assert result.plan is None


async def test_planner_continues_without_plan_for_unregistered_tool(
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
                {"step": 1, "description": "Hallucinated", "tool": "private_tool_xyz"},
            ],
        },
        {},
    )

    generate.assert_not_awaited()
    assert result == {"plan": [], "plan_reasoning": ""}
    assert "capability_limitation" not in result


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


@pytest.mark.parametrize(
    "corruption",
    [
        "owner",
        "expiry",
        "project",
        "thread",
        "job",
        "durable_unanchored",
        "registry_hash",
        "registry_version",
        "descriptor",
        "catalog",
    ],
)
async def test_old_checkpoint_projection_fails_closed_on_scope_or_metadata_corruption(
    corruption: str,
) -> None:
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    user_id = uuid4()
    project_id = uuid4()
    thread_id = uuid4()
    metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        project_id=project_id,
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
    values: dict[str, Any] = {
        "runtime_snapshot_id": str(row.id),
        "current_project_id": str(project_id),
        "thread_id": str(thread_id),
        "thread_persistence": "durable",
        "page_context": {},
    }
    kwargs: dict[str, Any] = {"user_id": user_id, "thread_id": thread_id}
    if corruption == "owner":
        row.user_id = uuid4()
    elif corruption == "expiry":
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif corruption == "project":
        row.project_id = uuid4()
    elif corruption == "thread":
        row.thread_id = uuid4()
    elif corruption == "job":
        row.thread_id = None
        row.job_id = "snapshot-job-123"
        values.pop("thread_id")
        kwargs.update({"thread_id": None, "job_id": "caller-job-456"})
    elif corruption == "durable_unanchored":
        row.thread_id = None
        values.pop("thread_id")
        kwargs["thread_id"] = None
    elif corruption == "registry_hash":
        row.tool_registry_hash = "unrecognized-hash"
    elif corruption == "registry_version":
        row.tool_registry_version = "unrecognized-version"
    elif corruption == "descriptor":
        row.tool_metadata = {"descriptors": [{"name": "unregistered_tool"}]}
    elif corruption == "catalog":
        row.tool_metadata = {
            "descriptors": TOOL_REGISTRY.frozen_descriptor_metadata(
                conditions={"project_skill_catalog"}
            )
        }
        row.skill_catalog = [
            {"version_id": str(uuid4()), "name": 7, "content_hash": "hash"}
        ]

    hydrated = await hydrate_runtime_state_from_snapshot(
        db,
        values,
        **kwargs,
    )

    assert hydrated["runtime_projection_unavailable"] is True
    assert hydrated["runtime_tool_names"] == []
    assert hydrated["project_skill_catalog"] == []
    assert hydrated["capability_limitation"]["branch"] == "runtime"


async def test_threadless_durable_checkpoint_hydrates_from_exact_job_anchor() -> None:
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    user_id = uuid4()
    metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        project_id=None,
        thread_id=None,
        job_id="durable-job-123",
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
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
            "thread_persistence": "durable",
            "page_context": {},
        },
        user_id=user_id,
        job_id="durable-job-123",
    )

    assert hydrated["runtime_projection_unavailable"] is False
    assert hydrated["runtime_tool_names"] == [
        item["name"] for item in row.tool_metadata["descriptors"]
    ]


@pytest.mark.parametrize(
    "anchor_kind", ["thread_arg", "job_arg", "thread_state", "job_state"]
)
async def test_threadless_ephemeral_snapshot_rejects_explicit_caller_anchor(
    anchor_kind: str,
) -> None:
    """Ephemeral hydration is valid only when the caller has no durable anchor."""
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    user_id = uuid4()
    metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        project_id=None,
        thread_id=None,
        job_id=None,
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
        tool_metadata={"descriptors": TOOL_REGISTRY.frozen_descriptor_metadata()},
        skill_catalog=[],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db = AsyncMock()
    db.get.return_value = row
    values: dict[str, Any] = {
        "runtime_snapshot_id": str(row.id),
        "current_project_id": "",
        "thread_persistence": "ephemeral",
        "page_context": {},
    }
    kwargs: dict[str, Any] = {"user_id": user_id}
    if anchor_kind == "thread_arg":
        kwargs["thread_id"] = uuid4()
    elif anchor_kind == "job_arg":
        kwargs["job_id"] = "caller-job-123"
    elif anchor_kind == "thread_state":
        values["thread_id"] = str(uuid4())
    else:
        values["job_id"] = "caller-job-123"

    hydrated = await hydrate_runtime_state_from_snapshot(db, values, **kwargs)

    assert hydrated["runtime_projection_unavailable"] is True
    assert hydrated["runtime_tool_names"] == []
    assert hydrated["project_skill_catalog"] == []
    assert hydrated["capability_limitation"]["branch"] == "runtime"


async def test_threadless_ephemeral_snapshot_hydrates_without_caller_anchor() -> None:
    """An unanchored ephemeral checkpoint may still hydrate its own snapshot."""
    from types import SimpleNamespace

    from src.services.agent.runtime_snapshot import hydrate_runtime_state_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    user_id = uuid4()
    metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        project_id=None,
        thread_id=None,
        job_id=None,
        tool_registry_hash=metadata["hash"],
        tool_registry_version=metadata["version"],
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
            "thread_persistence": "ephemeral",
            "page_context": {},
        },
        user_id=user_id,
    )

    assert hydrated["runtime_projection_unavailable"] is False
    assert hydrated["runtime_tool_names"] == [
        item["name"] for item in row.tool_metadata["descriptors"]
    ]


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
async def test_compiled_driver_continues_without_plan_for_unregistered_tool(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    stored_plan: bool,
) -> None:
    """R8-A5: a plan naming a tool the registry has never heard of is planner
    noise. The turn continues without a plan and the executor answers; it no
    longer ends in a capability-limitation terminal (the previous contract of
    this test). The bogus name still never reaches the model or the user."""
    from src.services.agent import _builders, graph, llm_factory, planner

    planner_calls = 0
    model_calls: list[list[Any]] = []

    class _AnsweringModel:
        def bind_tools(self, tools: list[Any], **kwargs: Any) -> _AnsweringModel:
            return self

        async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
            model_calls.append(list(messages))
            return AIMessage(content="Here is a direct answer.")

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

    monkeypatch.setattr(_builders, "preprocessing_node", preprocess)
    monkeypatch.setattr(planner, "generate_plan", generated_plan)
    monkeypatch.setattr(graph, "_build_llm", lambda *args, **kwargs: _AnsweringModel())
    monkeypatch.setattr(
        llm_factory, "build_synthesis_llm", lambda **kwargs: _AnsweringModel()
    )
    monkeypatch.setattr(
        llm_factory, "resolve_chat_deployment", lambda model_override=None: "main"
    )
    monkeypatch.setattr(llm_factory, "get_synthesis_model_name", lambda: "synth")
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
    agent_graph = _builders.build_agent_graph().compile()

    result = await agent_graph.ainvoke(state, {"recursion_limit": 30})

    assert planner_calls == (0 if stored_plan else 1)
    assert len(model_calls) == 1
    assert result["plan"] == []
    assert not result.get("capability_limitation")
    assert result["messages"][-1].content == "Here is a direct answer."
    assert not any("private_tool_xyz" in str(m.content) for m in model_calls[0])
