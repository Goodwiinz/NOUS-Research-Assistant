"""Runtime prompt/binding contracts for frozen project skills."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from src.services.agent.state import AgentState


def test_skill_catalog_prompt_is_compact_deterministic_and_instructs_loader_use() -> (
    None
):
    from src.services.agent.runtime_snapshot import render_project_skill_catalog

    prompt = render_project_skill_catalog(
        [
            {
                "name": "literature-review",
                "description": "Use the project review rubric.",
                "version": 2,
                "content_hash": "a" * 64,
            }
        ]
    )

    assert "literature-review (v2)" in prompt
    assert "load_project_skill(skill_name)" in prompt
    assert (
        "When the user explicitly names a listed skill, call "
        "load_project_skill(skill_name) before any other project tool." in prompt
    )
    assert "Use the project review rubric." in prompt
    assert "a" * 64 not in prompt


def test_main_loader_binding_requires_enabled_runtime_and_frozen_catalog() -> None:
    from src.services.agent._nodes_llm import tools_for_runtime_snapshot

    state = cast(
        AgentState,
        {
            "runtime_snapshot_id": "snapshot-1",
            "project_skill_catalog": [{"name": "literature-review"}],
        },
    )
    with patch(
        "src.services.agent._nodes_llm.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        tools = tools_for_runtime_snapshot([], state)
    assert [tool.name for tool in tools] == ["load_project_skill"]

    with patch(
        "src.services.agent._nodes_llm.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=False),
    ):
        assert tools_for_runtime_snapshot([], state) == []


def test_shared_initial_and_resume_runtime_fields_cover_both_transports() -> None:
    from src.services.agent.runtime_snapshot import (
        RuntimeSnapshot,
        resume_runtime_config_fields,
        runtime_config_fields,
        runtime_state_fields,
    )

    snapshot = RuntimeSnapshot(
        "snapshot-1", "hash", "1", ("search_documents",), ({"name": "skill"},), None
    )
    expected_state = {
        "current_project_id": "project-1",
        "runtime_snapshot_id": "snapshot-1",
        "runtime_tool_names": ["search_documents"],
        "tool_registry_hash": "hash",
        "tool_registry_version": "1",
        "runtime_projection_unavailable": False,
        "project_skill_catalog": [{"name": "skill"}],
        "loaded_skill_versions": [],
        "capability_limitation": {},
    }
    assert runtime_state_fields(snapshot, "project-1") == expected_state
    assert runtime_config_fields("snapshot-1", "project-1") == {
        "project_id": "project-1",
        "runtime_snapshot_id": "snapshot-1",
    }
    assert resume_runtime_config_fields(
        {**expected_state, "page_context": {"project_id": "other"}}
    ) == {"project_id": "project-1", "runtime_snapshot_id": "snapshot-1"}
    empty = RuntimeSnapshot(None, "hash", "1", ("search_documents",), (), None)
    assert runtime_state_fields(empty, None)["project_skill_catalog"] == []
    assert runtime_config_fields(empty.id, None) == {
        "project_id": "",
        "runtime_snapshot_id": "",
    }


def test_all_subgraph_tool_sets_use_the_same_runtime_loader_gate() -> None:
    from src.services.agent._nodes_llm import tools_for_runtime_snapshot
    from src.services.agent.subgraphs.data_agent import DATA_TOOLS
    from src.services.agent.subgraphs.research_agent import RESEARCH_TOOLS
    from src.services.agent.subgraphs.writing_agent import WRITING_TOOLS

    state = cast(
        AgentState,
        {
            "runtime_snapshot_id": "snapshot-1",
            "project_skill_catalog": [{"name": "skill"}],
        },
    )
    with patch(
        "src.services.agent._nodes_llm.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        for base in (RESEARCH_TOOLS, WRITING_TOOLS, DATA_TOOLS):
            assert (
                tools_for_runtime_snapshot(base, state)[-1].name == "load_project_skill"
            )
    with patch(
        "src.services.agent._nodes_llm.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=False),
    ):
        for base in (RESEARCH_TOOLS, WRITING_TOOLS, DATA_TOOLS):
            assert "load_project_skill" not in [
                tool.name for tool in tools_for_runtime_snapshot(base, state)
            ]
