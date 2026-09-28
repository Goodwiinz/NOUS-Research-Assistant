"""Behavioral contracts for frozen tool capability batches."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

pytestmark = pytest.mark.unit


def _authorized_mutation_context() -> tuple[dict[str, Any], dict[str, Any]]:
    """A valid Task 2 operation scope; a missing anchor must not fake this test."""
    user_id = str(uuid4())
    organization_id = str(uuid4())
    return (
        {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "task3-valid-turn-17",
        },
        {
            "configurable": {
                "user_id": user_id,
                "organization_id": organization_id,
                "thread_id": "task3-valid-thread-17",
            }
        },
    )


def test_writing_projection_includes_tenant_scoped_document_retrieval() -> None:
    from src.services.agent.tools import TOOL_REGISTRY

    writing = {
        descriptor.name
        for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("writing")
    }

    assert {"search_documents", "do_kb_retrieve"}.issubset(writing)


def test_main_router_does_not_approve_mixed_available_and_unregistered_batch() -> None:
    from src.services.agent._builders import should_continue
    from src.services.agent.state import AgentState

    operation_state, _config = _authorized_mutation_context()
    state = {
        **operation_state,
        "messages": [
            HumanMessage(content="Create this project note", id="task3-valid-turn-17"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "task3-supported-write",
                        "name": "create_project_note",
                        "args": {
                            "title": "Review notes",
                            "content": "A grounded note.",
                            "project_id": str(uuid4()),
                        },
                    },
                    {
                        "id": "task3-unregistered",
                        "name": "not_a_registered_tool",
                        "args": {},
                    },
                ],
            ),
        ],
        "tool_loop_count": 0,
    }

    assert should_continue(cast(AgentState, state)) == "tool_node"


async def test_filtered_node_rejects_whole_mixed_batch_before_mutation_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools

    operation_state, config = _authorized_mutation_context()
    execute = AsyncMock(
        return_value={
            "message": ToolMessage(
                content='{"status":"unexpected"}',
                tool_call_id="task3-supported-write",
            ),
            "execution": {"status": "completed"},
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }
    )
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)
    node = _nodes_tools.make_filtered_tool_node({"create_project_note"})
    calls = [
        {
            "id": "task3-supported-write",
            "name": "create_project_note",
            "args": {
                "title": "Review notes",
                "content": "A grounded note.",
                "project_id": str(uuid4()),
            },
        },
        {
            "id": "task3-out-of-scope",
            "name": "execute_code",
            "args": {"code": "print('must not execute')"},
        },
    ]

    result = await node(
        {
            **operation_state,
            "messages": [
                HumanMessage(
                    content="Create this project note", id="task3-valid-turn-17"
                ),
                AIMessage(content="", tool_calls=calls),
            ],
            "page_context": {},
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
        },
        config,
    )

    execute.assert_not_awaited()
    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "task3-supported-write",
        "task3-out-of-scope",
    ]
    assert all(message.status == "error" for message in tool_messages)
    assert result["tool_executions"] == []


async def test_main_tool_node_rejects_whole_batch_before_mutation_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools

    operation_state, config = _authorized_mutation_context()
    execute = AsyncMock(
        return_value={
            "message": ToolMessage(
                content='{"status":"unexpected"}',
                tool_call_id="task3-supported-write",
            ),
            "execution": {"status": "completed"},
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }
    )
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute)
    calls = [
        {
            "id": "task3-supported-write",
            "name": "create_project_note",
            "args": {
                "title": "Review notes",
                "content": "A grounded note.",
                "project_id": str(uuid4()),
            },
        },
        {"id": "task3-unregistered", "name": "not_a_registered_tool", "args": {}},
    ]

    result = await _nodes_tools.tool_node(
        {
            **operation_state,
            "messages": [
                HumanMessage(
                    content="Create this project note", id="task3-valid-turn-17"
                ),
                AIMessage(content="", tool_calls=calls),
            ],
            "page_context": {},
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
        },
        config,
    )

    execute.assert_not_awaited()
    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "task3-supported-write",
        "task3-unregistered",
    ]
    assert all(message.status == "error" for message in tool_messages)
    assert result["tool_executions"] == []
