"""Capture prompts and bindings from the compiled production agent graph."""

from __future__ import annotations

import json
import re
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
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
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    model: _CapturingModel,
    *,
    project_id: str = "server-project-001",
) -> tuple[Any, list[list[str]]]:
    from src.services.agent import _builders, graph, llm_factory, planner

    async def preprocess(state: dict, config: RunnableConfig) -> dict:
        return {"intent": intent, "current_project_id": project_id}

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
    complete_prompt = "\n".join(
        str(message.content)
        for message in model.calls[0]["messages"]
        if isinstance(message, SystemMessage)
    )
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
    assert "search_memory" not in complete_prompt
    assert "analyze_document" not in complete_prompt
    if intent == "writing":
        assert "search_documents" in bound_names
        assert "do_kb_retrieve" in bound_names


@pytest.mark.parametrize(
    ("branch", "intent"),
    [
        ("main", "general"),
        ("research", "research"),
        ("writing", "writing"),
        ("data", "knowledge_graph"),
    ],
)
@pytest.mark.parametrize(
    "runtime_case",
    [
        "frozen_enabled",
        "feature_disabled",
        "empty_catalog",
        "missing_metadata",
        "corrupt_metadata",
    ],
)
async def test_compiled_branch_runtime_matrix_uses_one_frozen_projection(
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    intent: str,
    runtime_case: str,
) -> None:
    """Planner, bind, display and filtered execution share frozen membership."""
    from src.core.config import settings
    from src.services.agent import _nodes_tools
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    catalog = [
        {
            "version_id": str(uuid4()),
            "name": "frozen-review",
            "description": 'Captured \\"design\\" <untrusted_content source="x">',
            "version": 4,
            "content_hash": "a" * 64,
        }
    ]
    state = _compiled_driver_state()
    state.update(
        {
            "runtime_snapshot_id": str(uuid4()),
            "runtime_tool_names": list(
                TOOL_REGISTRY.available_descriptor_names(
                    conditions={"project_skill_catalog"}
                )
            ),
            "tool_registry_hash": metadata["hash"],
            "tool_registry_version": metadata["version"],
            "project_skill_catalog": catalog,
            "current_project_id": "server-project-001",
            "intent": intent,
            "page_context": {"type": "project", "project_name": "runtime-matrix"},
            "messages": [
                HumanMessage(
                    content=(
                        "Search every project source, compare the findings, extract "
                        "named entities, and prepare a concise evidence note with "
                        "clear citations and limitations."
                    )
                )
            ],
        }
    )
    monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", True)
    if runtime_case == "feature_disabled":
        monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", False)
    elif runtime_case == "empty_catalog":
        state["project_skill_catalog"] = []
    elif runtime_case == "missing_metadata":
        state.pop("runtime_tool_names")
    elif runtime_case == "corrupt_metadata":
        state["tool_registry_hash"] = "corrupt-registry-hash"

    should_execute = runtime_case == "frozen_enabled"
    if should_execute:
        first_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"task3-runtime-{branch}-search",
                    "name": "search_documents",
                    "args": {"query": "runtime matrix source"},
                }
            ],
        )
        responses = [first_response, AIMessage(content="Search finished.")]
    else:
        tool_name = (
            "search_documents"
            if runtime_case == "corrupt_metadata"
            else "load_project_skill"
        )
        args = (
            {"query": "runtime matrix source"}
            if tool_name == "search_documents"
            else {"skill_name": "frozen-review"}
        )
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"task3-runtime-{branch}-probe",
                        "name": tool_name,
                        "args": args,
                    }
                ],
            )
        ]
    model = _CapturingModel(responses)
    graph, planner_names = _wire_compiled_graph(monkeypatch, intent, model)
    execute = AsyncMock(
        return_value={
            "message": ToolMessage(
                content='{"documents": []}',
                tool_call_id=f"task3-runtime-{branch}-search",
            ),
            "execution": {
                "id": f"task3-runtime-{branch}-search",
                "tool_name": "search_documents",
                "status": "success",
                "result": {"documents": []},
            },
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }
    )
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)

    result = await graph.ainvoke(state, {"recursion_limit": 40})

    if runtime_case in {"missing_metadata", "corrupt_metadata"}:
        assert model.calls == []
        assert model.bound_tool_names == []
        assert planner_names == []
        assert result["capability_limitation"]["reason"].startswith(
            "The saved tool runtime"
        )
        execute.assert_not_awaited()
        assert result["tool_executions"] == []
        return

    first_bound = model.bound_tool_names[0]
    context = _dynamic_context(model.calls[0])
    capability_line = next(
        line
        for line in context.splitlines()
        if line.startswith("Available registered tools for this branch")
    )
    displayed = capability_line.split(": ", 1)[1].removesuffix(".")
    displayed_names = displayed.split(", ") if displayed else []
    assert planner_names == [first_bound]
    assert displayed_names == first_bound
    assert ("load_project_skill" in first_bound) is should_execute
    assert ("load_project_skill" in context) is should_execute
    if should_execute:
        assert "frozen-review" in context
    if runtime_case == "feature_disabled":
        # Emergency rollout disable is deny-only even for a snapshot that
        # originally included the conditional loader and its frozen catalog.
        assert "load_project_skill" not in first_bound
        assert "frozen-review" not in context

    if should_execute:
        execute.assert_awaited_once()
        assert execute.await_args is not None
        assert execute.await_args.args[0]["name"] == "search_documents"
        assert [item["tool_name"] for item in result["tool_executions"]] == [
            "search_documents"
        ]
        assert _final_ai_text(result) == "Search finished."
    else:
        execute.assert_not_awaited()
        paired = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        assert len(paired) == 1
        assert paired[0].tool_call_id == f"task3-runtime-{branch}-probe"
        assert paired[0].status == "error"
        assert result["tool_executions"] == []
        assert result["capability_limitation"]["branch"] == branch


async def test_compiled_resume_uses_original_snapshot_after_live_skill_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later live catalog activation cannot alter an old compiled run."""
    from hashlib import sha256

    from src.core.config import settings
    from src.models import (
        AgentRuntimeSnapshot,
        ProjectSkill,
        ProjectSkillVersion,
        ProjectSkillVersionScan,
    )
    from src.services.agent import runtime_snapshot as runtime_snapshot_module
    from src.services.agent import tool_session as tool_session_module

    user_id = UUID("00000000-0000-4000-8000-000000000701")
    organization_id = UUID("00000000-0000-4000-8000-000000000702")
    project_id = UUID("00000000-0000-4000-8000-000000000703")
    skill_id = UUID("00000000-0000-4000-8000-000000000704")
    snapshot_id = UUID("00000000-0000-4000-8000-000000000705")
    frozen_version_id = UUID("00000000-0000-4000-8000-000000000706")
    live_version_id = UUID("00000000-0000-4000-8000-000000000707")
    frozen_instructions = "Use only the saved version-one review procedure."
    live_instructions = "Live version two was activated after this run began."
    frozen_hash = sha256(frozen_instructions.encode("utf-8")).hexdigest()
    live_hash = sha256(live_instructions.encode("utf-8")).hexdigest()
    frozen_version = ProjectSkillVersion(
        id=frozen_version_id,
        skill_id=skill_id,
        version=1,
        instructions=frozen_instructions,
        parsed_name="frozen-review",
        description="Version captured by the run.",
        content_hash=frozen_hash,
        author_id=user_id,
    )
    live_version = ProjectSkillVersion(
        id=live_version_id,
        skill_id=skill_id,
        version=2,
        instructions=live_instructions,
        parsed_name="frozen-review",
        description="Later active version.",
        content_hash=live_hash,
        author_id=user_id,
    )
    live_skill = ProjectSkill(
        id=skill_id,
        project_id=project_id,
        normalized_name="frozen-review",
        active_version_id=frozen_version_id,
        created_by_id=user_id,
        active_version=frozen_version,
    )
    user = SimpleNamespace(id=user_id, organization_id=organization_id)

    class QueryAwareSkillSession:
        """Return the same live ORM row through the production catalog query."""

        def __init__(self) -> None:
            self.snapshot: AgentRuntimeSnapshot | None = None
            self.catalog_rows: list[Any] = []
            self.scanned_version_ids: list[UUID] = []
            self.scan_results: list[tuple[UUID, str]] = []
            self.project_version_lookups: list[UUID] = []

        async def scalars(self, statement: Any) -> Any:
            assert statement.column_descriptions[0]["entity"] is ProjectSkill
            assert statement.compile().params["project_id_1"] == project_id
            self.catalog_rows.append(live_skill)
            return SimpleNamespace(all=lambda: [live_skill])

        async def scalar(self, statement: Any) -> Any:
            description = statement.column_descriptions[0]
            if description["entity"] is ProjectSkillVersionScan:
                scanned_version_id = UUID(
                    str(statement.compile().params["version_id_1"])
                )
                self.scanned_version_ids.append(scanned_version_id)
                if scanned_version_id in {frozen_version_id, live_version_id}:
                    self.scan_results.append((scanned_version_id, "passed"))
                    return SimpleNamespace(scan_state="passed")
                return None
            if description["expr"] is ProjectSkill.project_id:
                version_id = UUID(str(statement.compile().params["id_1"]))
                self.project_version_lookups.append(version_id)
                return project_id
            raise AssertionError(f"unexpected production scalar query: {statement}")

        async def get(self, model: Any, identity: Any, **_kwargs: Any) -> Any:
            requested_id = UUID(str(identity))
            if (
                model is AgentRuntimeSnapshot
                and self.snapshot is not None
                and requested_id == snapshot_id
            ):
                return self.snapshot
            if model is ProjectSkillVersion:
                return {
                    frozen_version_id: frozen_version,
                    live_version_id: live_version,
                }.get(requested_id)
            return None

        def add(self, row: Any) -> None:
            assert isinstance(row, AgentRuntimeSnapshot)
            setattr(row, "id", snapshot_id)
            self.snapshot = row

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    db: Any = QueryAwareSkillSession()
    snapshot_settings = SimpleNamespace(
        PROJECT_SKILL_CATALOG_ENABLED=True,
        PROJECT_SKILL_RUNTIME_ENABLED=True,
        PROJECT_SKILL_SNAPSHOT_RETENTION_DAYS=1,
    )
    monkeypatch.setattr(
        runtime_snapshot_module, "get_settings", lambda: snapshot_settings
    )
    monkeypatch.setattr(
        runtime_snapshot_module,
        "get_authorized_project",
        AsyncMock(return_value=SimpleNamespace(id=project_id)),
    )
    frozen_runtime = await runtime_snapshot_module.create_runtime_snapshot(
        db,
        user_id=user_id,
        project_id=project_id,
    )
    frozen_runtime_id = frozen_runtime.id
    assert frozen_runtime_id is not None
    snapshot = db.snapshot
    assert snapshot is not None
    assert frozen_runtime_id == str(snapshot_id)
    assert db.catalog_rows == [live_skill]
    assert db.catalog_rows[0] is live_skill
    assert db.scanned_version_ids == [frozen_version_id]
    assert db.scan_results == [(frozen_version_id, "passed")]
    assert snapshot.skill_catalog[0]["version_id"] == str(frozen_version_id)
    assert frozen_runtime.project_skill_catalog == (
        {
            "name": "frozen-review",
            "version": 1,
            "description": "Version captured by the run.",
            "content_hash": frozen_hash,
        },
    )

    # The same ORM object returned by the real catalog query now points at v2.
    setattr(live_skill, "active_version_id", live_version_id)
    setattr(live_skill, "active_version", live_version)
    assert live_skill.active_version_id == live_version_id
    fresh_catalog = await runtime_snapshot_module._eligible_catalog(
        db, project_id=project_id
    )
    assert fresh_catalog == [
        {
            "version_id": str(live_version_id),
            "name": "frozen-review",
            "description": "Later active version.",
            "version": 2,
            "content_hash": live_hash,
        }
    ]
    assert db.catalog_rows == [live_skill, live_skill]
    assert db.catalog_rows[1] is db.catalog_rows[0]
    assert db.scanned_version_ids == [frozen_version_id, live_version_id]
    assert db.scan_results == [
        (frozen_version_id, "passed"),
        (live_version_id, "passed"),
    ]
    assert snapshot.skill_catalog[0]["version_id"] == str(frozen_version_id)

    legacy_state = _compiled_driver_state()
    legacy_state.update(
        {
            "runtime_snapshot_id": frozen_runtime_id,
            "current_project_id": str(project_id),
            "thread_persistence": "ephemeral",
            "thread_id": "",
            "project_skill_catalog": [],
            "messages": [
                HumanMessage(
                    content=(
                        "Use my frozen-review skill, inspect the local project "
                        "sources, and explain the evidence clearly."
                    )
                )
            ],
        }
    )
    legacy_state.pop("runtime_tool_names")
    legacy_state.pop("tool_registry_hash")
    legacy_state.pop("tool_registry_version")
    hydrated = await runtime_snapshot_module.hydrate_runtime_state_from_snapshot(
        db,
        legacy_state,
        user_id=user_id,
    )
    legacy_state.update(hydrated)

    assert list(legacy_state["project_skill_catalog"]) == [
        {
            "name": "frozen-review",
            "version": 1,
            "description": "Version captured by the run.",
            "content_hash": frozen_hash,
        }
    ]

    @asynccontextmanager
    async def fake_tool_session() -> AsyncIterator[Any]:
        yield db

    monkeypatch.setattr(tool_session_module, "tool_session", fake_tool_session)
    monkeypatch.setattr(
        tool_session_module,
        "resolve_tool_user",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", True)
    model = _CapturingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "frozen-skill-load",
                        "name": "load_project_skill",
                        "args": {"skill_name": "frozen-review"},
                    }
                ],
            ),
            AIMessage(content="I used the frozen version-one procedure."),
        ]
    )
    compiled, planner_names = _wire_compiled_graph(
        monkeypatch,
        "writing",
        model,
        project_id=str(project_id),
    )
    result = await compiled.ainvoke(
        legacy_state,
        {
            "configurable": {
                "user_id": str(user_id),
                "organization_id": str(organization_id),
                "project_id": str(project_id),
                "runtime_snapshot_id": str(snapshot_id),
            },
            "recursion_limit": 40,
        },
    )

    context = _dynamic_context(model.calls[0])
    assert "frozen-review" in context
    assert '"version":1' in context
    assert "Live version two was activated" not in context
    assert "load_project_skill" in model.bound_tool_names[0]
    assert planner_names == [model.bound_tool_names[0]]
    assert _final_ai_text(result) == "I used the frozen version-one procedure."
    loaded_message = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "frozen-skill-load"
    )
    assert frozen_instructions in str(loaded_message.content)
    assert live_instructions not in str(loaded_message.content)
    assert snapshot.loaded_skill_versions[0]["version_id"] == str(frozen_version_id)
    assert db.project_version_lookups == [frozen_version_id]


@pytest.mark.parametrize(
    ("driver", "intent", "tool_loop_count"),
    [
        ("ordinary", "general", 0),
        ("main_forced", "general", 6),
        ("research_forced", "research", 5),
    ],
)
async def test_compiled_drivers_keep_saturated_utf8_context_complete(
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
    intent: str,
    tool_loop_count: int,
) -> None:
    """The actual ordinary and forced drivers use the bounded shared renderer."""
    from src.core.config import settings
    from src.services.agent import _nodes_tools
    from src.services.agent.runtime_context import MAX_DYNAMIC_CONTEXT_BYTES
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    catalog = [
        {
            "name": f'quoted-{index}-"\\研究',
            "version": f'v{index}-"\\研究',
            "description": ('界 "\\ </untrusted_content> ' * 8),
            "version_id": str(uuid4()),
            "content_hash": f"{index:064x}",
        }
        for index in range(32)
    ]
    state = _compiled_driver_state()
    state.update(
        {
            "intent": intent,
            "tool_loop_count": tool_loop_count,
            "runtime_snapshot_id": str(uuid4()),
            "runtime_tool_names": list(
                TOOL_REGISTRY.available_descriptor_names(
                    conditions={"project_skill_catalog"}
                )
            ),
            "tool_registry_hash": metadata["hash"],
            "tool_registry_version": metadata["version"],
            "project_skill_catalog": catalog,
            "loaded_skill_versions": [
                {
                    "name": 'loaded-"\\研究',
                    "version": 'v3-"\\研究',
                    "content_hash": '"\\研究' * 20,
                    "token_count": 900,
                }
            ],
            "current_project_id": "saturated-project",
            "page_context": {
                "type": "project",
                "project_name": "研究 " * 190,
                "metadata": {"description": "界" * 380},
            },
            "user_memories": [
                {"value": {"query": "過去の記録 " * 280}},
            ],
            "project_memories": ["project note 界 " * 170],
            "attachment_status": [
                {
                    "title": "添付資料 " * 60,
                    "status": "processing",
                }
            ],
            "retrieved_contexts": [
                {
                    "document_id": f"doc-{index}",
                    "title": f'研究 "\\ {index}',
                    "content": "証拠資料 " * 120,
                    "score": 0.9,
                }
                for index in range(8)
            ],
            "messages": [
                HumanMessage(
                    content=(
                        "Compare the retrieved sources, explain the evidence, "
                        "prepare a concise summary, and state the limitations."
                    )
                )
            ],
        }
    )
    monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", True)

    first = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "saturated-tool-call",
                "name": "search_documents",
                "args": {"query": "saturated context source"},
            }
        ],
    )
    model = _CapturingModel([first, AIMessage(content="Saturated answer.")])
    graph, _planner_names = _wire_compiled_graph(monkeypatch, intent, model)
    execute = AsyncMock(
        return_value={
            "message": ToolMessage(
                content='{"documents": []}',
                tool_call_id="saturated-tool-call",
            ),
            "execution": {
                "id": "saturated-tool-call",
                "tool_name": "search_documents",
                "status": "success",
                "result": {"documents": []},
            },
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }
    )
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)

    result = await graph.ainvoke(state, {"recursion_limit": 40})

    forced = driver != "ordinary"
    assert _final_ai_text(result) == "Saturated answer."
    assert len(model.calls) == (2 if forced or driver == "ordinary" else 1)
    context_call = model.calls[1] if forced else model.calls[0]
    system_messages = [
        message
        for message in context_call["messages"]
        if isinstance(message, SystemMessage)
    ]
    context_message = next(
        str(message.content)
        for message in reversed(system_messages)
        if "Execution branch:" in str(message.content)
    )
    dynamic_context = context_message[context_message.index("Execution branch:") :]
    assert len(dynamic_context.encode("utf-8")) <= MAX_DYNAMIC_CONTEXT_BYTES
    assert "Context omitted:" in dynamic_context
    identity_json = dynamic_context.split("IDENTITY EVIDENCE (JSON):\n", 1)[1].split(
        "\n\n", 1
    )[0]
    assert json.loads(identity_json) == {
        "version": 1,
        "records": [],
        "persisted_record_count": 0,
        "projected_record_count": 0,
        "omitted_record_count": 0,
        "ledger_loss_count": 0,
        "incomplete": False,
    }
    if forced:
        assert "Execution is closed for this pass." in dynamic_context
        assert "Available registered tools for this branch" not in dynamic_context
        assert "load_project_skill" not in dynamic_context
    else:
        catalog_json = re.findall(
            r'<untrusted_content source="skill_catalog">\n(.*?)\n</untrusted_content>',
            dynamic_context,
            flags=re.DOTALL,
        )
        assert catalog_json
        parsed_catalog = [json.loads(record) for record in catalog_json]
        assert all(
            set(record) == {"name", "version", "description"}
            and record["name"] in {item["name"] for item in catalog}
            for record in parsed_catalog
        )
        execute.assert_awaited_once()


def test_missing_role_asset_fallbacks_have_no_unavailable_tool_imperatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent.subgraphs import (
        agents_md_loader,
        data_agent,
        research_agent,
        writing_agent,
    )

    monkeypatch.setattr(agents_md_loader, "load_agents_md", lambda _branch: "")

    prompts = (
        research_agent._build_research_system_prompt(),
        writing_agent._build_writing_system_prompt(),
        data_agent._build_data_system_prompt(),
    )
    for prompt in prompts:
        assert "search_memory" not in prompt
        assert "analyze_document" not in prompt


@pytest.mark.parametrize(
    ("retrieval_backend", "provider_result"),
    [
        ("bedrock", "authorized"),
        ("bedrock", "invalid_scope"),
        ("bedrock", "foreign"),
        ("bedrock", "deleted"),
        ("bedrock", "unresolved"),
        ("do", "authorized"),
        ("do", "invalid_scope"),
        ("do", "foreign"),
        ("do", "deleted"),
        ("do", "unresolved"),
    ],
)
async def test_compiled_writing_resolves_named_local_source_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
    retrieval_backend: str,
    provider_result: str,
) -> None:
    from src.services.agent import tool_session
    from src.services.do_kb.models import Chunk, RetrieveResult

    document_id = UUID("00000000-0000-4000-8000-000000000123")
    organization_id = UUID("00000000-0000-4000-8000-000000000456")
    user = SimpleNamespace(
        id=UUID("00000000-0000-4000-8000-000000000789"), organization_id=organization_id
    )

    class Rows:
        def __init__(
            self,
            *,
            all_rows: list[Any] | None = None,
        ) -> None:
            self._all_rows = all_rows or []

        def all(self) -> list[Any]:
            return self._all_rows

        def scalars(self) -> Rows:
            return self

        def __iter__(self) -> Iterator[Any]:
            return iter(self._all_rows)

    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(do_kb_uuid="do-test-kb"))
    other_document_id = UUID("00000000-0000-4000-8000-000000000999")
    if provider_result == "invalid_scope":
        searched_documents: list[Any] = []
        db.execute = AsyncMock(return_value=Rows())
        requested_ids = ["not-a-canonical-document-id"]
    else:
        searched_documents = [
            SimpleNamespace(
                id=document_id,
                title="Exact Local Retrieval Study",
                document_type=None,
                processing_status=None,
                created_at=None,
            )
        ]
        resolver_rows_by_case: dict[str, list[Any]] = {
            "authorized": [(document_id, None, "Exact Local Retrieval Study")],
            "foreign": [(other_document_id, None, "Foreign Tenant Source")],
            # The provider can return a stale chunk after the document was
            # deleted between pre-dispatch authorization and resolution.
            "deleted": [(document_id, None, "Exact Local Retrieval Study")],
            "unresolved": [],
        }
        resolver_rows = resolver_rows_by_case[provider_result]

        query_number = 0

        async def execute_statement(statement: Any) -> Rows:
            nonlocal query_number
            current_number = query_number
            query_number += 1
            if current_number == 0:
                return Rows(all_rows=searched_documents)
            if current_number == 1:
                return Rows(
                    all_rows=[(document_id, None, "Exact Local Retrieval Study")]
                )
            if provider_result == "deleted":
                # Model the same row becoming soft-deleted after dispatch. The
                # SQL predicate is what keeps it out of post-provider evidence;
                # dropping that predicate makes this stale chunk usable.
                if "documents.is_deleted = false" in str(statement).lower():
                    return Rows()
            return Rows(all_rows=resolver_rows)

        db.execute = AsyncMock(side_effect=execute_statement)
        requested_ids = [str(document_id)]

    if provider_result in {"authorized", "invalid_scope", "deleted"}:
        provider_document_id = (
            str(document_id) if retrieval_backend == "bedrock" else f"{document_id}.txt"
        )
    elif provider_result == "foreign":
        provider_document_id = (
            str(other_document_id)
            if retrieval_backend == "bedrock"
            else f"{other_document_id}.txt"
        )
    else:
        provider_document_id = "unresolved-provider-file.txt"

    @asynccontextmanager
    async def fake_tool_session() -> AsyncIterator[Any]:
        yield db

    async def fake_resolve_user(_session: Any, _user_id: str, _org_id: str) -> Any:
        return user

    monkeypatch.setattr(tool_session, "tool_session", fake_tool_session)
    monkeypatch.setattr(tool_session, "resolve_tool_user", fake_resolve_user)
    claim_spy = AsyncMock(
        side_effect=AssertionError("read-only retrieval claimed an operation")
    )
    monkeypatch.setattr("src.services.agent.tool_operations.claim_operation", claim_spy)

    do_calls: list[dict[str, Any]] = []

    class FakeDoClient:
        async def retrieve(self, **kwargs: Any) -> RetrieveResult:
            do_calls.append(kwargs)
            return RetrieveResult(
                chunks=[
                    Chunk(
                        text="Evidence from the named local source.",
                        score=0.94,
                        document_id=provider_document_id,
                        metadata={"score_source": "upstream"},
                    )
                ],
                total=1,
            )

    bedrock_calls: list[tuple[Any, ...]] = []

    def fake_bedrock_retrieve(*args: Any) -> list[dict[str, Any]]:
        bedrock_calls.append(args)
        return [
            {
                "text": "Evidence from the named local source.",
                "score": 0.94,
                "document_id": provider_document_id,
                "metadata": {"score_source": "upstream"},
            }
        ]

    bedrock = retrieval_backend == "bedrock"
    from src.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "DO_KB_ENABLED", not bedrock)
    monkeypatch.setattr(
        app_settings,
        "BEDROCK_KB_ID",
        "bedrock-test-kb" if bedrock else "",
    )
    monkeypatch.setattr(app_settings, "AGENT_DOKB_COHERE_RERANK", False)
    monkeypatch.setattr(app_settings, "AGENT_ITERATIVE_RETRIEVAL", False)
    monkeypatch.setattr("src.services.do_kb.get_do_kb_client", lambda: FakeDoClient())
    monkeypatch.setattr(
        "src.services.bedrock_retrieval.retrieve_chunks", fake_bedrock_retrieve
    )

    search_call = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "task3-local-search",
                "name": "search_documents",
                "args": {"query": "Exact Local Retrieval Study"},
            }
        ],
    )
    retrieval_call = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "task3-local-retrieve",
                "name": "do_kb_retrieve",
                "args": {
                    "query": "main findings",
                    "document_ids": requested_ids,
                },
            }
        ],
    )
    model = _CapturingModel(
        [
            search_call,
            retrieval_call,
            AIMessage(content="The local source supports this summary."),
        ]
    )
    compiled, _planner_names = _wire_compiled_graph(monkeypatch, "writing", model)
    state = _compiled_driver_state()
    state.update(
        {
            "intent": "writing",
            "user_id": str(user.id),
            "current_project_id": "",
            "page_context": {},
            "messages": [
                HumanMessage(
                    content=(
                        'Summarize "Exact Local Retrieval Study" from my accessible '
                        "library in three sentences."
                    )
                )
            ],
        }
    )
    config = {
        "configurable": {
            "user_id": str(user.id),
            "organization_id": str(organization_id),
            "thread_id": "task3-local-document-thread",
        },
        "recursion_limit": 40,
    }

    result = await compiled.ainvoke(state, config)

    assert _final_ai_text(result) == "The local source supports this summary."
    assert [execution["tool_name"] for execution in result["tool_executions"]] == [
        "search_documents",
        "do_kb_retrieve",
    ]
    search_result = next(
        message.content
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "task3-local-search"
    )
    assert claim_spy.await_count == 0

    first_prompt = "\n".join(
        str(message.content)
        for message in model.calls[0]["messages"]
        if isinstance(message, SystemMessage)
    )
    assert (
        "First look for an exact source in the active project's documents"
        in first_prompt
    )
    assert (
        "Do not turn a local-source request into an external search or ingestion step."
        in first_prompt
    )

    retrieval_result = next(
        message.content
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id == "task3-local-retrieve"
    )

    if provider_result == "deleted":
        assert '"reason": "no_scoped_chunks"' in str(retrieval_result)
        assert "Evidence from the named local source." not in str(retrieval_result)

    if provider_result == "invalid_scope":
        assert '"documents": []' in str(search_result)
        assert '"reason": "invalid_document_scope"' in str(retrieval_result)
        assert not do_calls
        assert not bedrock_calls
        assert db.execute.await_count == 1
    else:
        assert str(document_id) in str(search_result)
        retrieval_prompt = model.calls[1]["messages"]
        assert any(
            isinstance(message, ToolMessage)
            and str(document_id) in str(message.content)
            for message in retrieval_prompt
        )
        sql = [str(call.args[0]).lower() for call in db.execute.await_args_list]
        assert "documents.organization_id" in sql[0]
        assert "documents.is_deleted = false" in sql[0]
        assert "documents.organization_id" in sql[1]
        assert "documents.is_deleted = false" in sql[1]
        assert "documents.id in" in sql[1]
        if provider_result != "authorized":
            assert "documents.organization_id" in sql[2]
            assert "documents.is_deleted = false" in sql[2]

    if provider_result == "authorized" and retrieval_backend == "bedrock":
        assert len(bedrock_calls) == 1
        assert bedrock_calls[0][0:4] == (
            "bedrock-test-kb",
            "main findings",
            organization_id,
            8,
        )
        assert bedrock_calls[0][4] == {
            "equals": {"key": "document_id", "value": str(document_id)}
        }
        assert not do_calls
    elif provider_result == "authorized" and retrieval_backend == "do":
        assert len(do_calls) == 1
        assert do_calls[0]["filters"] == {
            "equals": {"key": "item_name", "value": f"{document_id}.txt"}
        }
        assert not bedrock_calls
    elif provider_result == "invalid_scope":
        assert '"reason": "invalid_document_scope"' in str(retrieval_result)
        assert not do_calls
        assert not bedrock_calls
    else:
        assert '"reason": "no_scoped_chunks"' in str(retrieval_result)
        assert "Evidence from the named local source." not in str(retrieval_result)
        if retrieval_backend == "do":
            assert len(do_calls) == 1
            assert not bedrock_calls
        else:
            assert len(bedrock_calls) == 1
            assert not do_calls


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
