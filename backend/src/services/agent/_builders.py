"""LangGraph builder + routing helpers for the agent graph.

Extracted from ``graph.py`` so the orchestration module stays under the
800-line house rule. ``graph.py`` re-exports every public name so the
LangGraph entry point in ``langgraph.json`` (``create_graph``) and all
existing callers (``compile_agent_graph``, ``build_agent_graph``,
``should_continue``, ``after_interrupt``, ``MAX_ERRORS``,
``MAX_TOOL_LOOPS``) keep resolving from ``src.services.agent.graph``
without churn.

What lives here:

* ``should_continue`` / ``after_interrupt`` — the two conditional edges
  on the general path.
* ``MAX_ERRORS`` / ``MAX_TOOL_LOOPS`` — soft circuit breakers consumed
  by ``should_continue``.
* ``build_agent_graph`` — wires every node + sub-graph into a single
  ``StateGraph``.
* ``compile_agent_graph`` — applies checkpointer + store + compiles.
* ``create_graph`` — no-arg entry for ``langgraph.json``.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from src.services.agent._nodes_classify import preprocessing_node, route_by_intent
from src.services.agent._nodes_llm import llm_node
from src.services.agent._nodes_memory import memory_save_node
from src.services.agent._nodes_tools import interrupt_node, tool_node
from src.services.agent._sanitize import (
    _TOOL_ERROR_DEGRADED_ANSWER,
    normalize_terminal_messages,
    terminal_message_additions,
)
from src.services.agent.compactor import make_compactor_node
from src.services.agent.planner import make_planner_node
from src.services.agent.reflection import make_reflection_gate
from src.services.agent.state import AgentState
from src.services.agent.tool_registry import ToolPolicyTag
from src.services.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Soft circuit breakers
# ---------------------------------------------------------------------------

# Was 4 (down from 10 to stop 44-71s general roots re-running queries). Bumped
# to 6: 4 cut off legitimate capability-complete flows on the general path,
# which is the *tightest* ceiling in the system even though it is where
# multi-tool tasks route (both subgraphs run higher — writing 8, research 5).
# Measured on agent-project-management-v1: create_project → search_documents
# (resolve a document by title) → add_document_to_project → create_project_note
# → list_project_documents is 5 sequential calls, and at 4 the trailing read
# was starved — force_synthesis_node fired and the model narrated a fabricated
# list instead of executing it. 6 covers that flow with one spare; still well
# short of a runaway.
MAX_TOOL_LOOPS = 6
MAX_ERRORS = 3

# Derivation: 6 tool loops × 3 nodes (llm+tool+compactor) = 18, plus 6 fixed
# nodes (preprocessing, planner, interrupt, force_synthesis, reflection,
# memory_save), plus 2 revise cycles × 3 nodes = 6 → ~30 in the worst case.
# 50 leaves headroom for that while still cutting off a true runaway. We set it
# explicitly because LangGraph's default varies by version across our pin
# (langgraph>=0.4,<2.0): 25 on 0.4.x, 10007 on 1.0+ — neither is a safe default
# to rely on for this graph.
RECURSION_LIMIT = 50


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def should_continue(state: AgentState) -> str:
    """Decide whether to route to tool_node, interrupt_node, force_synthesis_node, or reflection_gate."""
    last = state["messages"][-1] if state["messages"] else None
    has_pending_tool_calls = isinstance(last, AIMessage) and bool(last.tool_calls)

    # A mixed/unsupported batch must reach the execution boundary's paired
    # rejection before any confirmation interrupt or loop/error terminal.
    # That node checks the same projection and never claims a durable write.
    if has_pending_tool_calls:
        from src.services.agent._nodes_tools import unavailable_tool_calls

        if unavailable_tool_calls(last.tool_calls, state):
            return "tool_node"

    # Bail out if too many errors have accumulated
    if state.get("error_count", 0) >= MAX_ERRORS:
        logger.warning(
            "Agent reached max error count (%d), stopping. Last error: %s",
            MAX_ERRORS,
            state.get("last_error", ""),
        )
        return "reflection_gate"

    if has_pending_tool_calls and state.get("tool_loop_count", 0) < MAX_TOOL_LOOPS:
        # Under budget — execute tools (or pause for confirmation).
        has_destructive = any(
            TOOL_REGISTRY.has_policy(tc["name"], ToolPolicyTag.DESTRUCTIVE)
            for tc in last.tool_calls
        )
        if has_destructive:
            return "interrupt_node"
        return "tool_node"

    # Loop ceiling tripped while model still wants more tools. Route through
    # force_synthesis_node so the final AIMessage has real content instead of
    # an empty body + orphan tool_calls (reflection_gate would otherwise flag
    # "no response" — mirrors research_subgraph fix for trace 019e1903).
    # If force_synthesis already fired and still emitted tool_calls (defective
    # model), break the loop and exit through reflection.
    if has_pending_tool_calls and not state.get("_force_synthesis_fired"):
        return "force_synthesis_node"
    return "reflection_gate"


def route_after_tool_node(state: AgentState) -> str:
    """Route from tool_node: skip the re-plan loop when the batch was fully deduped.

    A fully-deduped batch (every tool call was already executed this turn with
    identical args) carries zero new information.  Routing through
    compactor_node → llm_node would burn another LLM round-trip (~8 s Azure
    p95) for no gain.  Instead go straight to force_synthesis_node so it
    produces a final answer from the cached results already in state.

    For all normal batches (at least one fresh call ran) we keep the existing
    compactor_node → llm_node path so no behavior changes for the common case.

    Subgraph filtered_tool_nodes are NOT affected — this function only wires
    the main-graph tool_node.  Follow-up: apply the same optimisation to the
    research/writing/data filtered_tool_nodes (GOO-XXX).
    """
    if state.get("capability_limitation"):
        return "memory_save_node"
    if state.get("tools_all_deduped"):
        return "force_synthesis_node"
    return "compactor_node"


def route_after_planner_node(state: AgentState) -> str:
    """End a validated capability limitation before the executor model."""
    if state.get("capability_limitation"):
        return "memory_save_node"
    return "llm_node"


def after_interrupt(state: AgentState) -> str:
    """Route after interrupt: proceed to tool_node if confirmed, else reflection gate and end."""
    if state.get("user_confirmed", False):
        return "tool_node"
    return "reflection_gate"


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

_RETRY_POLICY = RetryPolicy(max_attempts=3)


def build_agent_graph() -> StateGraph:
    """Build the uncompiled agent state graph.

    Flow:
      START -> preprocessing_node (RAG + classify + memory in parallel)
        -> [route_by_intent]
        -> research_subgraph | writing_subgraph | data_subgraph | general path

      General path:
        planner_node -> llm_node -> [should_continue]
          -> tool_node -> compactor_node -> llm_node (loop)
          -> interrupt_node -> [after_interrupt] -> tool_node | reflection_gate
          -> reflection_gate -> [reflection_route] -> memory_save_node | llm_node (revise)

      Sub-graphs (internal planner + compactor + reflection) -> memory_save_node -> END
    """
    from src.services.agent.subgraphs.data_agent import build_data_subgraph
    from src.services.agent.subgraphs.research_agent import build_research_subgraph
    from src.services.agent.subgraphs.writing_agent import build_writing_subgraph

    # General-path v2 nodes
    from src.services.agent.tool_registry import runtime_tool_descriptors

    planner_node_fn = make_planner_node(
        lambda state: [
            descriptor.name
            for descriptor in runtime_tool_descriptors("main", state, TOOL_REGISTRY)
        ]
    )
    compactor_node_fn = make_compactor_node()
    reflection_node_fn, reflection_route_fn = make_reflection_gate()

    async def terminal_reflection_node(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        """Repair abandoned calls and keep reflection scoped to this turn."""
        messages = state.get("messages", [])
        reason = (
            _TOOL_ERROR_DEGRADED_ANSWER
            if state.get("error_count", 0) >= MAX_ERRORS
            else "I could not complete this request. Please try again or adjust your request."
        )
        normalized = normalize_terminal_messages(messages, reason)
        additions = terminal_message_additions(messages, normalized)
        updates = await reflection_node_fn({**state, "messages": normalized}, config)
        if additions:
            updates = {**updates, "messages": additions}
        return updates

    graph = StateGraph(AgentState)

    from src.services.agent._nodes_llm import force_synthesis_node

    graph.add_node("preprocessing_node", preprocessing_node)
    graph.add_node("planner_node", planner_node_fn)
    graph.add_node("llm_node", llm_node, retry=_RETRY_POLICY)
    graph.add_node("tool_node", tool_node)
    graph.add_node("compactor_node", compactor_node_fn)
    graph.add_node("interrupt_node", interrupt_node)
    graph.add_node("force_synthesis_node", force_synthesis_node)
    graph.add_node("reflection_gate", terminal_reflection_node)
    graph.add_node("memory_save_node", memory_save_node, retry=_RETRY_POLICY)

    # Sub-graphs compiled as nodes (they have internal planner/compactor/reflection)
    graph.add_node("research_subgraph", build_research_subgraph().compile())
    graph.add_node("writing_subgraph", build_writing_subgraph().compile())
    graph.add_node("data_subgraph", build_data_subgraph().compile())

    graph.set_entry_point("preprocessing_node")

    # Route by intent after parallel preprocessing
    graph.add_conditional_edges(
        "preprocessing_node",
        route_by_intent,
        {
            "research_subgraph": "research_subgraph",
            "writing_subgraph": "writing_subgraph",
            "data_subgraph": "data_subgraph",
            "llm_node": "planner_node",
        },
    )

    # Sub-graphs (with internal reflection) -> memory_save_node -> END
    graph.add_edge("research_subgraph", "memory_save_node")
    graph.add_edge("writing_subgraph", "memory_save_node")
    graph.add_edge("data_subgraph", "memory_save_node")

    # General path: planner -> llm, or directly to its deterministic terminal.
    graph.add_conditional_edges(
        "planner_node",
        route_after_planner_node,
        {"llm_node": "llm_node", "memory_save_node": "memory_save_node"},
    )

    # General path: llm_node -> conditional
    graph.add_conditional_edges(
        "llm_node",
        should_continue,
        {
            "tool_node": "tool_node",
            "interrupt_node": "interrupt_node",
            "force_synthesis_node": "force_synthesis_node",
            "reflection_gate": "reflection_gate",
        },
    )
    # Forced synthesis always exits through reflection (it produced final text).
    graph.add_edge("force_synthesis_node", "reflection_gate")
    graph.add_conditional_edges(
        "interrupt_node",
        after_interrupt,
        {"tool_node": "tool_node", "reflection_gate": "reflection_gate"},
    )

    # General path: tool_node -> compactor_node -> llm_node (normal loop),
    # OR tool_node -> force_synthesis_node when the entire batch was deduped
    # (no fresh calls ran; skips the wasted re-plan round-trip).
    graph.add_conditional_edges(
        "tool_node",
        route_after_tool_node,
        {
            "compactor_node": "compactor_node",
            "force_synthesis_node": "force_synthesis_node",
            "memory_save_node": "memory_save_node",
        },
    )
    graph.add_edge("compactor_node", "llm_node")

    # Reflection gate routes: proceed -> memory_save, revise -> llm_node
    graph.add_conditional_edges(
        "reflection_gate",
        reflection_route_fn,
        {"proceed": "memory_save_node", "revise": "llm_node"},
    )
    graph.add_edge("memory_save_node", END)

    return graph


# Cache for the compiled production graph. ``build_agent_graph`` rebuilds the
# main StateGraph AND compiles all three subgraphs (research/writing/data), so
# rebuilding per request — i.e. on every chat turn — is pure overhead. The
# compiled graph is request-agnostic (per-turn state lives in the checkpointer
# keyed by ``thread_id``) and the checkpointer/store are process singletons, so
# it is safe to compile once and reuse. Keyed by ``(id(checkpointer), id(store))``.
_COMPILED_GRAPH_CACHE: dict = {}


def reset_compiled_graph_cache() -> None:
    """Clear the compiled-graph cache (for tests / hot-reload)."""
    _COMPILED_GRAPH_CACHE.clear()


def compile_agent_graph(checkpointer=None, store=None, **kwargs):
    """Compile the agent graph, optionally with a checkpointer and store.

    ``checkpointer`` may be ``None`` (no checkpointing), an instance of
    ``BaseCheckpointSaver``, or ``True`` to opt into the default in-memory
    saver. Anything else is rejected with a clear error rather than
    silently passing a bool to ``graph.compile()`` (which would crash with
    ``AttributeError`` deep inside LangGraph at runtime).

    ``store`` is an optional ``BaseStore`` for long-term, cross-thread
    memory (user preferences, facts).  When provided, LangGraph injects it
    into nodes that accept a ``Runtime`` parameter so they can use
    ``runtime.store`` instead of importing the singleton directly.

    The compiled graph for the production path (a real ``BaseCheckpointSaver``,
    no extra compile kwargs) is cached and reused across turns — see
    ``_COMPILED_GRAPH_CACHE`` — so the main graph + 3 subgraphs are built once,
    not on every turn. ``None``/``True`` (no-checkpoint / fresh MemorySaver for
    tests) always rebuild.
    """
    from langgraph.checkpoint.base import BaseCheckpointSaver

    # Fast path: reuse the already-compiled graph for the production singletons.
    # Checked BEFORE build_agent_graph() so a cache hit skips building entirely.
    cacheable = isinstance(checkpointer, BaseCheckpointSaver) and not kwargs
    cache_key = (id(checkpointer), id(store)) if cacheable else None
    if cacheable:
        cached = _COMPILED_GRAPH_CACHE.get(cache_key)
        if cached is not None:
            return cached

    graph = build_agent_graph()

    compile_kwargs: dict = {}
    if store is not None:
        compile_kwargs["store"] = store

    if checkpointer is None or checkpointer is False:
        return graph.compile(**compile_kwargs)

    if checkpointer is True:
        # Convenience: ``True`` opts into the in-memory default so callers
        # don't have to import MemorySaver themselves.
        from langgraph.checkpoint.memory import MemorySaver

        return graph.compile(checkpointer=MemorySaver(), **compile_kwargs)

    if isinstance(checkpointer, BaseCheckpointSaver):
        compiled = graph.compile(checkpointer=checkpointer, **compile_kwargs)
        if cacheable:
            _COMPILED_GRAPH_CACHE[cache_key] = compiled
        return compiled

    raise TypeError(
        "compile_agent_graph(checkpointer=...) must be None, True, False, "
        f"or a BaseCheckpointSaver instance — got {type(checkpointer).__name__}"
    )


def create_graph():
    """No-arg entry point for langgraph dev (langgraph.json)."""
    return compile_agent_graph(checkpointer=True)


__all__ = [
    "MAX_ERRORS",
    "MAX_TOOL_LOOPS",
    "RECURSION_LIMIT",
    "should_continue",
    "after_interrupt",
    "route_after_tool_node",
    "route_after_planner_node",
    "build_agent_graph",
    "compile_agent_graph",
    "reset_compiled_graph_cache",
    "create_graph",
]
