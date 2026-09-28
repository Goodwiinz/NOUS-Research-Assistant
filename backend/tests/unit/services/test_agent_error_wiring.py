"""Tests for error recovery wiring in graph.py."""

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage


@pytest.mark.unit
class TestErrorRecoveryWiring:
    @pytest.mark.asyncio
    async def test_transient_error_retried_not_counted(self):
        """Transient errors should be retried and not increment error_count."""
        from src.services.agent.graph import _execute_single_tool

        call_count = 0

        async def mock_execute_tool(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise asyncio.TimeoutError()
            return {"status": "ok", "papers": []}

        # search_arxiv is in _NO_OUTER_RETRY_TOOLS (the tool retries internally
        # via arxiv_service to avoid 2x wall-clock amplification). Use
        # search_documents which still receives the outer retry_transient.
        tc = {"name": "search_documents", "args": {"query": "test"}, "id": "tc1"}
        config = {
            "configurable": {
                "user_id": "u1",
                "organization_id": "org1",
                "thread_id": "thread1",
            }
        }
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "turn1",
        }

        with patch(
            "src.services.agent.graph.execute_tool", side_effect=mock_execute_tool
        ):
            result = await _execute_single_tool(
                tc, config, {}, operation_context=operation_context
            )

        assert result["error_increment"] == 0  # Transient retry succeeded
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_recoverable_payload_has_suggestion(self):
        """Recoverable errors from payloads should include suggestion."""
        from src.services.agent.graph import _execute_single_tool

        document_id = str(uuid4())

        async def mock_execute_tool(**kwargs):
            return {"error": f"Document {document_id} not found"}

        tc = {
            "name": "add_document_to_project",
            "args": {
                "document_id": document_id,
                "project_id": str(uuid4()),
            },
            "id": "tc2",
        }
        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "thread_id": str(uuid4()),
            }
        }
        operation_context = {
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": str(uuid4()),
        }

        with patch(
            "src.services.agent.graph.execute_tool", side_effect=mock_execute_tool
        ):
            result = await _execute_single_tool(
                tc, config, {}, operation_context=operation_context
            )

        content = json.loads(result["message"].content)
        assert content["error"] == f"Document {document_id} not found"
        assert result["error_info"]["category"] == "recoverable"
        assert "ingest" in result["error_info"]["suggestion"].lower()
        # Error payloads must not carry LangChain's default status="success" —
        # that mislabeled every failure in traces and status-branching code.
        assert result["message"].status == "error"

    @pytest.mark.asyncio
    async def test_success_message_has_success_status(self):
        from src.services.agent.graph import _execute_single_tool

        async def mock_execute_tool(**kwargs):
            return {"status": "ok", "papers": []}

        tc = {"name": "search_documents", "args": {"query": "test"}, "id": "tc4"}
        config = {"configurable": {"user_id": "u1"}}

        with patch(
            "src.services.agent.graph.execute_tool", side_effect=mock_execute_tool
        ):
            result = await _execute_single_tool(tc, config, {})

        assert result["message"].status == "success"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed", [False, True])
    async def test_tool_span_records_returned_failure_without_payload(
        self, failed: bool
    ) -> None:
        from langsmith import get_current_run_tree, tracing_context

        from src.services.agent._nodes_tools import _execute_single_tool

        payload = (
            {"error": "request timed out at secret-host"} if failed else {"papers": []}
        )
        spans = []

        async def execute(**kwargs):
            spans.append(get_current_run_tree())
            return payload

        with (
            patch("src.services.agent.graph._get_execute_tool", return_value=execute),
            tracing_context(enabled="local"),
        ):
            result = await _execute_single_tool(
                {"name": "search_arxiv", "args": {"query": "test"}, "id": "traced"},
                {"configurable": {}},
                {},
            )

        assert len(spans) == 1
        assert spans[0] is not None
        assert spans[0].error == (
            "search_arxiv returned a tool error" if failed else None
        )
        assert result["execution"]["retry_exhausted"] is failed

    @pytest.mark.asyncio
    async def test_error_count_resets_on_success(self):
        """tool_node should reset error_count to 0 after a successful tool call."""
        from src.services.agent.graph import tool_node

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "search_arxiv", "args": {"query": "test"}, "id": "tc1"}
            ],
        )
        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 2,
            "last_error": "previous error",
            "page_context": {},
            "tool_loop_count": 0,
            "last_error_info": {},
        }

        async def mock_execute_tool(**kwargs):
            return {"papers": [{"id": "1", "title": "Test Paper"}]}

        config = {"configurable": {"user_id": "u1"}}

        with patch(
            "src.services.agent.graph.execute_tool", side_effect=mock_execute_tool
        ):
            result = await tool_node(state, config)

        assert result["error_count"] == 0  # Reset on success
