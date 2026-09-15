"""LangGraph partial execution tests.

Uses MemorySaver checkpointer + update_state to test node-to-node
flows without requiring real LLM calls or external services.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_initial_state(user_msg: str = "find papers on transformers") -> dict:
    """Create a minimal valid AgentState dict."""
    return {
        "messages": [HumanMessage(content=user_msg)],
        "page_context": {"type": "unknown"},
        "retrieved_contexts": [],
        "tool_executions": [],
        "thread_id": "",
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "pending_confirmation": {},
        "user_confirmed": False,
        "intent": "",
        "user_memories": [],
    }


def _make_config(thread_id: str | None = None) -> dict:
    # Ids-only configurable (audit B8) — never a live session / ORM user.
    return {
        "configurable": {
            "thread_id": thread_id or str(uuid4()),
            "user_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "page_context": {"type": "unknown"},
        }
    }


async def _capture_system_prompt(node_fn, state, config) -> str:
    """Run ``node_fn`` with a stubbed LLM and return its system prompt content.

    Used by the prompt-coverage regression tests to verify SHARED_AGENT_RULES
    is embedded in every llm-node prompt path.
    """
    captured: dict = {"messages": None}

    async def fake_ainvoke(messages, config=None):
        captured["messages"] = messages
        return AIMessage(content="ok")

    fake_with_tools = MagicMock()
    fake_with_tools.ainvoke = fake_ainvoke
    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_with_tools

    # Subgraph LLM nodes (research/writing/data) build LLMs via
    # llm_factory.build_lightweight_llm or build_synthesis_llm — not the
    # graph._build_llm helper used by the main llm_node — so patch every
    # entry point a node might import.
    with (
        patch("src.services.agent.graph._build_llm", return_value=fake_llm),
        patch(
            "src.services.agent.llm_factory.build_lightweight_llm",
            return_value=fake_llm,
        ),
        patch(
            "src.services.agent.llm_factory.build_synthesis_llm",
            return_value=fake_llm,
        ),
    ):
        await node_fn(state, config)

    assert captured["messages"], "node did not call the LLM"
    return captured["messages"][0].content


def _assert_shared_rules_present(system_text: str) -> None:
    """Assert every section of SHARED_AGENT_RULES is present in ``system_text``."""
    # Retry rule (PR #394)
    assert "Handling retry follow-ups" in system_text
    # Project reuse rule (PR #397)
    assert "Reusing project IDs from conversation history" in system_text
    assert "reuse its project_id" in system_text
    # Honesty rule (PR #397)
    assert "Honest tool-call reporting" in system_text
    assert "Do not invent troubleshooting steps" in system_text
    # Query derivation rule (this PR — bug #7)
    assert "Deriving search queries from active context" in system_text
    assert "Do not use arXiv paper IDs" in system_text
    # Document coreference rule (9d5709f — resolves "it"/"that paper" to a UUID)
    assert "Reusing document IDs from conversation history" in system_text
    assert "Do not ask the user for the document_id" in system_text
    # Named-source evidence must carry only UUIDs selected by title lookup;
    # broad semantic substitutes must never be presented as that source.
    assert "Named local source retrieval" in system_text
    assert "document_ids" in system_text
    assert "never substitute broad retrieval" in system_text
    assert "title-search metadata as evidence" in system_text
    # Always-reply rule (b96bd89 — prevents silent blank after tool success)
    assert "Always reply after a tool call" in system_text
    assert "do not return empty content" in system_text
    # Save-target resolution (this PR — writing_draft asked "which project?"
    # instead of calling list_projects, 5 dev runs out of 5)
    assert "Resolving a save target" in system_text
    assert "list_projects FIRST" in system_text


# ---------------------------------------------------------------------------
# Individual node tests via compiled_graph.nodes[...]
# ---------------------------------------------------------------------------


class TestIndividualNodes:
    """Test individual graph nodes called as plain async functions."""

    async def test_intent_classifier_research(self):
        """intent_classifier_node should classify 'search arxiv' as research."""
        from src.services.agent.graph import intent_classifier_node

        result = await intent_classifier_node(
            _make_initial_state("search arxiv for transformer papers"),
            _make_config(),
        )
        assert result["intent"] == "research"

    async def test_intent_classifier_writing(self):
        """intent_classifier_node should classify 'write a summary' as writing."""
        from src.services.agent.graph import intent_classifier_node

        result = await intent_classifier_node(
            _make_initial_state("write a summary of the paper"),
            _make_config(),
        )
        assert result["intent"] == "writing"

    async def test_intent_classifier_knowledge_graph(self):
        """intent_classifier_node should classify 'extract entities' as knowledge_graph."""
        from src.services.agent.graph import intent_classifier_node

        result = await intent_classifier_node(
            _make_initial_state("extract entities from the document"),
            _make_config(),
        )
        assert result["intent"] == "knowledge_graph"

    async def test_intent_classifier_general(self):
        """intent_classifier_node should return 'general' for ambiguous queries."""
        from src.services.agent.graph import intent_classifier_node

        result = await intent_classifier_node(
            _make_initial_state("hello, how are you?"),
            _make_config(),
        )
        assert result["intent"] == "general"

    async def test_intent_classifier_writing_with_paper_keyword(self):
        """'Write a summary of the findings in the paper' should be writing, not research."""
        from src.services.agent.graph import intent_classifier_node

        result = await intent_classifier_node(
            _make_initial_state("write a summary of the key findings in the paper"),
            _make_config(),
        )
        assert result["intent"] == "writing"

    async def test_llm_node_system_prompt_contains_shared_rules(self):
        """llm_node's system prompt must embed every section of SHARED_AGENT_RULES.

        Regression test: prevents the prompt from being trimmed or rephrased away from
        the rules that prevent duplicate-project creation, lost-project context,
        hallucinated tool completions, and arXiv-ID-as-search-query mistakes.
        """
        from src.services.agent.graph import llm_node

        system_text = await _capture_system_prompt(
            llm_node, _make_initial_state("hi"), _make_config()
        )
        _assert_shared_rules_present(system_text)

    async def test_research_subgraph_prompt_contains_shared_rules(self):
        """research_llm_node must embed SHARED_AGENT_RULES — bug #7 fix."""
        from src.services.agent.subgraphs.research_agent import research_llm_node

        system_text = await _capture_system_prompt(
            research_llm_node, _make_initial_state("find papers on RAG"), _make_config()
        )
        _assert_shared_rules_present(system_text)

    async def test_writing_subgraph_prompt_contains_shared_rules(self):
        """writing_llm_node must embed SHARED_AGENT_RULES."""
        from src.services.agent.subgraphs.writing_agent import writing_llm_node

        system_text = await _capture_system_prompt(
            writing_llm_node, _make_initial_state("draft a review"), _make_config()
        )
        _assert_shared_rules_present(system_text)

    async def test_data_subgraph_prompt_contains_shared_rules(self):
        """data_llm_node must embed SHARED_AGENT_RULES."""
        from src.services.agent.subgraphs.data_agent import data_llm_node

        system_text = await _capture_system_prompt(
            data_llm_node, _make_initial_state("extract entities"), _make_config()
        )
        _assert_shared_rules_present(system_text)

    async def test_rag_node_without_user(self):
        """rag_node should return empty contexts when no current_user."""
        from src.services.agent.graph import rag_node

        config = {"configurable": {"thread_id": str(uuid4())}}
        result = await rag_node(
            _make_initial_state("test query"),
            config,
        )
        assert result["retrieved_contexts"] == []

    async def test_rag_node_promotes_chat_context_to_project_when_uuid_in_text(self):
        """When the user's message names a project UUID the caller owns,
        rag_node must force page_context.type='project' so llm_node tells the
        LLM about the active project. Previously type='chat' was preserved, so
        the LLM ignored the project_id and replied "I'm not using any project
        yet".

        A text-extracted id is ownership-gated (audit M3) before promotion —
        mock the check as owned so this test still exercises promotion itself,
        not the gate (see test_rag_scope_and_sanitizer.py for the gate).
        """
        from src.services.agent.graph import rag_node

        config = {
            "configurable": {
                "thread_id": str(uuid4()),
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "search_fn": AsyncMock(return_value=[]),
            }
        }

        state = _make_initial_state(
            "use RAG Research — id: 88e10696-bcc1-4921-b1f3-773b571365bd"
        )
        state["page_context"] = {"type": "chat"}

        with patch(
            "src.services.agent._nodes_rag._user_owns_project",
            new=AsyncMock(return_value=True),
        ):
            result = await rag_node(state, config)

        assert result["current_project_id"] == "88e10696-bcc1-4921-b1f3-773b571365bd"
        assert result["page_context"]["type"] == "project"
        assert (
            result["page_context"]["project_id"]
            == "88e10696-bcc1-4921-b1f3-773b571365bd"
        )

    async def test_rag_node_keeps_carried_project_id_across_turns(self):
        """If a previous turn already resolved current_project_id, the next
        turn should keep type='project' even when the new message has no UUID
        and the CLI still sends type='chat'."""
        from src.services.agent.graph import rag_node

        config = {
            "configurable": {
                "thread_id": str(uuid4()),
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "search_fn": AsyncMock(return_value=[]),
            }
        }

        state = _make_initial_state("which project that are you using")
        state["page_context"] = {"type": "chat"}
        state["current_project_id"] = "88e10696-bcc1-4921-b1f3-773b571365bd"

        result = await rag_node(state, config)

        assert result["page_context"]["type"] == "project"
        assert (
            result["page_context"]["project_id"]
            == "88e10696-bcc1-4921-b1f3-773b571365bd"
        )

    async def test_rag_node_with_injected_search(self):
        """rag_node should use injected search_fn when provided."""
        from src.services.agent.graph import rag_node

        async def mock_search(query: str, user_id: str):
            return [
                {
                    "document_id": "doc-123",
                    "title": "Test Document",
                    "content": f"Results for: {query}",
                    "score": 0.95,
                }
            ]

        config = {
            "configurable": {
                "thread_id": str(uuid4()),
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "search_fn": mock_search,
            }
        }

        result = await rag_node(
            _make_initial_state("find papers about test query"),
            config,
        )
        assert len(result["retrieved_contexts"]) == 1
        assert result["retrieved_contexts"][0]["title"] == "Test Document"
        assert (
            "find papers about test query" in result["retrieved_contexts"][0]["content"]
        )

    async def test_rag_node_sanitizes_injected_contexts_for_sse(self):
        """Injected contexts must match the normal retrieval boundary."""
        from src.services.agent.graph import rag_node

        async def mock_search(query: str, user_id: str):
            return [
                {
                    "document_id": "doc-1",
                    "title": "Numeric score",
                    "content": "x" * 4000,
                    "score": "0.95",
                },
                {
                    "document_id": "doc-2",
                    "title": "Invalid score",
                    "content": "safe",
                    "score": "not-a-score",
                },
                {
                    "document_id": "doc-3",
                    "title": "Non-finite score",
                    "content": "safe",
                    "score": float("nan"),
                },
                {"document_id": "doc-4", "title": "Missing content", "score": 0.5},
                "not a context item",
            ]

        config = {
            "configurable": {
                "thread_id": str(uuid4()),
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "search_fn": mock_search,
            }
        }

        result = await rag_node(
            _make_initial_state("find papers about test query"),
            config,
        )

        contexts = result["retrieved_contexts"]
        assert len(contexts) == 3
        assert contexts[0]["score"] == 0.95
        assert len(contexts[0]["content"]) == 3000
        assert contexts[1]["score"] == 0.0
        assert contexts[2]["score"] == 0.0

    async def test_memory_retrieval_node_without_user(self):
        """memory_retrieval_node should return empty when no user in config."""
        from src.services.agent.graph import memory_retrieval_node

        config = {"configurable": {"thread_id": str(uuid4())}}
        result = await memory_retrieval_node(
            _make_initial_state("test"),
            config,
        )
        assert result["user_memories"] == []

    async def test_preprocessing_node_merges_all_results(self):
        """preprocessing_node should merge RAG, intent, and memory results."""
        from src.services.agent.graph import preprocessing_node

        async def mock_search(query: str, user_id: str):
            return [{"document_id": "d1", "title": "T", "content": "c", "score": 0.9}]

        config = {
            "configurable": {
                "thread_id": str(uuid4()),
                "user_id": str(uuid4()),
                "organization_id": str(uuid4()),
                "page_context": {"type": "unknown"},
                "search_fn": mock_search,
            }
        }

        result = await preprocessing_node(
            _make_initial_state("search arxiv for transformers"),
            config,
        )
        assert "retrieved_contexts" in result
        assert "intent" in result
        assert "user_memories" in result
        assert result["intent"] == "research"

    async def test_preprocessing_node_reraises_cancelled_error(self):
        """A CancelledError from a parallel subtask must propagate, not be
        swallowed into a TypeError via merged.update(<exception>)."""
        import asyncio

        from src.services.agent.graph import preprocessing_node

        async def _cancel(*_a, **_k):
            raise asyncio.CancelledError()

        with patch("src.services.agent._nodes_classify.rag_node", new=_cancel):
            with pytest.raises(asyncio.CancelledError):
                await preprocessing_node(
                    _make_initial_state("find papers on transformers"),
                    _make_config(),
                )

    async def test_memory_save_node_without_user(self):
        """memory_save_node should return empty dict when no user."""
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())

        config = {"configurable": {"thread_id": str(uuid4())}}
        result = await graph.nodes["memory_save_node"].ainvoke(
            _make_initial_state("test"),
            config,
        )
        assert result == {}

    async def test_tool_node_no_tool_calls(self):
        """tool_node should return empty lists when no tool_calls on last message."""
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())

        state = _make_initial_state("hello")
        # Add an AIMessage without tool_calls
        state["messages"].append(AIMessage(content="Hello there!"))

        result = await graph.nodes["tool_node"].ainvoke(
            state,
            _make_config(),
        )
        assert result["messages"] == []
        assert result["tool_executions"] == []

    async def test_interrupt_node_no_destructive(self):
        """interrupt_node should pass through when no destructive tools."""
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())

        state = _make_initial_state("search")
        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}}
                ],
            )
        )

        result = await graph.nodes["interrupt_node"].ainvoke(
            state,
            _make_config(),
        )
        assert result.get("user_confirmed") is True
        assert result.get("pending_confirmation") == {}


# ---------------------------------------------------------------------------
# Partial execution tests via update_state + invoke(None)
# ---------------------------------------------------------------------------


class TestPartialExecution:
    """Test partial graph execution via update_state + invoke(None)."""

    async def test_intent_routing_research_path(self):
        """After preprocessing_node, research intent should route to research_subgraph."""
        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Simulate state as if preprocessing_node just completed with research intent
        state_after_preprocessing = _make_initial_state("search arxiv for transformers")
        state_after_preprocessing["retrieved_contexts"] = [
            {
                "document_id": "d1",
                "title": "Attention Is All You Need",
                "content": "...",
                "score": 0.95,
            }
        ]
        state_after_preprocessing["intent"] = "research"
        state_after_preprocessing["intent_confidence"] = 0.9
        state_after_preprocessing["user_memories"] = []

        graph.update_state(
            config,
            values=state_after_preprocessing,
            as_node="preprocessing_node",
        )

        snapshot = await graph.aget_state(config)
        assert snapshot is not None
        # Verify next node is research_subgraph
        assert "research_subgraph" in snapshot.next

    async def test_intent_classification_sets_correct_intent(self):
        """preprocessing_node should set intent in state correctly."""
        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Simulate: preprocessing_node completed with writing intent
        state = _make_initial_state("write a literature review draft")
        state["intent"] = "writing"
        state["intent_confidence"] = 0.85
        state["retrieved_contexts"] = []
        state["user_memories"] = []

        graph.update_state(config, values=state, as_node="preprocessing_node")

        # Verify the intent was persisted in the checkpoint without running the graph
        snapshot = await graph.aget_state(config)
        assert snapshot.values.get("intent") == "writing"
        # Next node should be writing_subgraph (route_by_intent maps writing → writing_subgraph)
        assert "writing_subgraph" in snapshot.next

    async def test_tool_node_executes_and_increments_loop_count(self):
        """tool_node should execute tools and increment tool_loop_count."""
        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Simulate: LLM produced a tool call, state is at tool_node
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "search_arxiv",
                    "args": {"query": "attention mechanism"},
                }
            ],
        )
        state = _make_initial_state("find papers")
        state["messages"].append(ai_msg)
        state["intent"] = "general"
        state["tool_loop_count"] = 0

        # Mock execute_tool to avoid real API calls
        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"results": [], "total": 0},
        ):
            graph.update_state(config, values=state, as_node="llm_node")

            # Run tool_node, interrupt before next llm_node
            result = await graph.ainvoke(
                None,
                config=config,
                interrupt_before=["llm_node"],
            )

        snapshot = await graph.aget_state(config)
        # tool_loop_count should have been incremented by tool_node
        assert snapshot.values.get("tool_loop_count", 0) >= 1

    async def test_error_count_terminates_graph(self):
        """Graph should terminate when error_count >= MAX_ERRORS."""
        from src.services.agent.graph import MAX_ERRORS, compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Simulate: LLM returned with tool_calls but we've hit max errors
        ai_msg = AIMessage(
            content="Let me try again",
            tool_calls=[
                {"id": "tc1", "name": "search_arxiv", "args": {"query": "test"}}
            ],
        )
        state = _make_initial_state("test")
        state["messages"].append(ai_msg)
        state["intent"] = "general"
        state["error_count"] = MAX_ERRORS  # At max errors
        state["last_error"] = "repeated failures"

        graph.update_state(config, values=state, as_node="llm_node")

        # Should route to memory_save_node -> END instead of tool_node
        # This should complete without hanging
        result = await graph.ainvoke(None, config=config)
        assert result is not None


# ---------------------------------------------------------------------------
# Graph structure tests
# ---------------------------------------------------------------------------


class TestGraphStructure:
    """Verify the compiled graph has the expected structure."""

    def test_graph_has_all_expected_nodes(self):
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())

        expected_nodes = {
            "preprocessing_node",
            "llm_node",
            "tool_node",
            "interrupt_node",
            "memory_save_node",
            "research_subgraph",
            "writing_subgraph",
            "data_subgraph",
        }
        actual_nodes = set(graph.nodes.keys()) - {"__start__", "__end__"}
        assert expected_nodes.issubset(
            actual_nodes
        ), f"Missing nodes: {expected_nodes - actual_nodes}"
        # Sequential preprocessing nodes should not be top-level graph nodes
        assert "rag_node" not in actual_nodes
        assert "intent_classifier_node" not in actual_nodes
        assert "memory_retrieval_node" not in actual_nodes

    def test_graph_entry_point_is_preprocessing_node(self):
        """The first node after __start__ should be preprocessing_node."""
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())
        mermaid = graph.get_graph().draw_mermaid()
        assert "__start__" in mermaid
        assert "preprocessing_node" in mermaid

    def test_graph_mermaid_output(self):
        """Graph should produce valid Mermaid diagram."""
        from src.services.agent.graph import compile_agent_graph

        graph = compile_agent_graph(checkpointer=MemorySaver())
        mermaid = graph.get_graph().draw_mermaid()
        assert "preprocessing_node" in mermaid
        assert "llm_node" in mermaid
        assert "tool_node" in mermaid


# ---------------------------------------------------------------------------
# Human-in-the-loop (interrupt → confirm → resume) E2E tests
# ---------------------------------------------------------------------------


class TestHumanInTheLoopFlow:
    """Test the full interrupt → confirm → resume flow.

    In LangGraph >=1.x, ``interrupt()`` does not raise ``GraphInterrupt`` from
    ``ainvoke``.  Instead ``ainvoke`` returns the state dict containing an
    ``__interrupt__`` key and the graph pauses.  Interrupt details are available
    via ``aget_state().tasks[0].interrupts``.  Resuming is done by passing
    ``Command(resume=...)`` to a subsequent ``ainvoke`` call.
    """

    async def test_destructive_tool_triggers_interrupt(self):
        """A destructive tool call should pause the graph with an interrupt."""
        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Create state where LLM has requested a destructive tool (ingest_arxiv_papers)
        ai_msg = AIMessage(
            content="I'll ingest those papers for you.",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "ingest_arxiv_papers",
                    "args": {"paper_ids": ["2401.12345"]},
                }
            ],
        )
        state = _make_initial_state("ingest this paper 2401.12345")
        state["messages"].append(ai_msg)
        state["intent"] = "general"

        # Set state as if llm_node just ran — should_continue will route to interrupt_node
        graph.update_state(config, values=state, as_node="llm_node")

        # Invoking should return state with __interrupt__ (graph paused)
        result = await graph.ainvoke(None, config=config)
        assert "__interrupt__" in result, "Graph should pause with an __interrupt__ key"

        # Verify the graph is paused at interrupt_node
        snapshot = await graph.aget_state(config)
        assert (
            "interrupt_node" in snapshot.next
        ), "Graph should be paused at interrupt_node"

        # The interrupt should contain confirmation details
        assert len(snapshot.tasks) > 0
        task = snapshot.tasks[0]
        assert hasattr(task, "interrupts") and len(task.interrupts) > 0
        confirmation = task.interrupts[0].value
        assert "tools" in confirmation
        assert any(t["name"] == "ingest_arxiv_papers" for t in confirmation["tools"])
        assert "message" in confirmation

    async def test_confirmed_interrupt_resumes_execution(self):
        """After confirming, the graph should resume and execute the tool."""
        from langgraph.types import Command

        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Setup: LLM requested a destructive tool
        ai_msg = AIMessage(
            content="I'll ingest that paper.",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "ingest_arxiv_papers",
                    "args": {"paper_ids": ["2401.12345"]},
                }
            ],
        )
        state = _make_initial_state("ingest paper 2401.12345")
        state["messages"].append(ai_msg)
        state["intent"] = "general"

        graph.update_state(config, values=state, as_node="llm_node")

        # First invoke triggers interrupt (graph pauses)
        result = await graph.ainvoke(None, config=config)
        assert "__interrupt__" in result, "Graph should pause with an interrupt"

        # Now resume with confirmation
        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"status": "success", "document_ids": ["doc-uuid-1"]},
        ):
            # Also mock the LLM for the post-tool response. llm_node may
            # route to build_synthesis_llm (llm_factory) when the
            # lightweight-synthesis path is enabled, so patch both entry
            # points.
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Papers ingested successfully!")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            with (
                patch("src.services.agent.graph._build_llm", return_value=mock_llm),
                patch(
                    "src.services.agent.llm_factory.build_synthesis_llm",
                    return_value=mock_llm,
                ),
            ):
                result = await graph.ainvoke(
                    Command(resume={"confirmed": True}),
                    config=config,
                )

        # Graph should have completed with the tool result and LLM response
        assert result is not None
        assert (
            "__interrupt__" not in result
        ), "Graph should have completed (no pending interrupt)"
        final_answer = ""
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                final_answer = msg.content
                break
        assert final_answer, "Should have a final AI response after resume"

        # Verify the graph reached the end (no next nodes)
        snapshot = await graph.aget_state(config)
        assert not snapshot.next, "Graph should have completed (no next nodes)"

    async def test_confirmed_research_interrupt_resumes_tool_execution(self):
        """Research subgraph should execute destructive tools after confirmation."""
        from langgraph.types import Command

        from src.services.agent.subgraphs.research_agent import build_research_subgraph

        checkpointer = MemorySaver()
        graph = build_research_subgraph().compile(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        ai_msg = AIMessage(
            content="I'll ingest that paper.",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "ingest_arxiv_papers",
                    "args": {"paper_ids": ["2401.12345"]},
                }
            ],
        )
        state = _make_initial_state("ingest paper 2401.12345")
        state["messages"].append(ai_msg)
        state["intent"] = "research"

        graph.update_state(config, values=state, as_node="research_llm_node")

        result = await graph.ainvoke(None, config=config)
        assert (
            "__interrupt__" in result
        ), "Research graph should pause with an interrupt"

        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"status": "success", "document_ids": ["doc-uuid-1"]},
        ) as mock_execute_tool:
            # research_llm_node uses llm_factory.build_lightweight_llm /
            # build_synthesis_llm rather than graph._build_llm — patch
            # every entry point a node might import.
            mock_llm = MagicMock()
            mock_response = AIMessage(content="Paper ingested successfully.")
            mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
                return_value=mock_response
            )
            with (
                patch("src.services.agent.graph._build_llm", return_value=mock_llm),
                patch(
                    "src.services.agent.llm_factory.build_lightweight_llm",
                    return_value=mock_llm,
                ),
                patch(
                    "src.services.agent.llm_factory.build_synthesis_llm",
                    return_value=mock_llm,
                ),
            ):
                result = await graph.ainvoke(
                    Command(resume={"confirmed": True}),
                    config=config,
                )

        mock_execute_tool.assert_awaited_once()
        assert result is not None
        assert "__interrupt__" not in result
        assert any(
            te.get("tool_name") == "ingest_arxiv_papers"
            and te.get("status") == "completed"
            for te in result.get("tool_executions", [])
        )

    async def test_denied_interrupt_skips_tool(self):
        """Denying the interrupt should skip tool execution."""
        from langgraph.types import Command

        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Setup: LLM requested a destructive tool
        ai_msg = AIMessage(
            content="I'll create a draft.",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "create_draft",
                    "args": {"themes": ["AI"]},
                }
            ],
        )
        state = _make_initial_state("create a draft about AI")
        state["messages"].append(ai_msg)
        state["intent"] = "general"

        graph.update_state(config, values=state, as_node="llm_node")

        # First invoke triggers interrupt (graph pauses)
        result = await graph.ainvoke(None, config=config)
        assert "__interrupt__" in result, "Graph should pause with an interrupt"

        # Resume with denial
        result = await graph.ainvoke(
            Command(resume={"confirmed": False}),
            config=config,
        )

        # Should have a cancellation message, no tool execution
        assert result is not None
        assert "__interrupt__" not in result, "Graph should have completed after denial"
        # Check that the cancellation message is present
        has_cancel_msg = any(
            isinstance(msg, AIMessage) and "cancelled" in (msg.content or "").lower()
            for msg in result["messages"]
        )
        assert has_cancel_msg, "Should have a cancellation message"

        # Verify the graph reached the end
        snapshot = await graph.aget_state(config)
        assert not snapshot.next, "Graph should have completed (no next nodes)"

    async def test_non_destructive_tool_skips_interrupt(self):
        """Non-destructive tool calls should bypass the interrupt node entirely."""
        from src.services.agent.graph import compile_agent_graph

        checkpointer = MemorySaver()
        graph = compile_agent_graph(checkpointer=checkpointer)
        thread_id = str(uuid4())
        config = _make_config(thread_id)

        # Setup: LLM requested a non-destructive tool (search_arxiv)
        ai_msg = AIMessage(
            content="Let me search for that.",
            tool_calls=[
                {
                    "id": "tc1",
                    "name": "search_arxiv",
                    "args": {"query": "transformers"},
                }
            ],
        )
        state = _make_initial_state("find papers on transformers")
        state["messages"].append(ai_msg)
        state["intent"] = "general"

        graph.update_state(config, values=state, as_node="llm_node")

        # Mock tool execution and LLM for the post-tool response.
        # llm_node may route to build_synthesis_llm (llm_factory) for the
        # post-tool synthesis pass — patch both entry points.
        mock_llm = MagicMock()
        mock_response = AIMessage(content="No papers found.")
        mock_llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=mock_response)
        with patch(
            "src.services.agent.tools_impl.execute_tool",
            new_callable=AsyncMock,
            return_value={"results": [], "total": 0},
        ):
            with (
                patch("src.services.agent.graph._build_llm", return_value=mock_llm),
                patch(
                    "src.services.agent.llm_factory.build_synthesis_llm",
                    return_value=mock_llm,
                ),
            ):
                result = await graph.ainvoke(None, config=config)

        # Should complete without any interrupt
        assert result is not None
        assert (
            "__interrupt__" not in result
        ), "Non-destructive tools should not trigger interrupt"
        snapshot = await graph.aget_state(config)
        assert not snapshot.next, "Graph should have completed"
