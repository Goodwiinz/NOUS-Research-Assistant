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
        "Be analytical and thorough. Present findings in structured formats."
    )


async def data_llm_node(state: AgentState, config: RunnableConfig) -> dict:
    """Data-specialized LLM node."""
    from langchain_core.messages import ToolMessage

    from src.core.config import get_settings
    from src.services.agent.graph import AGENT_LLM_TIMEOUT_SECONDS
    from src.services.agent._nodes_llm import _attachment_status_part

    sanitized = _sanitize_messages(state["messages"])
    from src.services.agent.retrieval_provenance import render_retrieval_prompt

    messages = [
        SystemMessage(content=_build_data_system_prompt()),
    ]
    attachment_status_prompt = _attachment_status_part(
        state.get("attachment_status", [])
    )
    if attachment_status_prompt:
        messages.append(SystemMessage(content=attachment_status_prompt))
    messages.append(
        SystemMessage(
            content=render_retrieval_prompt(
                state.get("retrieved_contexts", []), sanitized
            )
        )
    )
    from src.services.agent.runtime_snapshot import render_project_skill_catalog

    skill_catalog_prompt = render_project_skill_catalog(
        state.get("project_skill_catalog", [])
    )
    if skill_catalog_prompt:
        messages.append(SystemMessage(content=skill_catalog_prompt))
    messages += sanitized

    settings = get_settings()
    use_synthesis = bool(
        settings.AGENT_LIGHTWEIGHT_SYNTHESIS
        and sanitized
        and isinstance(sanitized[-1], ToolMessage)
    )

    # Close the plan→execute handoff (see planner.render_plan_directive).
    if not use_synthesis:
        from src.services.agent.planner import render_plan_directive

        plan_directive = render_plan_directive(state.get("plan"))
        if plan_directive:
            messages.insert(1, SystemMessage(content=plan_directive))

    # Hoisted above the branch: _build_llm is used inside it, so importing
    # after would NameError.
    from src.services.agent.graph import _build_llm, _merge_run_config

    if use_synthesis:
        from src.services.agent.llm_factory import build_synthesis_llm

        llm = build_synthesis_llm(max_tokens=4096, tool_calling=True)
        logger.debug("data_llm_node: using synthesis model after ToolMessage")
    else:
        # Tool-decision turn runs on the main deployment — see the note in
        # research_agent.research_llm_node. KG traversal chains several calls
        # (search → neighborhood → paths), which is exactly the multi-step
        # shape small tiers degrade on.
        llm = _build_llm(model_override=state.get("model") or None)
        logger.debug("data_llm_node: using main model for tool decision")
    from src.services.agent._nodes_llm import (
        normalize_ai_content as _normalize_ai_content,
    )
    from src.services.agent._nodes_llm import tools_for_runtime_snapshot

    llm_with_tools = llm.bind_tools(
        tools_for_runtime_snapshot(DATA_TOOLS, state),
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
