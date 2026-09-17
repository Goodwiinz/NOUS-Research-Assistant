"""Research Agent sub-graph.

Specialized for paper discovery, search, and ingestion tasks.
Tools: search_arxiv, ingest_arxiv_papers, search_documents,
       create_project, add_document_to_project, list_project_documents

Shared machinery (routers, interrupt, forced synthesis, wiring) comes from
``subgraphs._factory.make_specialist_subgraph``; this module keeps only what
is genuinely research-specific: the tool/ceiling constants, the system
prompt, the direct-arxiv fast path, and the LLM node.
"""

import asyncio
import logging
import re
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from src.services.agent.graph import _sanitize_messages
from src.services.agent.observability import track_node_execution
from src.services.agent.state import AgentState
from src.services.agent.subgraphs._factory import make_specialist_subgraph
from src.services.agent.tool_registry import ToolPolicyTag
from src.services.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

RESEARCH_TOOLS = [
    descriptor.tool for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("research")
]

RESEARCH_TOOL_NAMES_LIST = [t.name for t in RESEARCH_TOOLS]

# Lowered from 8 after trace 019e18f0 showed a 4-round runaway tool fan-out
# (13+ search_arxiv calls, 95s wall). Five iterations is enough for a search →
# refine → ingest → list → confirm sequence; anything more is the agent
# refining queries the user did not ask for.
MAX_RESEARCH_TOOL_LOOPS = 5

_DIRECT_ARXIV_SEARCH_RE = re.compile(
    r"\b(?:search|find|look\s+up|lookup|discover|list|show)\b.*\barxiv\b"
    r"|\barxiv\b.*\b(?:search|find|look\s+up|lookup|discover|list|show)\b",
    re.IGNORECASE,
)


# The fast path hard-codes search_arxiv's default 365-day window, so any turn
# that states its own window must skip it and let the LLM set ``recency_days``
# (a five-year review would otherwise silently return only the last year).
# "recent"/"latest" are deliberately absent — the 365-day default is right for
# those, and they are the most common phrasing the fast path exists to serve.
_EXPLICIT_TIME_WINDOW_RE = re.compile(
    r"\b(?:last|past|previous|within|over)\s+(?:the\s+)?"
    r"(?:\w+[-\s]+)?(?:year|month|week|day|decade)s?\b"
    r"|\b(?:since|before|after|between|from|until|up\s+to)\s+(?:19|20)\d{2}\b"
    r"|\b(?:in|during)\s+(?:19|20)\d{2}\b"
    r"|\b(?:19|20)\d{2}\s*(?:-|–|to)\s*(?:19|20)\d{2}\b"
    r"|\ball[-\s]?time\b"
    r"|\bdecades?\b"
    r"|\b(?:earliest|oldest|foundational|seminal|historical|pioneering)\b",
    re.IGNORECASE,
)


def _direct_arxiv_search_query(content: str) -> str | None:
    """Return a search query when the user explicitly asks to search arXiv."""
    if not content or not content.strip():
        return None
    if not _DIRECT_ARXIV_SEARCH_RE.search(content):
        return None
    if _EXPLICIT_TIME_WINDOW_RE.search(content):
        return None

    query = re.sub(
        r"\b(?:search|find|look\s+up|lookup|discover|list|show)\b",
        " ",
        content,
        count=1,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\barxiv(?:\.org)?\b", " ", query, flags=re.IGNORECASE)
    query = query.strip()
    query = re.sub(
        r"^(?:for|on|about|regarding|related\s+to)\s+",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = " ".join(query.split())
    return query or content.strip()


def _direct_arxiv_search_message(messages: list) -> AIMessage | None:
    """Build a deterministic search_arxiv call for clear direct-search turns."""
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None

    raw_content = messages[-1].content
    content = raw_content if isinstance(raw_content, str) else str(raw_content)
    query = _direct_arxiv_search_query(content)
    if query is None:
        return None

    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": f"direct_search_arxiv_{uuid4().hex}",
                "name": "search_arxiv",
                "args": {"query": query, "max_results": 5},
            }
        ],
    )


def _build_research_system_prompt() -> str:
    """Construct the research subgraph system prompt with shared rules embedded.

    Driver protocol (tools, loop, constraints, heuristics) is sourced from
    ``AGENTS_research.md`` — see ``agents_md_loader`` for the rationale.
    Falls back to a minimal inline prompt if the file is missing so the
    subgraph never crashes on a deploy that omits the markdown file.

    Imported lazily to avoid circular imports with graph.py.
    """
    from src.services.agent.graph import SHARED_AGENT_RULES
    from src.services.agent.subgraphs.agents_md_loader import load_agents_md

    driver_protocol = load_agents_md("research")
    if driver_protocol:
        return f"{driver_protocol}\n\n{SHARED_AGENT_RULES}"

    # Fallback if AGENTS_research.md is missing (deploy issue).
    return (
        "You are a research assistant focused on discovering, searching, "
        "and organizing academic papers and documents.\n\n"
        f"{SHARED_AGENT_RULES}\n\n"
        "Be thorough in searching and systematic in organizing research."
    )


# Only tools actually in RESEARCH_TOOLS belong here — the filtered tool
# node can never execute anything else, so extra entries are dead weight
# that misleads readers about what this subgraph can run.
RESEARCH_DESTRUCTIVE_TOOLS = frozenset(
    descriptor.name
    for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("research")
    if ToolPolicyTag.DESTRUCTIVE in descriptor.policy_tags
)


@track_node_execution("research_llm_node")
async def research_llm_node(state: AgentState, config: RunnableConfig) -> dict:
    """Research-specialized LLM node."""
    from langchain_core.messages import ToolMessage

    from src.core.config import get_settings

    sanitized = _sanitize_messages(state["messages"])
    direct_search = _direct_arxiv_search_message(sanitized)
    if direct_search is not None:
        return {"messages": [direct_search]}

    from src.services.agent.retrieval_provenance import render_retrieval_prompt
    from src.services.agent._nodes_llm import _attachment_status_part

    messages = [
        SystemMessage(content=_build_research_system_prompt()),
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
    # Post-tool synthesis turn → use the lightweight deployment. Mirrors the
    # main graph.llm_node optimization. Trace 019e191a showed the main gpt-5
    # spending 70s + 4352 reasoning tokens on prose synthesis after a single
    # search_arxiv call. Lightweight handles that in ~5-10s.
    use_lightweight_synthesis = bool(
        settings.AGENT_LIGHTWEIGHT_SYNTHESIS
        and sanitized
        and isinstance(sanitized[-1], ToolMessage)
    )

    # Close the plan→execute handoff (see planner.render_plan_directive).
    # Inject on the pre-tool pass only; skip on synthesis turns where tools
    # have already run.
    if not use_lightweight_synthesis:
        from src.services.agent.planner import render_plan_directive

        plan_directive = render_plan_directive(state.get("plan"))
        if plan_directive:
            messages.insert(1, SystemMessage(content=plan_directive))

    # Hoisted above the branch: _build_llm is used inside it, so importing
    # after would NameError.
    from src.services.agent.graph import (
        AGENT_LLM_TIMEOUT_SECONDS,
        _build_llm,
        _merge_run_config,
    )

    if use_lightweight_synthesis:
        from src.services.agent.llm_factory import build_synthesis_llm

        llm = build_synthesis_llm(max_tokens=4096, tool_calling=True)
        logger.debug("research_llm_node: using synthesis model after ToolMessage")
    else:
        # Tool-decision turn: the main deployment, deliberately. Multi-step
        # function calling is where model tier dominates — benchmarks put the
        # top tier around 72% at 5 required calls against ~16% for the small
        # ones, and this node drives an 8-loop tool path. The cheap tier used
        # to sit here only because the main deployment was model-router,
        # which hit the 30s cap (trace 019e1da5); that is no longer the
        # deployment, so the workaround goes with it.
        llm = _build_llm(model_override=state.get("model") or None)
        logger.debug("research_llm_node: using main model for tool decision")
    # See graph.llm_node for rationale on parallel_tool_calls=False.
    from src.services.agent._nodes_llm import (
        normalize_ai_content as _normalize_ai_content,
    )
    from src.services.agent._nodes_llm import tools_for_runtime_snapshot

    llm_with_tools = llm.bind_tools(
        tools_for_runtime_snapshot(RESEARCH_TOOLS, state),
        parallel_tool_calls=settings.AGENT_PARALLEL_TOOL_CALLS,
    )

    invoke_config = _merge_run_config(
        config,
        run_name="research_llm_node",
        tags=["intent:research", "subgraph:research"],
    )
    try:
        response = await asyncio.wait_for(
            llm_with_tools.ainvoke(messages, config=invoke_config),
            timeout=AGENT_LLM_TIMEOUT_SECONDS,
        )
        response = _normalize_ai_content(response)
    except asyncio.TimeoutError:
        logger.warning(
            "research_llm_node: LLM exceeded %ds; emitting fallback",
            AGENT_LLM_TIMEOUT_SECONDS,
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        "The research model took too long to respond. Please "
                        "try again or narrow the query."
                    ),
                ),
            ],
            "last_error": "research_llm_timeout",
            "error_count": state.get("error_count", 0) + 1,
        }

    return {
        "messages": [response],
    }


_parts = make_specialist_subgraph(
    name="research",
    llm_node=research_llm_node,
    max_tool_loops=MAX_RESEARCH_TOOL_LOOPS,
    prompt_builder=_build_research_system_prompt,
    synthesis_addendum=(
        "\n\n## Final synthesis turn\n"
        "You ran {count} tool calls and reached "
        "the per-turn search budget. Do not request any more tools. Write a "
        "final answer drawn from the tool results already in this conversation: "
        "report the research findings gathered so far in the format the user "
        "requested. Do NOT repeat or quote these instructions in your reply."
    ),
    synthesis_timeout_message=(
        "I ran my searches but the final summary step timed out "
        "({timeout}s) before producing an answer. "
        "The search results are still in context — please ask me again "
        "and I'll synthesize them directly, rather than re-searching."
    ),
    loop_exhaustion_intent="research",
    invoke_tags=["intent:research", "subgraph:research"],
    reflection_intent_filter={"research"},
    has_interrupt=True,
    # Late-bound so tests that monkeypatch this module's TOOL_REGISTRY
    # still steer routing/interrupt decisions.
    tool_registry_getter=lambda: TOOL_REGISTRY,
)

research_should_continue = _parts.should_continue
research_force_synthesis_node = _parts.force_synthesis_node
research_interrupt_node = _parts.interrupt_node
research_after_interrupt = _parts.after_interrupt
route_after_research_tool_node = _parts.route_after_tool_node


def build_research_subgraph() -> StateGraph:
    """Build the research agent sub-graph.

    Flow:
      research_planner_node -> research_llm_node -> research_should_continue ->
        | research_tool_node -> research_compactor_node -> research_llm_node (loop)
        | research_reflection_gate -> END (or revise -> research_llm_node)
    """
    return _parts.build()
