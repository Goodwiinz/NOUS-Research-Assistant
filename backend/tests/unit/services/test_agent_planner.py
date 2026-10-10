"""Unit tests for the adaptive planner module.

Tests plan generation (with folded-in complexity gating) and planner
node behavior with mocked LLM calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from src.services.agent.planner import (
    AgentPlan,
    PlanStep,
    generate_plan,
    make_planner_node,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_NAMES = ["search_arxiv", "search_documents", "create_note", "ingest_arxiv"]
PAGE_CONTEXT = {
    "type": "project",
    "project_id": "proj-123",
    "document_id": "doc-456",
}


def _mock_llm_structured(return_value):
    """Create a mock LLM that supports with_structured_output().ainvoke()."""
    mock_llm = MagicMock()
    structured = AsyncMock(return_value=return_value)
    mock_llm.with_structured_output.return_value.ainvoke = structured
    return mock_llm


# ---------------------------------------------------------------------------
# test_generate_plan_returns_valid_plan
# ---------------------------------------------------------------------------


class TestGeneratePlan:
    async def test_generate_plan_returns_valid_plan(self):
        """generate_plan should return an AgentPlan with valid PlanSteps."""
        expected_plan = AgentPlan(
            steps=[
                PlanStep(
                    step=1,
                    description="Search arXiv for transformer papers",
                    tool="search_arxiv",
                    args_hint={"query": "transformer architectures"},
                    depends_on=[],
                ),
                PlanStep(
                    step=2,
                    description="Ingest the top result",
                    tool="ingest_arxiv",
                    args_hint={"paper_id": "<from step 1>"},
                    depends_on=[1],
                ),
                PlanStep(
                    step=3,
                    description="Create a summary note",
                    tool="create_note",
                    args_hint={"title": "Transformer Summary"},
                    depends_on=[2],
                ),
            ],
            reasoning="The user wants a multi-step research workflow.",
        )

        mock_llm = _mock_llm_structured(expected_plan)

        with patch(
            "src.services.agent.planner._build_planner_llm", return_value=mock_llm
        ):
            result = await generate_plan(
                "Find transformer papers, ingest them, create a summary note",
                TOOL_NAMES,
                PAGE_CONTEXT,
            )

        assert isinstance(result, AgentPlan)
        assert len(result.steps) == 3
        for step in result.steps:
            assert isinstance(step, PlanStep)
            assert step.tool in TOOL_NAMES
            assert step.description
        assert result.reasoning

    async def test_generate_plan_prompt_carries_query_tools_and_gating(self):
        """The single planner prompt must carry the query, every tool name,
        the page context, and the folded-in complexity gate (empty steps for
        simple queries) — the old separate check_complexity call is gone."""
        mock_llm = _mock_llm_structured(AgentPlan(steps=[]))

        with patch(
            "src.services.agent.planner._build_planner_llm", return_value=mock_llm
        ):
            result = await generate_plan("search for papers", TOOL_NAMES, PAGE_CONTEXT)

        ainvoke = mock_llm.with_structured_output.return_value.ainvoke
        (messages,), _ = ainvoke.call_args
        assert len(messages) == 1 and isinstance(messages[0], HumanMessage)
        prompt = messages[0].content
        assert "search for papers" in prompt
        for tool in TOOL_NAMES:
            assert tool in prompt
        assert "proj-123" in prompt
        # Complexity gating folded into the prompt
        assert "FEWER" in prompt and "empty steps list" in prompt
        assert "normally returns a completed terminal result" in prompt
        assert "pending result is terminal for the turn" in prompt
        assert "revise_draft is synchronous" in prompt

        # Empty plan passes through unmodified (node layer treats it as "simple").
        assert result.steps == []

    async def test_generate_plan_keeps_steps_after_create_draft(self):
        model_plan = AgentPlan(
            steps=[
                PlanStep(
                    step=1,
                    description="Find sources",
                    tool="search_arxiv",
                ),
                PlanStep(
                    step=2,
                    description="Create the draft",
                    tool="create_draft",
                    depends_on=[1],
                ),
                PlanStep(
                    step=3,
                    description="Read the pending draft",
                    tool="get_current_draft",
                    depends_on=[2],
                ),
            ],
            reasoning="Find evidence, then start draft generation.",
        )
        mock_llm = _mock_llm_structured(model_plan)

        with patch(
            "src.services.agent.planner._build_planner_llm", return_value=mock_llm
        ):
            result = await generate_plan(
                "Find sources, create a draft, then inspect it",
                ["search_arxiv", "create_draft", "get_current_draft"],
                PAGE_CONTEXT,
            )

        assert [step.tool for step in result.steps] == [
            "search_arxiv",
            "create_draft",
            "get_current_draft",
        ]
        assert result.steps[-1].depends_on == [2]
        assert result.reasoning == model_plan.reasoning


# ---------------------------------------------------------------------------
# test_planner_node_skips_if_plan_exists
# ---------------------------------------------------------------------------


class TestPlannerNode:
    async def test_planner_node_skips_if_plan_exists(self):
        """Node should return empty dict when state['plan'] is already populated."""
        node_fn = make_planner_node(TOOL_NAMES)

        state = {
            "messages": [HumanMessage(content="Do something complex")],
            "page_context": PAGE_CONTEXT,
            "plan": [{"step": 1, "tool": "search_arxiv", "description": "existing"}],
        }

        result = await node_fn(state, {})
        assert result == {}

    async def test_planner_node_skips_simple_queries(self):
        """Node should return empty dict when the plan comes back with <3 steps
        (the model judged the query simple)."""
        mock_llm = _mock_llm_structured(AgentPlan(steps=[]))

        with patch(
            "src.services.agent.planner._build_planner_llm",
            return_value=mock_llm,
        ):
            node_fn = make_planner_node(TOOL_NAMES)

            state = {
                "messages": [HumanMessage(content="search for papers")],
                "page_context": PAGE_CONTEXT,
                "plan": [],
            }

            result = await node_fn(state, {})

        assert result == {}

    async def test_planner_node_generates_plan_for_complex_queries(self):
        """Node should generate and return plan for complex queries."""
        expected_plan = AgentPlan(
            steps=[
                PlanStep(
                    step=1,
                    description="Search arXiv",
                    tool="search_arxiv",
                    args_hint={"query": "transformers"},
                    depends_on=[],
                ),
                PlanStep(
                    step=2,
                    description="Ingest paper",
                    tool="ingest_arxiv",
                    args_hint={"paper_id": "123"},
                    depends_on=[1],
                ),
                PlanStep(
                    step=3,
                    description="Create note",
                    tool="create_note",
                    args_hint={"title": "Summary"},
                    depends_on=[2],
                ),
            ],
            reasoning="Multi-step research workflow",
        )

        mock_llm_plan = _mock_llm_structured(expected_plan)

        with patch(
            "src.services.agent.planner._build_planner_llm",
            return_value=mock_llm_plan,
        ) as build_llm:
            node_fn = make_planner_node(TOOL_NAMES)

            state = {
                "messages": [
                    HumanMessage(
                        # Phase 1 raised the skip threshold to 12 words.
                        content=(
                            "Find recent transformer papers, ingest them into my "
                            "project, summarize each, and create a research note"
                        )
                    )
                ],
                "page_context": PAGE_CONTEXT,
                "plan": [],
            }

            result = await node_fn(state, {})

        assert "plan" in result
        assert len(result["plan"]) == 3
        assert result["plan"][0]["tool"] == "search_arxiv"
        assert result["plan"][2]["depends_on"] == [2]
        # Phase 1: the planner's rationale is plumbed through, not dropped.
        assert result["plan_reasoning"] == "Multi-step research workflow"
        # Single planner LLM call — the old separate complexity check is gone.
        build_llm.assert_called_once()

    async def test_planner_node_caps_reasoning_at_2000_chars(self):
        """plan_reasoning is capped to 2000 chars in planner_node, once,
        regardless of how long the model's rationale string is."""
        long_reasoning = "x" * 2500
        expected_plan = AgentPlan(
            steps=[
                PlanStep(
                    step=1,
                    description="Search arXiv",
                    tool="search_arxiv",
                    args_hint={"query": "transformers"},
                    depends_on=[],
                ),
                PlanStep(
                    step=2,
                    description="Ingest paper",
                    tool="ingest_arxiv",
                    args_hint={"paper_id": "123"},
                    depends_on=[1],
                ),
                PlanStep(
                    step=3,
                    description="Create note",
                    tool="create_note",
                    args_hint={"title": "Summary"},
                    depends_on=[2],
                ),
            ],
            reasoning=long_reasoning,
        )

        mock_llm_plan = _mock_llm_structured(expected_plan)

        with patch(
            "src.services.agent.planner._build_planner_llm",
            return_value=mock_llm_plan,
        ):
            node_fn = make_planner_node(TOOL_NAMES)

            state = {
                "messages": [
                    HumanMessage(
                        content=(
                            "Find recent transformer papers, ingest them into my "
                            "project, summarize each, and create a research note"
                        )
                    )
                ],
                "page_context": PAGE_CONTEXT,
                "plan": [],
            }

            result = await node_fn(state, {})

        assert len(result["plan_reasoning"]) == 2000
        assert result["plan_reasoning"] == long_reasoning[:2000]

    async def test_planner_node_skip_paths_omit_reasoning(self):
        """Every skip path returns a bare {} — no plan_reasoning key leaks
        through when the planner never ran (state reset already seeds the
        AgentState default of "" for these turns)."""
        node_fn = make_planner_node(TOOL_NAMES)

        # Skip: plan already exists this turn.
        result = await node_fn(
            {
                "messages": [HumanMessage(content="Do something complex")],
                "page_context": PAGE_CONTEXT,
                "plan": [{"step": 1, "tool": "search_arxiv", "description": "x"}],
            },
            {},
        )
        assert result == {}

        # Skip: non-project page context.
        result = await node_fn(
            {
                "messages": [HumanMessage(content="Do something complex")],
                "page_context": {"type": "chat"},
                "plan": [],
            },
            {},
        )
        assert result == {}
