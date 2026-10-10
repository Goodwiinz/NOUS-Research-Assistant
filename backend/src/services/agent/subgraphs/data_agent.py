"""Data Agent sub-graph.

Specialized for entity extraction, knowledge graph exploration, and data analysis.
Tools: extract_entities, search_knowledge_graph, search_documents,
       list_project_documents

Shared machinery (routers, forced synthesis, wiring) comes from
``subgraphs._factory.make_specialist_subgraph``; this module keeps only what
is genuinely data-specific: the tool/ceiling constants, the system prompt,
and the LLM node. Data has no interrupt node — no destructive tools — and
its nodes are historically undecorated (``tracked=False``).
"""

import asyncio
import logging

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from src.services.agent.graph import _sanitize_messages
from src.services.agent.state import AgentState
from src.services.agent.subgraphs._factory import make_specialist_subgraph
from src.services.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

DATA_TOOLS = [
    descriptor.tool for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("data")
]

DATA_TOOL_NAMES_LIST = [t.name for t in DATA_TOOLS]

# Named ceiling (preserves the previous hardcoded ``< 8``) so the
# forced-synthesis routing below and the bump in data_force_synthesis_node
# stay in sync. Same pattern as research's MAX_RESEARCH_TOOL_LOOPS but an
# independent value — research deliberately lowered theirs to 5 after a
# runaway-fanout trace; this one has no such justification yet.
MAX_DATA_TOOL_LOOPS = 8


def _build_data_system_prompt() -> str:
    """Construct the data subgraph system prompt with shared rules embedded.

    Driver protocol sourced from ``AGENTS_data.md`` — see ``agents_md_loader``.
    Inline fallback for missing-file safety.

    Imported lazily to avoid circular imports with graph.py.
    """
    from src.services.agent.graph import SHARED_AGENT_RULES
    from src.services.agent.subgraphs.agents_md_loader import load_agents_md

    driver_protocol = load_agents_md("data")
    if driver_protocol:
        return f"{driver_protocol}\n\n{SHARED_AGENT_RULES}"

    return (
        "You are a specialized Data Agent focused on extracting entities, "
        "exploring knowledge graphs, and analyzing structured data from documents.\n\n"
        f"{SHARED_AGENT_RULES}\n\n"
        "Search the knowledge graph before using entity IDs; pass through only "
        "IDs returned by those results, and never invent graph IDs. State only "
        "returned entity and relationship rows. Scope neighborhood results "
        "separately from organization graph totals. Use search_documents to "
        "resolve a document UUID before extract_entities. The per-turn budget "
        "is max 8 tool loops; then synthesize only from the results already "
        "returned. Present findings in structured formats."
    )


async def data_llm_node(state: AgentState, config: RunnableConfig) -> dict:
    """Data-specialized LLM node."""
    from langchain_core.messages import ToolMessage

    from src.core.config import get_settings
    from src.services.agent.graph import AGENT_LLM_TIMEOUT_SECONDS

    sanitized = _sanitize_messages(state["messages"])

    settings = get_settings()
    use_synthesis = bool(
        settings.AGENT_LIGHTWEIGHT_SYNTHESIS
        and sanitized
        and isinstance(sanitized[-1], ToolMessage)
    )

    # Hoisted above the branch: _build_llm is used inside it, so importing
    # after would NameError.
    from src.services.agent.graph import _build_llm, _merge_run_config

    if use_synthesis:
        from src.services.agent.llm_factory import (
            build_synthesis_llm,
            get_synthesis_model_name,
        )

        llm = build_synthesis_llm(max_tokens=4096, tool_calling=True)
        resolved_model = get_synthesis_model_name()
        logger.debug("data_llm_node: using synthesis model after ToolMessage")
    else:
        # Tool-decision turn runs on the main deployment — see the note in
        # research_agent.research_llm_node. KG traversal chains several calls
        # (search → neighborhood → paths), which is exactly the multi-step
        # shape small tiers degrade on.
        llm = _build_llm(model_override=state.get("model") or None)
        from src.services.agent.llm_factory import resolve_chat_deployment

        resolved_model = resolve_chat_deployment(state.get("model") or None)
        logger.debug("data_llm_node: using main model for tool decision")
    from src.services.agent.runtime_context import render_dynamic_context

    dynamic_context = render_dynamic_context(
        state,
        config,
        resolved_model=resolved_model,
        branch="data",
        messages=sanitized,
    )
    from src.services.agent.reflection import reflection_revision_messages

    messages = [
        SystemMessage(content=_build_data_system_prompt()),
        SystemMessage(content=dynamic_context),
        *sanitized,
        # R8-A2: on a reflection "revise" pass, hand the review issues back.
        # Transient — never returned, so never checkpointed.
        *reflection_revision_messages(state),
    ]
    from src.services.agent._nodes_llm import (
        normalize_ai_content as _normalize_ai_content,
    )
    from src.services.agent._nodes_llm import tools_for_runtime_projection

    llm_with_tools = llm.bind_tools(
        tools_for_runtime_projection(state, branch="data"),
        parallel_tool_calls=settings.AGENT_PARALLEL_TOOL_CALLS,
    )

    invoke_config = _merge_run_config(
        config,
        run_name="data_llm_node",
        tags=["intent:knowledge_graph", "subgraph:data"],
    )
    try:
        response = await asyncio.wait_for(
            llm_with_tools.ainvoke(messages, config=invoke_config),
            timeout=AGENT_LLM_TIMEOUT_SECONDS,
        )
        response = _normalize_ai_content(response)
    except asyncio.TimeoutError:
        logger.warning(
            "data_llm_node: LLM exceeded %ds; emitting fallback",
            AGENT_LLM_TIMEOUT_SECONDS,
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        "The data model took too long to respond. Please "
                        "try again with a narrower query."
                    ),
                ),
            ],
            "last_error": "data_llm_timeout",
            "error_count": state.get("error_count", 0) + 1,
        }

    return {
        "messages": [response],
    }


# intent_filter excludes knowledge_graph — KG queries are deterministic
# lookups, so the reflection node passes through for them.
_parts = make_specialist_subgraph(
    name="data",
    llm_node=data_llm_node,
    max_tool_loops=MAX_DATA_TOOL_LOOPS,
    prompt_builder=_build_data_system_prompt,
    synthesis_addendum=(
        "\n\n## Final synthesis turn\n"
        "You ran {count} tool calls and reached "
        "the per-turn tool budget. Do not request any more tools. Write a "
        "final answer drawn from the tool results already in this "
        "conversation: present the entities, relationships, or graph "
        "findings gathered so far in a structured format, and state plainly "
        "any query you could not complete. Do NOT repeat or quote these "
        "instructions in your reply."
    ),
    synthesis_timeout_message=(
        "I gathered results but ran out of time composing a final "
        "summary. Please ask me to summarize the findings above."
    ),
    loop_exhaustion_intent="knowledge_graph",
    invoke_tags=["intent:knowledge_graph", "subgraph:data"],
    reflection_intent_filter={"research", "writing"},
    has_interrupt=False,
    tracked=False,
)

data_should_continue = _parts.should_continue
data_force_synthesis_node = _parts.force_synthesis_node
route_after_data_tool_node = _parts.route_after_tool_node


def build_data_subgraph() -> StateGraph:
    """Build the data agent sub-graph.

    Flow:
      data_planner_node -> data_llm_node -> data_should_continue ->
        | data_tool_node -> data_compactor_node -> data_llm_node (loop)
        | data_reflection_gate -> END (or revise -> data_llm_node)

    Note: Reflection gate skips for knowledge_graph intent (excluded from
    intent_filter) since KG queries are deterministic lookups.
    """
    return _parts.build()
