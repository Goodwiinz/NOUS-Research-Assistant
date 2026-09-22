"""Writing Agent sub-graph.

Specialized for content creation, summarization, and bibliography tasks.
Tools: create_draft, revise_draft, create_project_note, export_bibliography,
       summarize_document, compare_documents

Shared machinery (routers, interrupt, forced synthesis, wiring) comes from
``subgraphs._factory.make_specialist_subgraph``; this module keeps only what
is genuinely writing-specific: the tool/ceiling constants, the system
prompt, and the LLM node with its grounded-synthesis path.
"""

import asyncio
import json
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from src.services.agent.graph import _sanitize_messages
from src.services.agent.observability import track_node_execution
from src.services.agent.state import AgentState
from src.services.agent.subgraphs._factory import make_specialist_subgraph
from src.services.agent.tool_registry import ToolPolicyTag
from src.services.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

WRITING_TOOLS = [
    descriptor.tool for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("writing")
]

WRITING_TOOL_NAMES_LIST = [t.name for t in WRITING_TOOLS]

# Named ceiling (preserves the previous hardcoded ``< 8``) so the
# forced-synthesis routing below and the bump in
# writing_force_synthesis_node stay in sync. Same pattern as research's
# MAX_RESEARCH_TOOL_LOOPS but an independent value — research deliberately
# lowered theirs to 5 after a runaway-fanout trace; this one has no such
# justification yet.
MAX_WRITING_TOOL_LOOPS = 8

# Destructive tools written by the writing subgraph. The main graph's
# DESTRUCTIVE_TOOLS gate only fires from the top-level interrupt_node and
# is bypassed once intent routes us into a subgraph, so the subgraph has
# to enforce HITL itself for any tool that mutates user data.
WRITING_DESTRUCTIVE_TOOLS = frozenset(
    descriptor.name
    for descriptor in TOOL_REGISTRY.descriptors_for_subgraph("writing")
    if ToolPolicyTag.DESTRUCTIVE in descriptor.policy_tags
)


def _build_writing_system_prompt() -> str:
    """Construct the writing subgraph system prompt with shared rules embedded.

    Driver protocol sourced from ``AGENTS_writing.md`` — see
    ``agents_md_loader``. Inline fallback for missing-file safety.

    Imported lazily to avoid circular imports with graph.py.
    """
    from src.services.agent.graph import SHARED_AGENT_RULES
    from src.services.agent.subgraphs.agents_md_loader import load_agents_md

    driver_protocol = load_agents_md("writing")
    if driver_protocol:
        return f"{driver_protocol}\n\n{SHARED_AGENT_RULES}"

    return (
        "You are a specialized Writing Agent focused on creating content, "
        "summarizing documents, and managing bibliographies.\n\n"
        f"{SHARED_AGENT_RULES}\n\n"
        "Write clearly and academically. Cite sources when available."
    )


@track_node_execution("writing_llm_node")
async def writing_llm_node(state: AgentState, config: RunnableConfig) -> dict:
    """Writing-specialized LLM node."""
    from src.core.config import get_settings
    from src.services.agent._nodes_llm import _attachment_status_part
    from src.services.agent.graph import (
        AGENT_LLM_TIMEOUT_SECONDS,
        _build_page_context_line,
    )

    sanitized = _sanitize_messages(state["messages"])
    trailing_tools = []
    for message in reversed(sanitized):
        if not isinstance(message, ToolMessage):
            break
        trailing_tools.append(message)
    parent_index = len(sanitized) - len(trailing_tools) - 1
    mixed_pending_create = False
    if (
        trailing_tools
        and parent_index >= 0
        and isinstance(sanitized[parent_index], AIMessage)
    ):
        parent = sanitized[parent_index]
        create_call_ids = {
            tool_call.get("id")
            for tool_call in getattr(parent, "tool_calls", [])
            if isinstance(tool_call, dict) and tool_call.get("name") == "create_draft"
        }
        pending_create_payload = None
        for tool_message in trailing_tools:
            if (
                tool_message.tool_call_id not in create_call_ids
                or getattr(tool_message, "status", "success") != "success"
            ):
                continue
            try:
                payload = json.loads(tool_message.content)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("status") == "pending":
                pending_create_payload = payload
                break
        if pending_create_payload is not None:
            parent_tool_calls = getattr(parent, "tool_calls", [])
            mixed_pending_create = any(
                isinstance(tool_call, dict) and tool_call.get("name") != "create_draft"
                for tool_call in parent_tool_calls
            )
            if not mixed_pending_create:
                message = pending_create_payload.get("message")
                task_id = pending_create_payload.get("task_id")
                if isinstance(message, str) and isinstance(task_id, str):
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    f"{message}\n\nStatus: pending\n"
                                    f"Task ID: {task_id}"
                                )
                            )
                        ]
                    }
    messages = [SystemMessage(content=_build_writing_system_prompt())]
    context_line = _build_page_context_line(state.get("page_context", {}))
    if context_line:
        messages.append(SystemMessage(content=context_line))
    attachment_status_prompt = _attachment_status_part(
        state.get("attachment_status", [])
    )
    if attachment_status_prompt:
        messages.append(SystemMessage(content=attachment_status_prompt))
    retrieved = state.get("retrieved_contexts", [])
    if retrieved:
        from src.services.agent._nodes_llm import _retrieval_context_part

        messages.append(
            SystemMessage(content=_retrieval_context_part(retrieved, sanitized))
        )
    from src.services.agent.runtime_snapshot import render_project_skill_catalog

    skill_catalog_prompt = render_project_skill_catalog(
        state.get("project_skill_catalog", [])
    )
    if skill_catalog_prompt:
        messages.append(SystemMessage(content=skill_catalog_prompt))
    if mixed_pending_create:
        messages.append(
            SystemMessage(
                content=(
                    "The create_draft result in this batch is pending and terminal for "
                    "this turn. Report that pending status together with substantive "
                    "completed tool results. Do not request or promise any more tools."
                )
            )
        )
    messages += sanitized

    # Post-tool synthesis turn → use the synthesis deployment. Mirrors
    # research_llm_node + main llm_node. Trace 019e191a showed gpt-5
    # spending 70s on prose synthesis after a tool result.
    settings = get_settings()
    last_user_query = next(
        (
            message.content
            for message in reversed(sanitized)
            if isinstance(message, HumanMessage) and isinstance(message.content, str)
        ),
        "",
    )
    from src.services.agent.planner import _is_grounded_summary_flow

    grounded_direct_synthesis = bool(
        retrieved and _is_grounded_summary_flow(last_user_query)
    )
    use_synthesis = bool(
        mixed_pending_create
        or (
            settings.AGENT_LIGHTWEIGHT_SYNTHESIS
            and sanitized
            and (isinstance(sanitized[-1], ToolMessage) or grounded_direct_synthesis)
        )
    )

    # Inject the planner's plan on the pre-tool pass so the executor follows
    # it instead of refusing. Skipped on synthesis turns — by then the tools
    # have already run and the plan would only re-trigger completed steps.
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
        logger.debug("writing_llm_node: using synthesis model after ToolMessage")
    else:
        # Tool-decision turn runs on the main deployment — see the note in
        # research_agent.research_llm_node. Multi-step function calling is the
        # job small tiers are worst at, and create_draft / create_project_note
        # are destructive, so a wrong call costs a confirmation round trip.
        llm = _build_llm(model_override=state.get("model") or None)
        logger.debug("writing_llm_node: using main model for tool decision")
    from src.services.agent._nodes_llm import (
        normalize_ai_content as _normalize_ai_content,
    )
    from src.services.agent._nodes_llm import tools_for_runtime_snapshot

    bound_tools = (
        []
        if grounded_direct_synthesis or mixed_pending_create
        else tools_for_runtime_snapshot(WRITING_TOOLS, state)
    )
    llm_with_tools = llm.bind_tools(
        bound_tools,
        parallel_tool_calls=settings.AGENT_PARALLEL_TOOL_CALLS,
    )

    invoke_config = _merge_run_config(
        config,
        run_name="writing_llm_node",
        tags=["intent:writing", "subgraph:writing"],
    )
    try:
        response = await asyncio.wait_for(
            llm_with_tools.ainvoke(messages, config=invoke_config),
            timeout=AGENT_LLM_TIMEOUT_SECONDS,
        )
        response = _normalize_ai_content(response)
    except asyncio.TimeoutError:
        logger.warning(
            "writing_llm_node: LLM exceeded %ds; emitting fallback",
            AGENT_LLM_TIMEOUT_SECONDS,
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        "The writing model took too long to respond. Please "
                        "try again with a shorter or more specific request."
                    ),
                ),
            ],
            "last_error": "writing_llm_timeout",
            "error_count": state.get("error_count", 0) + 1,
        }

    return {
        "messages": [response],
    }


_parts = make_specialist_subgraph(
    name="writing",
    llm_node=writing_llm_node,
    max_tool_loops=MAX_WRITING_TOOL_LOOPS,
    prompt_builder=_build_writing_system_prompt,
    synthesis_addendum=(
        "\n\n## Final synthesis turn\n"
        "You ran {count} tool calls and reached "
        "the per-turn tool budget. Do not request any more tools. Write a "
        "final answer drawn from the tool results already in this "
        "conversation: deliver the requested content (draft, note, summary, "
        "or comparison) as far as the gathered material allows, and state "
        "plainly any step you could not complete. Do NOT repeat or quote "
        "these instructions in your reply."
    ),
    synthesis_timeout_message=(
        "I gathered material but ran out of time composing the final "
        "text. Please ask me to finish from the results above."
    ),
    loop_exhaustion_intent="writing",
    invoke_tags=["intent:writing", "subgraph:writing"],
    reflection_intent_filter={"writing"},
    has_interrupt=True,
    # Late-bound so tests that monkeypatch this module's TOOL_REGISTRY
    # still steer routing/interrupt decisions.
    tool_registry_getter=lambda: TOOL_REGISTRY,
)

writing_should_continue = _parts.should_continue
writing_force_synthesis_node = _parts.force_synthesis_node
writing_interrupt_node = _parts.interrupt_node
writing_after_interrupt = _parts.after_interrupt
route_after_writing_tool_node = _parts.route_after_tool_node


def build_writing_subgraph() -> StateGraph:
    """Build the writing agent sub-graph.

    Flow:
      writing_planner_node -> writing_llm_node -> writing_should_continue ->
        | writing_interrupt_node -> writing_after_interrupt -> writing_tool_node
        | writing_tool_node -> writing_compactor_node -> writing_llm_node (loop)
        | writing_reflection_gate -> END (or revise -> writing_llm_node)
    """
    return _parts.build()
