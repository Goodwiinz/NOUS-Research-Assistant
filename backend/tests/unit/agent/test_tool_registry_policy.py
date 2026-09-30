"""Regression tests for registry-owned agent execution policy."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage

from src.services.agent.state import AgentState


@pytest.mark.unit
def test_main_llm_binding_queries_registry_for_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_llm

    expected = [Mock(name="registry_tool")]
    registry = Mock()
    registry.descriptors_for_intent.return_value = [Mock(tool=expected[0])]
    monkeypatch.setattr(_nodes_llm, "TOOL_REGISTRY", registry)

    assert _nodes_llm._get_tools_for_intent("research") == expected
    registry.descriptors_for_intent.assert_called_once_with("research")


@pytest.mark.unit
def test_main_routing_queries_registry_destructive_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _builders

    registry = Mock()
    registry.has_policy.return_value = True
    monkeypatch.setattr(_builders, "TOOL_REGISTRY", registry)

    route = _builders.should_continue(
        cast(
            AgentState,
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"id": "call-1", "name": "search_arxiv", "args": {}}
                        ],
                    )
                ],
                "tool_loop_count": 0,
            },
        )
    )

    assert route == "interrupt_node"
    registry.has_policy.assert_called_once()


@pytest.mark.unit
def test_subgraph_routing_requires_descriptor_policy_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent.subgraphs import research_agent, writing_agent

    registry = Mock()
    registry.has_policy.return_value = True
    registry.has_policy_in_subgraph.return_value = False
    monkeypatch.setattr(research_agent, "TOOL_REGISTRY", registry)
    monkeypatch.setattr(writing_agent, "TOOL_REGISTRY", registry)
    research_state = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "ingest_arxiv_papers",
                            "args": {},
                        }
                    ],
                )
            ],
            "tool_loop_count": 0,
        },
    )
    writing_state = cast(
        AgentState,
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call-2", "name": "create_draft", "args": {}}],
                )
            ],
            "tool_loop_count": 0,
        },
    )

    assert (
        research_agent.research_should_continue(research_state) == "research_tool_node"
    )
    assert writing_agent.writing_should_continue(writing_state) == "writing_tool_node"
    assert registry.has_policy_in_subgraph.call_count == 2


@pytest.mark.unit
async def test_disabled_registry_tool_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import tools, tools_impl
    from src.services.agent.tool_registry import ToolRegistry

    disabled = tuple(
        (
            replace(descriptor, enabled=False)
            if descriptor.name == "search_arxiv"
            else descriptor
        )
        for descriptor in tools.TOOL_REGISTRY.descriptors
    )
    monkeypatch.setattr(tools, "TOOL_REGISTRY", ToolRegistry(disabled))
    handler = AsyncMock(return_value={"unexpected": True})
    monkeypatch.setattr(tools_impl, "_tool_search_arxiv", handler)

    result = await tools_impl.execute_tool("search_arxiv", {"query": "test"})

    assert result == {"error": "Unknown tool: search_arxiv"}
    handler.assert_not_awaited()


@pytest.mark.unit
async def test_filtered_node_rejects_registered_name_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools, tools
    from src.services.agent.tool_registry import ToolRegistry

    disabled = tuple(
        (
            replace(descriptor, enabled=False)
            if descriptor.name == "search_arxiv"
            else descriptor
        )
        for descriptor in tools.TOOL_REGISTRY.descriptors
    )
    monkeypatch.setattr(_nodes_tools, "TOOL_REGISTRY", ToolRegistry(disabled))
    execute = AsyncMock()
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)

    node = _nodes_tools.make_filtered_tool_node({"search_arxiv"})
    result = await node(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call-1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
        },
        {},
    )

    execute.assert_not_awaited()
    assert "not available" in result["messages"][0].content


def test_general_lane_binds_semantic_retrieval_and_compare() -> None:
    """The classifier demotes weak-evidence turns to general on the premise
    that general is a superset of the specialist lanes. That premise was false
    for exactly the two content-level tools: a compare/content question landing
    in general had only title search (live miss 2026-08-12: 'Compare the METR
    and MIT studies' -> search_documents -> 0 hits -> 'no documents found')."""
    from src.services.agent.tools import TOOL_REGISTRY

    general = {d.name for d in TOOL_REGISTRY.descriptors_for_intent("general")}
    assert "do_kb_retrieve" in general
    assert "compare_documents" in general
    assert "search_documents" in general


def test_search_documents_description_disclaims_content_search() -> None:
    """The schema description steers the model; it must not claim content
    search when the implementation is a title/filename ILIKE."""
    from src.services.agent.tools import TOOL_REGISTRY

    desc = TOOL_REGISTRY.descriptor("search_documents").tool.description
    # The old text — "Search the user's indexed documents by title or content"
    # — also contained "content", so assert the disclaimer, not the substring.
    assert "does not search" in desc.lower()
    assert "do_kb_retrieve" in desc  # points at the right tool
