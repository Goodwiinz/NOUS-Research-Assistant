"""Regression tests for project-grounded writing turns."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.services.agent.subgraphs.writing_agent import writing_llm_node


def test_writing_prompt_forbids_claiming_pending_artifacts_are_complete() -> None:
    prompt = (
        Path(__file__).parents[3] / "src/services/agent/subgraphs/AGENTS_writing.md"
    ).read_text()
    assert "A pending artifact is not complete" in prompt
    assert "repeat the tool's status" in prompt
    assert "deliver their substantive results" in prompt
    assert "status-only" in prompt
    assert "do not write a substitute draft body" in prompt
    assert "Treat tool results as the evidence boundary" in prompt
    assert "A historical `pending` result is not live status" in prompt
    assert "call `get_current_draft` before answering" in prompt
    assert "does not prove that a particular task completed" in prompt
    assert "list_project_documents` says `project_id is required" in prompt
    assert "call `list_projects`" in prompt
    assert "Never route a revision through `create_draft`" in prompt
    assert "pending `create_draft` result is terminal" in prompt


def _grounded_state(
    query: str = (
        "Based on the documents in this project, summarize the main "
        "research topic in two sentences."
    ),
) -> dict:
    return {
        "messages": [HumanMessage(content=query)],
        "page_context": {
            "type": "project",
            "project_id": "00000000-0000-0000-0000-000000000001",
        },
        "retrieved_contexts": [
            {
                "title": "Project paper",
                "content": "The project studies grounded retrieval systems.",
            }
        ],
        "plan": [],
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        AGENT_LIGHTWEIGHT_SYNTHESIS=True,
        AGENT_PARALLEL_TOOL_CALLS=False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_node_injects_retrieved_context() -> None:
    llm = MagicMock()
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content="Grounded summary"))
    llm.bind_tools.return_value = bound

    with (
        patch("src.core.config.get_settings", return_value=_settings()),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            return_value=llm,
        ),
        patch(
            "src.services.agent.llm_factory.build_lightweight_llm",
            return_value=llm,
        ),
        # Tool-decision turns build the main LLM now, not the lightweight
        # one — without this the node reaches the real Azure config guard.
        patch(
            "src.services.agent.graph._build_llm",
            return_value=llm,
        ),
    ):
        await writing_llm_node(_grounded_state(), config={})

    assert bound.ainvoke.await_args is not None
    messages = bound.ainvoke.await_args.args[0]
    system_text = "\n".join(
        str(message.content)
        for message in messages
        if isinstance(message, SystemMessage)
    )
    assert "Retrieved context:" in system_text
    assert "[Doc 1]" in system_text
    assert "title: Project paper" in system_text
    assert "The project studies grounded retrieval systems." in system_text
    assert "00000000-0000-0000-0000-000000000001" in system_text
    assert "use this project_id" in system_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grounded_simple_summary_binds_no_tools() -> None:
    llm = MagicMock()
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content="Grounded summary"))
    llm.bind_tools.return_value = bound

    with (
        patch("src.core.config.get_settings", return_value=_settings()),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            return_value=llm,
        ),
        patch(
            "src.services.agent.llm_factory.build_lightweight_llm",
            return_value=llm,
        ),
        # Tool-decision turns build the main LLM now, not the lightweight
        # one — without this the node reaches the real Azure config guard.
        patch(
            "src.services.agent.graph._build_llm",
            return_value=llm,
        ),
    ):
        await writing_llm_node(_grounded_state(), config={})

    llm.bind_tools.assert_called_once_with([], parallel_tool_calls=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grounded_create_note_request_keeps_writing_tools() -> None:
    llm = MagicMock()
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content="Creating note"))
    llm.bind_tools.return_value = bound
    state = _grounded_state(
        "Based on the documents in this project, create a note about the topic."
    )

    with (
        patch("src.core.config.get_settings", return_value=_settings()),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            return_value=llm,
        ),
        patch(
            "src.services.agent.llm_factory.build_lightweight_llm",
            return_value=llm,
        ),
        # Tool-decision turns build the main LLM now, not the lightweight
        # one — without this the node reaches the real Azure config guard.
        patch(
            "src.services.agent.graph._build_llm",
            return_value=llm,
        ),
    ):
        await writing_llm_node(state, config={})

    bound_tools = llm.bind_tools.call_args.args[0]
    assert any(tool.name == "create_project_note" for tool in bound_tools)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_create_draft_returns_without_synthesis() -> None:
    state = {
        "messages": [
            HumanMessage(content="Create a draft and apply my preferences."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "draft-call",
                        "name": "create_draft",
                        "args": {},
                    }
                ],
            ),
            ToolMessage(
                content=(
                    '{"status":"pending","task_id":"task-123",'
                    '"message":"Draft generation started."}'
                ),
                tool_call_id="draft-call",
            ),
        ]
    }

    with (
        patch(
            "src.core.config.get_settings",
            side_effect=AssertionError("pending drafts must not construct an LLM"),
        ),
        patch(
            "src.services.agent.graph._build_llm",
            side_effect=AssertionError("pending drafts must not construct an LLM"),
        ),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            side_effect=AssertionError("pending drafts must not construct an LLM"),
        ),
    ):
        result = await writing_llm_node(state, config={})

    content = result["messages"][0].content
    assert "Draft generation started." in content
    assert "Status: pending" in content
    assert "Task ID: task-123" in content
    assert "preferences" not in content.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batched_pending_create_draft_is_terminal() -> None:
    state = {
        "messages": [
            HumanMessage(content="Create a draft and summarize the project."),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "summary-call", "name": "summarize_document", "args": {}},
                    {"id": "draft-call", "name": "create_draft", "args": {}},
                ],
            ),
            ToolMessage(content='{"status":"completed"}', tool_call_id="summary-call"),
            ToolMessage(
                content=(
                    '{"status":"pending","task_id":"task-123",'
                    '"message":"Draft generation started."}'
                ),
                tool_call_id="draft-call",
            ),
        ]
    }

    llm = MagicMock()
    bound = MagicMock()
    combined = AIMessage(
        content="The summary completed. Draft generation started and is pending."
    )
    bound.ainvoke = AsyncMock(return_value=combined)
    llm.bind_tools.return_value = bound

    with (
        patch("src.core.config.get_settings", return_value=_settings()),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            return_value=llm,
        ),
        patch(
            "src.services.agent.graph._build_llm",
            side_effect=AssertionError("mixed pending batch must use synthesis"),
        ),
    ):
        result = await writing_llm_node(state, config={})

    llm.bind_tools.assert_called_once_with([], parallel_tool_calls=False)
    assert result["messages"] == [combined]
    system_text = "\n".join(
        str(message.content)
        for message in bound.ainvoke.await_args.args[0]
        if isinstance(message, SystemMessage)
    )
    assert "pending and terminal" in system_text
    assert "substantive completed tool results" in system_text
