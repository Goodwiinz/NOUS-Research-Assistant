"""Regression tests for writing-subgraph tool surface.

Covers the recovery-path inclusion of ``ingest_arxiv_papers`` so that
queries like "Summarize arxiv 2201.00978" can auto-chain
``summarize_document`` (recoverable) → ``ingest_arxiv_papers`` → retry
``summarize_document`` instead of dead-ending in a refusal.
"""

from __future__ import annotations

import pytest

from src.services.agent.subgraphs.writing_agent import (
    WRITING_DESTRUCTIVE_TOOLS,
    WRITING_TOOL_NAMES_LIST,
    WRITING_TOOLS,
    _build_writing_system_prompt,
)


@pytest.mark.unit
def test_ingest_arxiv_papers_present_in_writing_tools() -> None:
    """Recovery path requires the writing LLM to bind ingest_arxiv_papers."""
    assert "ingest_arxiv_papers" in WRITING_TOOL_NAMES_LIST
    assert any(t.name == "ingest_arxiv_papers" for t in WRITING_TOOLS)


@pytest.mark.unit
def test_ingest_arxiv_papers_marked_destructive() -> None:
    """ingest_arxiv_papers must trigger the writing-subgraph HITL gate."""
    assert "ingest_arxiv_papers" in WRITING_DESTRUCTIVE_TOOLS


@pytest.mark.unit
def test_search_arxiv_present_and_read_only() -> None:
    """Title resolution requires search_arxiv, and it must not be HITL-gated."""
    assert "search_arxiv" in WRITING_TOOL_NAMES_LIST
    assert any(t.name == "search_arxiv" for t in WRITING_TOOLS)
    # Read-only discovery — must NOT trigger the destructive-tool confirmation.
    assert "search_arxiv" not in WRITING_DESTRUCTIVE_TOOLS


@pytest.mark.unit
def test_writing_prompt_documents_title_resolution() -> None:
    """System prompt must tell the agent to resolve titles itself, not ask for ids."""
    prompt = _build_writing_system_prompt()
    assert "ingest_arxiv_papers" in prompt
    assert "search_arxiv" in prompt
    # The protocol must cover the title → search → ingest path.
    assert "title" in prompt.lower()


@pytest.mark.unit
def test_writing_tool_surface_unchanged_for_core_tools() -> None:
    """Core writing tools must remain available — ingest is additive."""
    expected_core = {
        "create_draft",
        "revise_draft",
        "create_project_note",
        "export_bibliography",
        "summarize_document",
        "compare_documents",
    }
    assert expected_core.issubset(set(WRITING_TOOL_NAMES_LIST))


def test_current_draft_lookup_is_read_only_and_available_to_writing() -> None:
    assert "get_current_draft" in WRITING_TOOL_NAMES_LIST
    assert "get_current_draft" not in WRITING_DESTRUCTIVE_TOOLS


def test_revise_draft_is_available_and_destructive() -> None:
    assert "revise_draft" in WRITING_TOOL_NAMES_LIST
    assert "revise_draft" in WRITING_DESTRUCTIVE_TOOLS


def test_revise_draft_schema_never_accepts_draft_content() -> None:
    from src.services.agent.tools import TOOL_REGISTRY

    schema = TOOL_REGISTRY.descriptor(
        "revise_draft"
    ).tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == {
        "instructions",
        "project_id",
        "base_version",
        "mode",
    }


def test_project_crud_reachable_from_writing() -> None:
    """capability-14: writing must be able to complete the full flow."""
    from src.services.agent.subgraphs.writing_agent import (
        WRITING_DESTRUCTIVE_TOOLS,
        WRITING_TOOL_NAMES_LIST,
    )
    from src.services.agent.tool_registry import ToolPolicyTag
    from src.services.agent.tools import TOOL_REGISTRY

    for name in ("create_project", "add_document_to_project"):
        assert name in WRITING_TOOL_NAMES_LIST, f"{name} missing from writing"
        assert name in WRITING_DESTRUCTIVE_TOOLS, f"{name} lost HITL in writing"
        # Pin the exact function the runtime interrupt gate calls, not just
        # the sibling frozenset — this is what _factory.py checks per tool_call.
        assert TOOL_REGISTRY.has_policy_in_subgraph(
            name, ToolPolicyTag.DESTRUCTIVE, "writing"
        )


def test_project_crud_reachable_from_research() -> None:
    """capability-14 symmetric half: research must also be able to note."""
    from src.services.agent.subgraphs.research_agent import (
        RESEARCH_DESTRUCTIVE_TOOLS,
        RESEARCH_TOOL_NAMES_LIST,
    )
    from src.services.agent.tool_registry import ToolPolicyTag
    from src.services.agent.tools import TOOL_REGISTRY

    assert "create_project_note" in RESEARCH_TOOL_NAMES_LIST
    assert "create_project_note" in RESEARCH_DESTRUCTIVE_TOOLS
    assert TOOL_REGISTRY.has_policy_in_subgraph(
        "create_project_note", ToolPolicyTag.DESTRUCTIVE, "research"
    )


def test_writing_prompt_lists_project_creation() -> None:
    from src.services.agent.subgraphs.agents_md_loader import load_agents_md

    prompt = load_agents_md("writing")
    assert "create_project" in prompt
    assert "add_document_to_project" in prompt


def test_research_prompt_lists_note_creation() -> None:
    from src.services.agent.subgraphs.agents_md_loader import load_agents_md

    prompt = load_agents_md("research")
    assert "create_project_note" in prompt
