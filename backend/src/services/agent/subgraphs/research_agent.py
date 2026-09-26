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
import unicodedata
from collections.abc import Mapping
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
    r"(?:please[ ]+)?(?:search[ ]+arxiv[ ]+for|find[ ]+papers[ ]+on[ ]+arxiv[ ]+about)"
    r"[ ]+(?P<topic>.+)",
    re.IGNORECASE,
)

_DIRECT_ARXIV_BLOCKED_WORDS = frozenset(
    {
        "and",
        "or",
        "then",
        "also",
        "before",
        "after",
        "not",
        "no",
        "never",
        "without",
        "except",
        "exclude",
        "it",
        "them",
        "these",
        "those",
        "this",
        "that",
        "above",
        "same",
        "skill",
        "skills",
        "top",
        "first",
        "last",
        "exactly",
        "only",
        "all",
        "every",
        "each",
        "few",
        "several",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "recent",
        "latest",
        "newest",
        "earliest",
        "oldest",
        "current",
        "since",
        "during",
        "within",
        "previous",
        "year",
        "years",
        "month",
        "months",
        "week",
        "weeks",
        "day",
        "days",
        "decade",
        "decades",
    }
)
_DIRECT_ARXIV_BLOCKED_MODIFIER_RE = re.compile(
    r"\b(?:19|20)\d{2}\b"
    r"|\b\d+\s+(?:papers?|results?|studies|articles?)\b"
    r"|\b(?:up\s+to|at\s+least|no\s+more\s+than|more\s+than|"
    r"fewer\s+than|less\s+than)\s+\d+\b",
    re.IGNORECASE,
)


def _direct_arxiv_search_query(content: str) -> str | None:
    """Match only a short standalone affirmative arXiv search command.

    This is an optimization for one read-only operation. Any punctuation,
    modifier, compound request, reference to prior context, or non-default
    time/count constraint falls through to normal planning.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    # Do not let strip() turn a multi-line request into a standalone command.
    # The shortcut accepts plain spaces only; controls always go through normal
    # planning even when they occur outside the matched phrase.
    if any(unicodedata.category(char).startswith("C") for char in content):
        return None
    match = _DIRECT_ARXIV_SEARCH_RE.fullmatch(content.strip())
    if match is None:
        return None

    topic = match.group("topic").strip()
    if topic.endswith("."):
        topic = topic[:-1].rstrip()
    if not topic:
        return None

    # The prompt permits Unicode letters and numbers in topics while excluding
    # all punctuation/control characters except the ASCII hyphen used in
    # technical names (for example, retrieval-augmented generation).
    if any(
        not (
            char == " "
            or char == "-"
            or unicodedata.category(char)[0] in {"L", "M", "N"}
        )
        for char in topic
    ):
        return None

    words = topic.split()
    if not 1 <= len(words) <= 20:
        return None
    normalized_words = {
        part.casefold() for word in words for part in word.split("-") if part
    }
    if normalized_words & _DIRECT_ARXIV_BLOCKED_WORDS:
        return None
    if _DIRECT_ARXIV_BLOCKED_MODIFIER_RE.search(topic):
        return None
    return topic


def _direct_arxiv_search_message(
    messages: list, state: Mapping[str, object] | None = None
) -> AIMessage | None:
    """Build a direct search only for fresh, ordinary, projected search turns."""
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None

    raw_content = messages[-1].content
    if not isinstance(raw_content, str):
        return None
    content = raw_content
    query = _direct_arxiv_search_query(content)
    if query is None:
        return None

    runtime_state = state or {}
    if (
        runtime_state.get("plan")
        or runtime_state.get("capability_limitation")
        or runtime_state.get("pending_confirmation")
        or runtime_state.get("pending_skill_prerequisite")
        or runtime_state.get("tool_loop_count", 0) != 0
        or runtime_state.get("tool_executions")
        or runtime_state.get("loaded_skill_versions")
    ):
        return None

    # Catalog entries can require a normal skill-loading prerequisite. This
    # only disables the optimization; it never grants a tool or authorizes a
    # catalog instruction.
    lowered_content = content.casefold()
    catalog = runtime_state.get("project_skill_catalog")
    if isinstance(catalog, (list, tuple)):
        for item in catalog:
            name = item.get("name") if isinstance(item, Mapping) else None
            if isinstance(name, str) and name.strip():
                escaped_name = re.escape(name.strip())
                if re.search(
                    rf"(?<!\w){escaped_name}(?!\w)",
                    lowered_content,
                    re.IGNORECASE,
                ):
                    return None

    from src.services.agent.tool_registry import runtime_tool_descriptors

    if not any(
        descriptor.name == "search_arxiv"
        for descriptor in runtime_tool_descriptors(
            "research", runtime_state, TOOL_REGISTRY
        )
    ):
        return None

    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": f"direct_search_arxiv_{uuid4().hex}",
                "name": "search_arxiv",
                "args": {"query": query, "max_results": 5, "recency_days": 365},
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
        "Use search_arxiv with a clean topic query; its default searches up to "
        "five relevance-ranked papers from the last 365 days. Ingestion returns "
        "document_ids (plural UUIDs) and ingested_count; it does not return a "
        "single document_id. When an active project is in page context, ingest "
        "already attaches its documents. Use the exact returned document_ids "
        "for later document tools. For a named document, resolve its canonical "
        "UUID with search_documents before retrieving content."
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
    direct_search = _direct_arxiv_search_message(sanitized, state)
    if direct_search is not None:
        return {"messages": [direct_search]}

    static_prompt = _build_research_system_prompt()

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

    # Hoisted above the branch: _build_llm is used inside it, so importing
    # after would NameError.
    from src.services.agent.graph import (
        AGENT_LLM_TIMEOUT_SECONDS,
        _build_llm,
        _merge_run_config,
    )

    if use_lightweight_synthesis:
        from src.services.agent.llm_factory import (
            build_synthesis_llm,
            get_synthesis_model_name,
        )

        llm = build_synthesis_llm(max_tokens=4096, tool_calling=True)
        resolved_model = get_synthesis_model_name()
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
        from src.services.agent.llm_factory import resolve_chat_deployment

        resolved_model = resolve_chat_deployment(state.get("model") or None)
        logger.debug("research_llm_node: using main model for tool decision")
    from src.services.agent.runtime_context import render_dynamic_context

    dynamic_context = render_dynamic_context(
        state,
        config,
        resolved_model=resolved_model,
        branch="research",
        messages=sanitized,
    )
    messages = [
        SystemMessage(content=static_prompt),
        SystemMessage(content=dynamic_context),
        *sanitized,
    ]
    # See graph.llm_node for rationale on parallel_tool_calls=False.
    from src.services.agent._nodes_llm import (
        normalize_ai_content as _normalize_ai_content,
    )
    from src.services.agent._nodes_llm import tools_for_runtime_projection

    llm_with_tools = llm.bind_tools(
        tools_for_runtime_projection(state, branch="research"),
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
