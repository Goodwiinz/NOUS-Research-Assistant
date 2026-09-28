"""Writing/data subgraph loop-ceiling routing.

The research subgraph gained a forced-synthesis node after trace 019e1903
(loop ceiling tripped with unanswered tool_calls → AIMessage/ToolMessage
pairing violation + empty final answer). Writing and data had the same
exit path but no synthesis node — these tests pin the parity fix.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent.subgraphs._factory import _is_execution_evidence
from src.services.agent.subgraphs.data_agent import (
    MAX_DATA_TOOL_LOOPS,
    data_should_continue,
)
from src.services.agent.subgraphs.research_agent import (
    MAX_RESEARCH_TOOL_LOOPS,
    research_force_synthesis_node,
)
from src.services.agent.subgraphs.writing_agent import (
    MAX_WRITING_TOOL_LOOPS,
    writing_should_continue,
)


def _state_with_pending_tool_calls(tool_name: str, loop_count: int) -> dict:
    """Build a minimal state where the last AI message has a tool_call."""
    return {
        "messages": [
            HumanMessage(content="do the thing"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": tool_name,
                        "args": {},
                    }
                ],
            ),
        ],
        "tool_loop_count": loop_count,
        "error_count": 0,
    }


@pytest.mark.unit
def test_unsuccessful_tool_message_is_not_execution_evidence():
    message = ToolMessage(
        content="tool failed",
        tool_call_id="call_failed",
        status="error",
    )

    assert _is_execution_evidence(message) is False


@pytest.mark.unit
def test_pending_tool_message_is_started_execution_evidence():
    message = ToolMessage(
        content='{"status": "pending", "draft_id": "draft-1"}',
        tool_call_id="call_pending",
        status="success",
    )

    assert _is_execution_evidence(message) is True


# ---------------------------------------------------------------------------
# Writing subgraph
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writing_continues_below_ceiling():
    state = _state_with_pending_tool_calls(
        "summarize_document", MAX_WRITING_TOOL_LOOPS - 1
    )
    assert writing_should_continue(state) == "writing_tool_node"


@pytest.mark.unit
def test_writing_destructive_routes_to_interrupt_below_ceiling():
    state = _state_with_pending_tool_calls("create_draft", MAX_WRITING_TOOL_LOOPS - 1)
    assert writing_should_continue(state) == "writing_interrupt_node"


@pytest.mark.unit
def test_writing_routes_to_force_synthesis_at_ceiling():
    """At the ceiling WITH unanswered tool_calls, route to forced synthesis
    instead of exiting with a dangling tool-call AIMessage."""
    state = _state_with_pending_tool_calls("summarize_document", MAX_WRITING_TOOL_LOOPS)
    assert writing_should_continue(state) == "writing_force_synthesis_node"


@pytest.mark.unit
def test_writing_no_loop_back_into_force_synthesis_after_first_fire():
    state = _state_with_pending_tool_calls(
        "summarize_document", MAX_WRITING_TOOL_LOOPS + 2
    )
    state["_force_synthesis_fired"] = True
    assert writing_should_continue(state) == "writing_reflection_gate"


@pytest.mark.unit
def test_writing_routes_to_reflection_when_no_pending_tool_calls():
    state = {
        "messages": [
            HumanMessage(content="summarize"),
            AIMessage(content="Here is the summary..."),
        ],
        "tool_loop_count": MAX_WRITING_TOOL_LOOPS,
        "error_count": 0,
    }
    assert writing_should_continue(state) == "writing_reflection_gate"


@pytest.mark.unit
def test_writing_error_count_short_circuits():
    state = _state_with_pending_tool_calls("summarize_document", 0)
    state["error_count"] = 3
    assert writing_should_continue(state) == "writing_reflection_gate"


# ---------------------------------------------------------------------------
# Data subgraph
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_data_continues_below_ceiling():
    state = _state_with_pending_tool_calls(
        "search_knowledge_graph", MAX_DATA_TOOL_LOOPS - 1
    )
    assert data_should_continue(state) == "data_tool_node"


@pytest.mark.unit
def test_data_routes_to_force_synthesis_at_ceiling():
    state = _state_with_pending_tool_calls(
        "search_knowledge_graph", MAX_DATA_TOOL_LOOPS
    )
    assert data_should_continue(state) == "data_force_synthesis_node"


@pytest.mark.unit
def test_data_no_loop_back_into_force_synthesis_after_first_fire():
    state = _state_with_pending_tool_calls(
        "search_knowledge_graph", MAX_DATA_TOOL_LOOPS + 2
    )
    state["_force_synthesis_fired"] = True
    assert data_should_continue(state) == "data_reflection_gate"


@pytest.mark.unit
def test_data_routes_to_reflection_when_no_pending_tool_calls():
    state = {
        "messages": [
            HumanMessage(content="show entities"),
            AIMessage(content="Here are the entities..."),
        ],
        "tool_loop_count": MAX_DATA_TOOL_LOOPS,
        "error_count": 0,
    }
    assert data_should_continue(state) == "data_reflection_gate"


@pytest.mark.unit
def test_data_error_count_short_circuits():
    state = _state_with_pending_tool_calls("search_knowledge_graph", 0)
    state["error_count"] = 3
    assert data_should_continue(state) == "data_reflection_gate"


# ---------------------------------------------------------------------------
# Graph wiring — synthesis nodes exist and exit through reflection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writing_graph_has_force_synthesis_node():
    from src.services.agent.subgraphs.writing_agent import build_writing_subgraph

    compiled = build_writing_subgraph().compile()
    assert "writing_force_synthesis_node" in compiled.get_graph().nodes


@pytest.mark.unit
def test_data_graph_has_force_synthesis_node():
    from src.services.agent.subgraphs.data_agent import build_data_subgraph

    compiled = build_data_subgraph().compile()
    assert "data_force_synthesis_node" in compiled.get_graph().nodes


# ---------------------------------------------------------------------------
# Force-synthesis node bodies — strip-and-synthesize + timeout fallback
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402


def _ceiling_state(tool_name: str, loop_count: int) -> dict:
    return {
        "messages": [
            HumanMessage(content="do the thing"),
            AIMessage(content="partial findings from tools"),
            AIMessage(
                content="",
                tool_calls=[{"id": "call_n", "name": tool_name, "args": {}}],
            ),
        ],
        "tool_loop_count": loop_count,
        "error_count": 0,
        "thread_id": "t-ceiling",
    }


def _capturing_llm(response_text: str):
    llm = AsyncMock()
    captured: dict = {}

    async def fake_ainvoke(messages, config=None):
        captured["messages"] = messages
        return AIMessage(content=response_text)

    llm.ainvoke = fake_ainvoke
    return llm, captured


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_force_synthesis_strips_tool_calls_and_synthesizes():
    from src.services.agent.subgraphs.writing_agent import writing_force_synthesis_node

    llm, captured = _capturing_llm("final synthesized draft")
    state = _ceiling_state("summarize_document", MAX_WRITING_TOOL_LOOPS)

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        result = await writing_force_synthesis_node(state, {"configurable": {}})

    assert result["messages"][0].content == "final synthesized draft"
    assert result["tool_loop_count"] == MAX_WRITING_TOOL_LOOPS + 1
    assert result["_force_synthesis_fired"] is True
    # The trailing tool-call AIMessage was stripped before the LLM saw it.
    assert not any(getattr(m, "tool_calls", None) for m in captured["messages"])
    prompt = captured["messages"][0].content
    assert "unanswered tool request was not executed" in prompt
    assert "Only completed, non-placeholder ToolMessages" in prompt
    assert "must be described as not executed" in prompt
    assert "execution limit stopped the remaining work" in prompt
    assert "emit tool-call syntax" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_research_force_synthesis_stops_unmatched_stage_six():
    llm, captured = _capturing_llm(
        "The tool limit stopped the remaining work after the verified results."
    )
    state = _ceiling_state("do_kb_retrieve", MAX_RESEARCH_TOOL_LOOPS)

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        result = await research_force_synthesis_node(state, {"configurable": {}})

    assert result["tool_loop_count"] == MAX_RESEARCH_TOOL_LOOPS + 1
    assert result["_force_synthesis_fired"] is True
    assert result["messages"][-1].tool_calls == []
    assert "limit" in str(result["messages"][-1].content).lower()
    assert not any(
        getattr(message, "tool_calls", None) for message in captured["messages"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_research_force_synthesis_marks_skipped_placeholder_as_non_evidence():
    llm, captured = _capturing_llm("Stage 4 was not executed.")
    state = _ceiling_state("do_kb_retrieve", MAX_RESEARCH_TOOL_LOOPS)
    state["messages"].insert(
        -2,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_skipped",
                    "name": "do_kb_retrieve",
                    "args": {"query": "NOUS-LONG-4"},
                }
            ],
        ),
    )

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        await research_force_synthesis_node(state, {"configurable": {}})

    placeholder = next(
        message
        for message in captured["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "call_skipped"
    )
    assert placeholder.content == '{"status": "skipped"}'
    prompt = captured["messages"][0].content
    assert (
        "skipped placeholders and failed or error ToolMessages do not prove execution"
        in prompt
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_force_synthesis_reports_pending_write_as_started():
    from src.services.agent.subgraphs.writing_agent import writing_force_synthesis_node

    llm, captured = _capturing_llm("The draft was started and remains pending.")
    state = _ceiling_state("summarize_document", MAX_WRITING_TOOL_LOOPS)
    state["messages"][1:1] = [
        AIMessage(
            content="",
            tool_calls=[{"id": "call_pending", "name": "create_draft", "args": {}}],
        ),
        ToolMessage(
            content='{"status": "pending", "draft_id": "draft-1"}',
            tool_call_id="call_pending",
            status="success",
        ),
    ]

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        await writing_force_synthesis_node(state, {"configurable": {}})

    prompt = captured["messages"][0].content
    pending_result = next(
        message
        for message in captured["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "call_pending"
    )
    assert '"status": "pending"' in pending_result.content
    assert '"draft_id": "draft-1"' in pending_result.content
    assert (
        "A pending status proves the tool started but does not prove completion"
        in prompt
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_force_synthesis_timeout_emits_fallback():
    from src.services.agent.subgraphs.writing_agent import writing_force_synthesis_node

    llm = AsyncMock()
    llm.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())
    state = _ceiling_state("summarize_document", MAX_WRITING_TOOL_LOOPS)

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        result = await writing_force_synthesis_node(state, {"configurable": {}})

    # Degraded but never dangling: a real AIMessage with content, flag set.
    assert result["messages"][0].content
    assert not result["messages"][0].tool_calls
    assert result["_force_synthesis_fired"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_data_force_synthesis_strips_tool_calls_and_synthesizes():
    from src.services.agent.subgraphs.data_agent import data_force_synthesis_node

    llm, captured = _capturing_llm("entities: A relates to B")
    state = _ceiling_state("search_knowledge_graph", MAX_DATA_TOOL_LOOPS)

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        result = await data_force_synthesis_node(state, {"configurable": {}})

    assert result["messages"][0].content == "entities: A relates to B"
    assert result["tool_loop_count"] == MAX_DATA_TOOL_LOOPS + 1
    assert result["_force_synthesis_fired"] is True
    assert not any(getattr(m, "tool_calls", None) for m in captured["messages"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_data_force_synthesis_timeout_emits_fallback():
    from src.services.agent.subgraphs.data_agent import data_force_synthesis_node

    llm = AsyncMock()
    llm.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())
    state = _ceiling_state("search_knowledge_graph", MAX_DATA_TOOL_LOOPS)

    with patch("src.services.agent.graph._build_llm", return_value=llm):
        result = await data_force_synthesis_node(state, {"configurable": {}})

    assert result["messages"][0].content
    assert not result["messages"][0].tool_calls
    assert result["_force_synthesis_fired"] is True
