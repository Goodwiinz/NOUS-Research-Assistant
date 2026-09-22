"""Unit tests for agent feature bug fixes and reliability improvements.

Covers Phase 1 (critical bugs), Phase 2 (reliability), Phase 3 (validation),
and Phase 4 (state schema) fixes from the agent code review.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Phase 1: Critical Bug Fixes
# ---------------------------------------------------------------------------


class TestToolLoopCounter:
    """1.4 — tool_loop_count should increment in tool_node, not llm_node."""

    async def test_llm_node_does_not_increment_tool_loop_count(self):
        """llm_node return should NOT include tool_loop_count."""
        from langchain_core.messages import AIMessage, HumanMessage

        # After T1.1 split, llm_node may route through build_synthesis_llm
        # for synthesis turns (last msg is ToolMessage OR intent="general").
        # Patch both factories so tests work regardless of which branch fires.
        with (
            patch("src.services.agent.graph._build_llm") as mock_build,
            patch("src.services.agent.llm_factory.build_synthesis_llm") as mock_synth,
            patch("src.services.agent.llm_factory.build_lightweight_llm") as mock_light,
        ):
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Hello")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            mock_build.return_value = mock_llm
            mock_synth.return_value = mock_llm
            mock_light.return_value = mock_llm

            from src.services.agent.graph import llm_node

            state = {
                "messages": [HumanMessage(content="test")],
                "page_context": {},
                "retrieved_contexts": [],
                "tool_loop_count": 3,
                "error_count": 0,
                "last_error": "",
                "intent": "",
                "user_memories": [],
            }
            config = {"configurable": {}}

            result = await llm_node(state, config)

            assert "tool_loop_count" not in result
            assert "messages" in result

    async def test_tool_node_increments_tool_loop_count(self):
        """tool_node return should include incremented tool_loop_count."""
        from langchain_core.messages import AIMessage, ToolMessage

        from src.services.agent.graph import tool_node

        # Create a state with an AIMessage that has tool_calls
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}}
            ],
        )

        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "page_context": {},
            "tool_loop_count": 5,
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
            }
        }

        with patch(
            "src.services.agent.graph._execute_single_tool",
            new_callable=AsyncMock,
            return_value={
                "message": ToolMessage(content='{"results": []}', tool_call_id="tc1"),
                "execution": {
                    "id": "tc1",
                    "tool_name": "search_arxiv",
                    "tool_display_name": "Search Arxiv",
                    "args": {"query": "test"},
                    "status": "completed",
                    "result": {"results": []},
                    "duration_ms": 100,
                },
                "error_increment": 0,
                "error_text": "",
            },
        ):
            result = await tool_node(state, config)

        assert result["tool_loop_count"] == 6


class TestMessageSanitizationIndex:
    """1.5 — sanitization should use enumerate, not list.index()."""

    async def test_sanitization_with_duplicate_messages(self):
        """Should not raise ValueError when duplicate AIMessages exist."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        # Two identical AIMessages with tool_calls — list.index() would
        # always return index of the first one for both
        tc_id_1 = "tc-1"
        tc_id_2 = "tc-2"
        ai_msg_1 = AIMessage(
            content="",
            tool_calls=[{"id": tc_id_1, "name": "search_arxiv", "args": {}}],
        )
        ai_msg_2 = AIMessage(
            content="",
            tool_calls=[{"id": tc_id_2, "name": "search_arxiv", "args": {}}],
        )
        tool_msg_1 = ToolMessage(content='{"ok": true}', tool_call_id=tc_id_1)

        state = {
            "messages": [
                HumanMessage(content="test"),
                ai_msg_1,
                tool_msg_1,
                ai_msg_2,
                # No ToolMessage for ai_msg_2 — should get placeholder
            ],
            "page_context": {},
            "retrieved_contexts": [],
            "tool_loop_count": 0,
            "error_count": 0,
            "last_error": "",
            "intent": "",
            "user_memories": [],
        }

        # After T1.1 split, llm_node may route through build_synthesis_llm
        # for synthesis turns (last msg is ToolMessage OR intent="general").
        # Patch both factories so tests work regardless of which branch fires.
        with (
            patch("src.services.agent.graph._build_llm") as mock_build,
            patch("src.services.agent.llm_factory.build_synthesis_llm") as mock_synth,
            patch("src.services.agent.llm_factory.build_lightweight_llm") as mock_light,
        ):
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Done")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            mock_build.return_value = mock_llm
            mock_synth.return_value = mock_llm
            mock_light.return_value = mock_llm

            from src.services.agent.graph import llm_node

            # Should NOT raise ValueError
            result = await llm_node(state, {"configurable": {}})
            assert "messages" in result


class TestGraphInterruptImport:
    """1.6 — GraphInterrupt should be imported and used with isinstance."""

    def test_graphinterrupt_is_imported(self):
        """execute.py should import GraphInterrupt from langgraph.errors."""
        from src.api.agent import execute

        assert hasattr(execute, "GraphInterrupt")


# ---------------------------------------------------------------------------
# Phase 2: Reliability Improvements
# ---------------------------------------------------------------------------


class TestSubgraphErrorCountCheck:
    """2.1/2.2 — Subgraph should_continue must check error_count."""

    def test_research_should_continue_stops_on_high_errors(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.research_agent import research_should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "tool_loop_count": 1,
            "error_count": 3,
        }
        assert research_should_continue(state) == "research_reflection_gate"

    def test_writing_should_continue_stops_on_high_errors(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.writing_agent import writing_should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "create_draft", "args": {}}],
                )
            ],
            "tool_loop_count": 1,
            "error_count": 3,
        }
        assert writing_should_continue(state) == "writing_reflection_gate"

    def test_data_should_continue_stops_on_high_errors(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.data_agent import data_should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "extract_entities", "args": {}}],
                )
            ],
            "tool_loop_count": 1,
            "error_count": 3,
        }
        assert data_should_continue(state) == "data_reflection_gate"

    def test_research_should_continue_proceeds_when_errors_low(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.research_agent import research_should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "tool_loop_count": 1,
            "error_count": 2,
        }
        assert research_should_continue(state) == "research_tool_node"

    def test_writing_should_continue_routes_destructive_to_interrupt(self):
        """create_project_note / create_draft must hit the writing
        interrupt gate before the tool runs — otherwise the destructive
        write happens silently when intent routes us into the writing
        subgraph (the top-level interrupt_node only fires for the main
        graph)."""
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.writing_agent import writing_should_continue

        for tool_name in ("create_project_note", "create_draft"):
            state = {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "tc1", "name": tool_name, "args": {}}],
                    )
                ],
                "tool_loop_count": 1,
                "error_count": 0,
            }
            assert (
                writing_should_continue(state) == "writing_interrupt_node"
            ), f"{tool_name} must be gated by writing_interrupt_node"

    def test_writing_should_continue_skips_interrupt_for_read_tools(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.subgraphs.writing_agent import writing_should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "tc1", "name": "summarize_document", "args": {}}
                    ],
                )
            ],
            "tool_loop_count": 1,
            "error_count": 0,
        }
        assert writing_should_continue(state) == "writing_tool_node"


class TestSubgraphLlmNodeNoLoopIncrement:
    """2.1 — Subgraph LLM nodes should NOT increment tool_loop_count."""

    async def test_research_llm_node_no_loop_increment(self):
        from langchain_core.messages import AIMessage, HumanMessage

        # After T1.1 split, llm_node may route through build_synthesis_llm
        # for synthesis turns (last msg is ToolMessage OR intent="general").
        # Patch both factories so tests work regardless of which branch fires.
        with (
            patch("src.services.agent.graph._build_llm") as mock_build,
            patch("src.services.agent.llm_factory.build_synthesis_llm") as mock_synth,
            patch("src.services.agent.llm_factory.build_lightweight_llm") as mock_light,
        ):
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Research result")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            mock_build.return_value = mock_llm
            mock_synth.return_value = mock_llm
            mock_light.return_value = mock_llm

            from src.services.agent.subgraphs.research_agent import research_llm_node

            state = {
                "messages": [HumanMessage(content="find papers")],
                "tool_loop_count": 2,
            }
            result = await research_llm_node(state, {"configurable": {}})
            assert "tool_loop_count" not in result

    async def test_writing_llm_node_no_loop_increment(self):
        from langchain_core.messages import AIMessage, HumanMessage

        # After T1.1 split, llm_node may route through build_synthesis_llm
        # for synthesis turns (last msg is ToolMessage OR intent="general").
        # Patch both factories so tests work regardless of which branch fires.
        with (
            patch("src.services.agent.graph._build_llm") as mock_build,
            patch("src.services.agent.llm_factory.build_synthesis_llm") as mock_synth,
            patch("src.services.agent.llm_factory.build_lightweight_llm") as mock_light,
        ):
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Writing result")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            mock_build.return_value = mock_llm
            mock_synth.return_value = mock_llm
            mock_light.return_value = mock_llm

            from src.services.agent.subgraphs.writing_agent import writing_llm_node

            state = {
                "messages": [HumanMessage(content="write summary")],
                "tool_loop_count": 2,
            }
            result = await writing_llm_node(state, {"configurable": {}})
            assert "tool_loop_count" not in result

    async def test_data_llm_node_no_loop_increment(self):
        from langchain_core.messages import AIMessage, HumanMessage

        # After T1.1 split, llm_node may route through build_synthesis_llm
        # for synthesis turns (last msg is ToolMessage OR intent="general").
        # Patch both factories so tests work regardless of which branch fires.
        with (
            patch("src.services.agent.graph._build_llm") as mock_build,
            patch("src.services.agent.llm_factory.build_synthesis_llm") as mock_synth,
            patch("src.services.agent.llm_factory.build_lightweight_llm") as mock_light,
        ):
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Data result")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            mock_build.return_value = mock_llm
            mock_synth.return_value = mock_llm
            mock_light.return_value = mock_llm

            from src.services.agent.subgraphs.data_agent import data_llm_node

            state = {
                "messages": [HumanMessage(content="extract entities")],
                "tool_loop_count": 2,
            }
            result = await data_llm_node(state, {"configurable": {}})
            assert "tool_loop_count" not in result


class TestMutableDefaultConfig:
    """2.3 — Tool functions should not use mutable default args."""

    def test_all_tools_accept_none_config(self):
        """All tools should have config default of None, not {}."""
        import inspect

        from src.services.agent.tools import ALL_TOOLS

        for tool_fn in ALL_TOOLS:
            # StructuredTool stores the original async fn in .coroutine
            func = getattr(tool_fn, "coroutine", None) or getattr(tool_fn, "func", None)
            assert func is not None, f"{tool_fn.name}: cannot find underlying function"
            sig = inspect.signature(func)
            param = sig.parameters.get("config")
            assert param is not None, f"{tool_fn.name} missing config param"
            assert param.default is None, (
                f"{tool_fn.name} should have config default of None, "
                f"got {param.default!r}"
            )


class TestSafeJsonLoads:
    """2.4 — _safe_json_loads should handle malformed input."""

    def test_valid_json(self):
        from src.services.agent.graph import _safe_json_loads

        result = _safe_json_loads('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json_returns_raw(self):
        from src.services.agent.graph import _safe_json_loads

        result = _safe_json_loads("not valid json")
        assert result == {"raw": "not valid json"}

    def test_none_input_returns_raw(self):
        from src.services.agent.graph import _safe_json_loads

        result = _safe_json_loads(None)
        assert result == {"raw": None}


class TestGatherExceptionToolMessages:
    """2.5 — asyncio.gather exceptions should still produce ToolMessages."""

    async def test_exception_produces_error_tool_message(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import tool_node

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}},
                {"id": "tc2", "name": "search_documents", "args": {"query": "test"}},
            ],
        )

        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "page_context": {},
            "tool_loop_count": 0,
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
            }
        }

        # First tool succeeds, second raises an exception
        from langchain_core.messages import ToolMessage

        async def mock_execute(tc, cfg, ctx):
            if tc["id"] == "tc1":
                return {
                    "message": ToolMessage(
                        content='{"results": []}', tool_call_id="tc1"
                    ),
                    "execution": {
                        "id": "tc1",
                        "tool_name": "search_arxiv",
                        "tool_display_name": "Search Arxiv",
                        "args": {},
                        "status": "completed",
                        "result": {},
                        "duration_ms": 50,
                    },
                    "error_increment": 0,
                    "error_text": "",
                }
            raise RuntimeError("Connection failed")

        with patch(
            "src.services.agent._nodes_tools._execute_single_tool",
            side_effect=mock_execute,
        ):
            result = await tool_node(state, config)

        # Should have 2 ToolMessages — one success, one error
        assert len(result["messages"]) == 2
        # Error message should contain the error
        error_msg = result["messages"][1]
        assert error_msg.tool_call_id == "tc2"
        content = json.loads(error_msg.content)
        assert "error" in content
        assert "Connection failed" in content["error"]
        # Mixed-success batch: counter does NOT reset (only a fully-clean batch
        # resets it so a "1 success + N failures" loop cannot suppress MAX_ERRORS).
        assert result["error_count"] == 1


class TestCheckpointerLock:
    """2.6 — get_checkpointer should use asyncio.Lock for thread safety."""

    def test_lock_exists(self):
        from src.services.agent import checkpointer

        assert hasattr(checkpointer, "_checkpointer_lock")
        assert isinstance(checkpointer._checkpointer_lock, asyncio.Lock)

    def test_memory_store_lock_exists(self):
        from src.services.agent import memory

        assert hasattr(memory, "_store_lock")
        assert isinstance(memory._store_lock, asyncio.Lock)


class TestJobOwnership:
    """1.3 — Job endpoints should enforce user ownership."""

    def test_set_job_stores_user_id(self):
        from src.api.agent.execute import _get_job, _set_job

        job_id = str(uuid4())
        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                "user_id": "user-abc",
            },
        )
        job = _get_job(job_id)
        assert job is not None
        assert job["user_id"] == "user-abc"

    def test_set_job_stores_request(self):
        from src.api.agent.execute import _get_job, _set_job

        job_id = str(uuid4())
        request_data = {
            "messages": [{"role": "user", "content": "hello"}],
            "page_context": {"type": "unknown"},
            "model": "model-router",
            "use_rag": True,
            "max_context_docs": 5,
        }
        _set_job(
            job_id,
            {
                "status": "running",
                "tool_executions": [],
                "user_id": "user-abc",
                "request": request_data,
            },
        )
        job = _get_job(job_id)
        assert job["request"]["messages"][0]["content"] == "hello"


# ---------------------------------------------------------------------------
# Phase 4: State Schema
# ---------------------------------------------------------------------------


class TestAgentStateDocstring:
    """4.1 — AgentState should have a docstring."""

    def test_agent_state_has_docstring(self):
        from src.services.agent.state import AgentState

        assert AgentState.__doc__ is not None
        assert "initial state" in AgentState.__doc__.lower()


# ---------------------------------------------------------------------------
# should_continue edge cases
# ---------------------------------------------------------------------------


class TestFilteredToolNode:
    """2.1 — Subgraph tool nodes should filter out-of-scope tool calls."""

    async def test_filtered_tool_node_skips_out_of_scope(self):
        """Filtered tool node should skip tools not in the allowed set."""
        from langchain_core.messages import AIMessage, ToolMessage

        from src.services.agent.graph import make_filtered_tool_node

        filtered_node = make_filtered_tool_node({"search_arxiv"})

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}},
                {"id": "tc2", "name": "create_draft", "args": {"themes": ["AI"]}},
            ],
        )

        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "page_context": {},
            "tool_loop_count": 0,
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
            }
        }

        with patch(
            "src.services.agent.graph._execute_single_tool",
            new_callable=AsyncMock,
            return_value={
                "message": ToolMessage(content='{"results": []}', tool_call_id="tc1"),
                "execution": {
                    "id": "tc1",
                    "tool_name": "search_arxiv",
                    "tool_display_name": "Search Arxiv",
                    "args": {},
                    "status": "completed",
                    "result": {},
                    "duration_ms": 50,
                },
                "error_increment": 0,
                "error_text": "",
            },
        ):
            result = await filtered_node(state, config)

        # Should have 2 messages: 1 real result + 1 skip message
        assert len(result["messages"]) == 2
        # The skipped tool should have an error message
        skip_msg = [m for m in result["messages"] if m.tool_call_id == "tc2"][0]
        assert "not available" in skip_msg.content

    async def test_filtered_tool_node_all_skipped(self):
        """When all tool calls are out-of-scope, none should execute."""
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import make_filtered_tool_node

        filtered_node = make_filtered_tool_node({"search_arxiv"})

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "create_draft", "args": {"themes": ["AI"]}},
            ],
        )

        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "page_context": {},
            "tool_loop_count": 0,
        }

        config = {"configurable": {}}

        result = await filtered_node(state, config)

        assert len(result["messages"]) == 1
        assert "not available" in result["messages"][0].content
        assert result["tool_loop_count"] == 1

    async def test_filtered_tool_node_all_allowed(self):
        """When all tool calls are in-scope, all should execute normally."""
        from langchain_core.messages import AIMessage, ToolMessage

        from src.services.agent.graph import make_filtered_tool_node

        filtered_node = make_filtered_tool_node({"search_arxiv", "search_documents"})

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}},
                {"id": "tc2", "name": "search_documents", "args": {"query": "test"}},
            ],
        )

        state = {
            "messages": [ai_msg],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "page_context": {},
            "tool_loop_count": 0,
        }

        config = {
            "configurable": {
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
            }
        }

        async def mock_execute(tc, cfg, ctx):
            return {
                "message": ToolMessage(
                    content='{"results": []}', tool_call_id=tc["id"]
                ),
                "execution": {
                    "id": tc["id"],
                    "tool_name": tc["name"],
                    "tool_display_name": tc["name"].replace("_", " ").title(),
                    "args": tc["args"],
                    "status": "completed",
                    "result": {},
                    "duration_ms": 50,
                },
                "error_increment": 0,
                "error_text": "",
            }

        with patch(
            "src.services.agent.graph._execute_single_tool",
            side_effect=mock_execute,
        ):
            result = await filtered_node(state, config)

        # All 2 tools allowed, no skips
        assert len(result["messages"]) == 2
        for msg in result["messages"]:
            assert "not available" not in msg.content


class TestShouldContinue:
    """Verify should_continue routing logic with fixed tool_loop_count."""

    def test_stops_on_max_errors(self):
        from src.services.agent.graph import MAX_ERRORS, should_continue

        state = {
            "messages": [],
            "error_count": MAX_ERRORS,
            "last_error": "timeout",
            "tool_loop_count": 0,
        }
        assert should_continue(state) == "reflection_gate"

    def test_routes_to_tool_node_for_non_destructive(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "error_count": 0,
            "tool_loop_count": 0,
        }
        assert should_continue(state) == "tool_node"

    def test_routes_to_interrupt_for_destructive(self):
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "tc1",
                            "name": "ingest_arxiv_papers",
                            "args": {},
                        }
                    ],
                )
            ],
            "error_count": 0,
            "tool_loop_count": 0,
        }
        assert should_continue(state) == "interrupt_node"

    def test_general_ceiling_covers_capability_complete_flow(self):
        """MAX_TOOL_LOOPS must fit the longest legitimate general-path flow.

        agent-project-management-v1 needs 5 sequential calls: create_project →
        search_documents (resolve a doc by title) → add_document_to_project →
        create_project_note → list_project_documents. At 4 the trailing read
        was starved and force_synthesis_node fabricated it. Do not re-lower
        below 5 without re-checking that flow.
        """
        from src.services.agent.graph import MAX_TOOL_LOOPS

        assert MAX_TOOL_LOOPS >= 5

    def test_routes_to_force_synthesis_at_max_tool_loops(self):
        """At ceiling with unanswered tool_calls → force_synthesis_node."""
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import MAX_TOOL_LOOPS, should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "error_count": 0,
            "tool_loop_count": MAX_TOOL_LOOPS,
        }
        assert should_continue(state) == "force_synthesis_node"

    def test_no_loop_back_into_force_synthesis(self):
        """After force_synthesis fired, defective tool_calls route to reflection."""
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import MAX_TOOL_LOOPS, should_continue

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "tc1", "name": "search_arxiv", "args": {}}],
                )
            ],
            "error_count": 0,
            "tool_loop_count": MAX_TOOL_LOOPS + 1,
            "_force_synthesis_fired": True,
        }
        assert should_continue(state) == "reflection_gate"

    def test_no_tool_calls_routes_to_reflection(self):
        """Plain AIMessage with no tool_calls always exits via reflection."""
        from langchain_core.messages import AIMessage

        from src.services.agent.graph import should_continue

        state = {
            "messages": [AIMessage(content="final answer")],
            "error_count": 0,
            "tool_loop_count": 0,
        }
        assert should_continue(state) == "reflection_gate"
