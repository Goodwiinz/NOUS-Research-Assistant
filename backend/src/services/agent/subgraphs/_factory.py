"""Specialist-subgraph factory.

The research / writing / data subgraphs are one state machine instantiated
three times: identical node sets, identical edge wiring, and router /
interrupt / force-synthesis bodies that differed only in a name prefix, a
loop-ceiling constant, and prompt prose. This module generates those shared
parts from a per-subgraph config so the wiring exists exactly once.

What stays in the subgraph modules (genuinely different per subgraph):
the ``*_llm_node`` (research has the direct-arxiv fast path, writing has
grounded synthesis over ``retrieved_contexts``), the system-prompt builders,
and the module-level tool/ceiling constants.

Behavior contract: byte-identical to the pre-factory modules — pinned by
``tests/unit/services/test_agent_graph_topology.py``. ``tracked=False``
exists because data's nodes were never decorated with
``track_node_execution``; uniform decoration would be a (tiny) behavior
change and belongs in its own PR, not here.
"""

import asyncio
import json
import logging
from typing import (
    Awaitable,
    Callable,
    Hashable,
    Literal,
    NamedTuple,
    Optional,
    TypeVar,
    cast,
)

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from src.services.agent.compactor import make_compactor_node
from src.services.agent.graph import _TOOL_PLACEHOLDER_CONTENT, _sanitize_messages
from src.services.agent.observability import track_node_execution
from src.services.agent.planner import make_planner_node
from src.services.agent.reflection import make_reflection_gate
from src.services.agent.retrieval_provenance import render_retrieval_prompt
from src.services.agent.state import AgentState
from src.services.agent.tool_registry import ToolPolicyTag, ToolRegistry
from src.services.agent.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_NodeFn = Callable[[AgentState, RunnableConfig], Awaitable[dict]]
_RouteFn = Callable[[AgentState], str]


class SpecialistParts(NamedTuple):
    """Factory output: the generated shared parts of one specialist subgraph.

    Subgraph modules rebind these to their historical module-level names
    (``research_should_continue = _parts.should_continue`` …) so every
    existing import keeps resolving.
    """

    should_continue: _RouteFn
    route_after_tool_node: _RouteFn
    reflection_route: _RouteFn
    reflection_node: _NodeFn
    force_synthesis_node: _NodeFn
    interrupt_node: Optional[_NodeFn]
    after_interrupt: Optional[_RouteFn]
    build: Callable[[], StateGraph]


_F = TypeVar("_F", bound=Callable[..., object])


def _rename(fn: _F, name: str) -> _F:
    """Stamp the historical function name onto a generated closure.

    Cheap insurance for anything that reads ``__name__``/``__qualname__``
    (logging, tracing, repr in test failures).
    """
    fn.__name__ = name
    fn.__qualname__ = name
    return fn


def _execution_evidence_state(
    message: ToolMessage,
) -> Literal["completed", "pending", "none"]:
    """Classify whether a ToolMessage proves completion, start, or neither."""
    if getattr(message, "status", "success") != "success":
        return "none"
    content = str(message.content or "").strip()
    if not content:
        return "none"
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return "completed"
    if not isinstance(payload, dict):
        return "completed"
    status = str(payload.get("status") or "").lower()
    if payload.get("error") or status in {
        "cancelled",
        "denied",
        "error",
        "failed",
        "skipped",
        "timeout",
    }:
        return "none"
    if status == "pending":
        return "pending"
    return "completed"


def _is_execution_evidence(message: ToolMessage) -> bool:
    """Return whether a ToolMessage proves a tool started or completed."""
    return _execution_evidence_state(message) != "none"


# Emitted only when the breaker trips (error_count >= 3) AND the model's
# final AIMessage carries no content of its own (a pure tool-calls
# response). Extraction (agent_execution_service._run_agent_graph) needs
# SOME content-bearing AIMessage to persist as this turn's answer; without
# it, the walk falls through to whatever content-bearing AIMessage precedes
# this turn (a stale prior-turn answer, or nothing at all on turn one).
_BREAKER_DEGRADED_ANSWER = (
    "I hit repeated tool errors and I'm stopping here without finishing the "
    "remaining steps. Let me know how you'd like to proceed."
)


def _placeholder_tool_messages(tool_calls: list, content: str) -> list[ToolMessage]:
    """Answer every pending ``tool_call`` with a fixed-content placeholder.

    Shared by the HITL-deny and breaker-exit paths below: both abandon a
    tool_calls batch without ever reaching the tool node, so without this
    the trailing AIMessage's tool_calls stay dangling in the checkpoint
    until ``_sanitize_messages`` (graph.py) repairs it on the NEXT turn.
    That is too late for THIS turn's extraction walk
    (``agent_execution_service._run_agent_graph``, which skips
    content-less AIMessages hunting for the final answer) and for a
    same-turn reflection revise-loop back into the LLM, which needs a
    wire-valid history immediately.
    """
    return [ToolMessage(content=content, tool_call_id=tc["id"]) for tc in tool_calls]


def make_specialist_subgraph(
    *,
    name: str,
    llm_node: _NodeFn,
    max_tool_loops: int,
    prompt_builder: Callable[[], str],
    synthesis_addendum: str,
    synthesis_timeout_message: str,
    loop_exhaustion_intent: str,
    invoke_tags: list[str],
    reflection_intent_filter: set[str],
    has_interrupt: bool,
    tracked: bool = True,
    tool_registry_getter: Callable[[], ToolRegistry] = lambda: TOOL_REGISTRY,
) -> SpecialistParts:
    """Build the shared parts of a specialist subgraph.

    Args:
        name: Subgraph key — node-name prefix AND the ``TOOL_REGISTRY``
            subgraph key (``"research"`` / ``"writing"`` / ``"data"``).
        llm_node: The per-subgraph LLM node (stays hand-written).
        max_tool_loops: Tool-loop ceiling for ``should_continue`` and the
            past-ceiling bump in forced synthesis.
        prompt_builder: Zero-arg system-prompt builder (called lazily at
            node runtime, matching the original lazy-import pattern).
        synthesis_addendum: "## Final synthesis turn" prose with a
            ``{count}`` placeholder for the tool-loop count.
        synthesis_timeout_message: Canned AIMessage content when the
            forced-synthesis LLM call itself times out.
        loop_exhaustion_intent: First arg to ``record_loop_exhaustion``
            (``"research"`` / ``"writing"`` / ``"knowledge_graph"``).
        invoke_tags: LangSmith tags for the forced-synthesis invoke
            (``["intent:…", "subgraph:…"]`` — ``"phase:synthesis"`` is
            appended here).
        reflection_intent_filter: Passed to ``make_reflection_gate``.
        has_interrupt: Whether the subgraph has destructive tools and thus
            an HITL interrupt node (data does not).
        tracked: Decorate generated nodes with ``track_node_execution``.
            False for data — its nodes were historically undecorated.
        tool_registry_getter: Late-bound registry lookup used by the
            routing/interrupt closures. Subgraph modules pass
            ``lambda: TOOL_REGISTRY`` (their own module global) so tests
            that monkeypatch ``<subgraph_module>.TOOL_REGISTRY`` keep
            working — a closure over this module's import would freeze
            the patch point.
    """
    tool_names_list = [
        descriptor.name for descriptor in TOOL_REGISTRY.descriptors_for_subgraph(name)
    ]
    allowed_tool_names = set(tool_names_list) | {"load_project_skill"}

    llm = f"{name}_llm_node"
    tool = f"{name}_tool_node"
    interrupt_name = f"{name}_interrupt_node"
    compactor_name = f"{name}_compactor_node"
    force_synthesis = f"{name}_force_synthesis_node"
    reflection = f"{name}_reflection_gate"
    planner_name = f"{name}_planner_node"

    def should_continue(state: AgentState) -> str:
        """Decide whether to continue tool execution in this sub-graph."""
        if state.get("error_count", 0) >= 3:
            return reflection
        last = state["messages"][-1] if state["messages"] else None
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("tool_loop_count", 0) < max_tool_loops:
                if has_interrupt and any(
                    tool_registry_getter().has_policy_in_subgraph(
                        tc["name"], ToolPolicyTag.DESTRUCTIVE, name
                    )
                    for tc in last.tool_calls
                ):
                    return interrupt_name
                return tool
            # Loop ceiling tripped while the model still wants more tools.
            # If forced synthesis already ran once and the response STILL has
            # tool_calls (defective model), route to reflection — never loop
            # back into forced synthesis or we'd spin until checkpoint
            # timeout. Without forced synthesis the subgraph would exit with
            # an AIMessage whose tool_calls have no ToolMessages
            # (trace 019e1903).
            if state.get("_force_synthesis_fired"):
                return reflection
            return force_synthesis
        return reflection

    _rename(should_continue, f"{name}_should_continue")

    async def force_synthesis_node(state: AgentState, config: RunnableConfig) -> dict:
        """Final-answer LLM call when the tool-loop ceiling was hit.

        The model has fired ``max_tool_loops`` tool calls and still wants
        more. We strip the unanswered tool_calls and re-invoke the LLM with
        NO tools bound so it must produce text.

        Uses the main deployment because recovering a grounded partial answer
        from a long, capped trajectory requires more than routine prose
        rendering. Ordinary post-tool synthesis keeps using the cheaper tier.

        The "no more tools, synthesize now" directive is embedded into the
        system prompt (NOT a separate SystemMessage). Trace 019e190c showed
        gpt-5 echoed a second SystemMessage verbatim into its response when
        we appended the directive as its own message.

        Bumps tool_loop_count past the ceiling so ``should_continue``
        cannot route back here in a loop if the synthesis response somehow
        contains tool_calls (defensive — the directive forbids it).
        """
        from src.services.agent.observability import record_loop_exhaustion

        # Degraded-answer signal: reached this subgraph's tool-loop ceiling.
        record_loop_exhaustion(loop_exhaustion_intent, name)

        messages = list(state["messages"])

        # Drop the trailing AIMessage with unanswered tool_calls so the
        # model sees a clean conversational head when synthesizing.
        while (
            messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls
        ):
            messages.pop()

        sanitized = _sanitize_messages(messages)
        non_evidence_ids = [
            message.tool_call_id
            for message in sanitized
            if isinstance(message, ToolMessage)
            and _execution_evidence_state(message) == "none"
        ]
        pending_ids = [
            message.tool_call_id
            for message in sanitized
            if isinstance(message, ToolMessage)
            and _execution_evidence_state(message) == "pending"
        ]
        base_prompt = prompt_builder()
        addendum = synthesis_addendum.format(count=state.get("tool_loop_count", 0))
        limit_contract = (
            "\n\nThe unanswered tool request was not executed because the "
            "per-turn execution limit was reached. Do not claim it ran, infer "
            "its result, emit tool-call syntax, or promise to run it next. "
            "Only completed, non-placeholder ToolMessages with substantive "
            "results prove completion. Synthetic skipped placeholders and failed "
            "or error ToolMessages do not prove execution. A pending status proves "
            "the tool started but does not prove completion. Any requested tool or "
            "stage without execution evidence must be described as not executed. "
            + (
                "These tool call IDs are not execution evidence: "
                f"{json.dumps(non_evidence_ids)}. "
                if non_evidence_ids
                else ""
            )
            + (
                "These tool call IDs started asynchronously and remain pending: "
                f"{json.dumps(pending_ids)}; they must be reported as "
                "started/pending, not completed. "
                if pending_ids
                else ""
            )
            + "Answer the user's original request from completed tool results "
            "only. State that the execution limit stopped the remaining work "
            "and identify the last completed or verified result."
        )
        retrieval_prompt = render_retrieval_prompt(
            state.get("retrieved_contexts", []), sanitized
        )
        from src.services.agent._nodes_llm import _attachment_status_part

        attachment_status_prompt = _attachment_status_part(
            state.get("attachment_status", [])
        )
        attachment_status_suffix = (
            f"\n\n{attachment_status_prompt}" if attachment_status_prompt else ""
        )
        full = [
            SystemMessage(
                content=(
                    base_prompt
                    + attachment_status_suffix
                    + "\n\n"
                    + retrieval_prompt
                    + addendum
                    + limit_contract
                )
            )
        ] + sanitized

        # No bind_tools — force a pure text response.
        from src.services.agent.graph import (
            AGENT_LLM_TIMEOUT_SECONDS,
            _build_llm,
            _merge_run_config,
        )

        llm_client = _build_llm(state.get("model") or None)

        # _merge_run_config returns a plain dict; cast for the ainvoke
        # signature (mypy blocks on added files — the historical modules
        # carried the same untyped pattern).
        invoke_config = cast(
            RunnableConfig,
            _merge_run_config(
                config,
                run_name=force_synthesis,
                tags=[*invoke_tags, "phase:synthesis"],
            ),
        )
        try:
            response = await asyncio.wait_for(
                llm_client.ainvoke(full, config=invoke_config),
                timeout=AGENT_LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Error, not warning: the turn still completes "successfully"
            # with the canned fallback below, so this log line is the only
            # machine-visible signal that synthesis was degraded.
            logger.error(
                "%s: LLM exceeded %ds; emitting fallback "
                "(thread_id=%s, tool_loop_count=%s)",
                force_synthesis,
                AGENT_LLM_TIMEOUT_SECONDS,
                state.get("thread_id", ""),
                state.get("tool_loop_count", 0),
            )
            response = AIMessage(
                content=synthesis_timeout_message.format(
                    timeout=AGENT_LLM_TIMEOUT_SECONDS
                ),
            )

        return {
            "messages": [response],
            # Bump past ceiling so a defective response with stray
            # tool_calls cannot re-enter forced synthesis (would loop
            # infinitely).
            "tool_loop_count": max_tool_loops + 1,
            # Marker for routing: should_continue checks this flag before
            # sending back here.
            "_force_synthesis_fired": True,
        }

    if tracked:
        force_synthesis_node = track_node_execution(force_synthesis)(
            force_synthesis_node
        )
    _rename(force_synthesis_node, force_synthesis)

    interrupt_node: Optional[_NodeFn] = None
    after_interrupt: Optional[_RouteFn] = None
    if has_interrupt:

        async def interrupt_node_fn(state: AgentState, config: RunnableConfig) -> dict:
            """Pause for user confirmation before executing destructive tools."""
            last = state["messages"][-1] if state.get("messages") else None
            if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
                # Defensive guard — should_continue routes here only when
                # the last message is an AIMessage with tool_calls, but a
                # stale checkpoint or an out-of-order edge could violate
                # that contract.
                return {"pending_confirmation": {}, "user_confirmed": False}
            destructive_calls = [
                tc
                for tc in last.tool_calls
                if tool_registry_getter().has_policy_in_subgraph(
                    tc["name"], ToolPolicyTag.DESTRUCTIVE, name
                )
            ]
            tool_names = [tc["name"] for tc in destructive_calls]

            confirmation_details = {
                "pending_tools": tool_names,
                "tools": [
                    {"name": tc["name"], "args": tc["args"]} for tc in destructive_calls
                ],
                "message": f"Confirm: {', '.join(tool_names)}?",
            }
            from src.services.agent._nodes_tools import (
                hitl_log_raised,
                record_hitl_decision,
            )

            hitl_log_raised(config, destructive_calls)
            user_response = interrupt(confirmation_details)

            confirmed = bool(user_response and user_response.get("confirmed"))
            await record_hitl_decision(config, destructive_calls, confirmed)
            if confirmed:
                return {"pending_confirmation": {}, "user_confirmed": True}

            # should_continue sends the WHOLE batch to interrupt when ANY
            # member is destructive, so last.tool_calls (not just
            # destructive_calls) were all routed here unexecuted. Every one
            # needs an answer or the checkpoint keeps an OpenAI-invalid
            # dangling tool_calls AIMessage until _sanitize_messages repairs
            # it next turn.
            return {
                "messages": [
                    *_placeholder_tool_messages(
                        last.tool_calls, '{"status": "cancelled_by_user"}'
                    ),
                    AIMessage(
                        content=(
                            "Action cancelled by user. Let me know if you'd "
                            "like to proceed differently."
                        ),
                    ),
                ],
                "pending_confirmation": {},
                "user_confirmed": False,
            }

        if tracked:
            interrupt_node_fn = track_node_execution(interrupt_name)(interrupt_node_fn)
        interrupt_node = _rename(interrupt_node_fn, interrupt_name)

        def after_interrupt_fn(state: AgentState) -> str:
            if state.get("user_confirmed", False):
                return tool
            return reflection

        after_interrupt = _rename(after_interrupt_fn, f"{name}_after_interrupt")

    # The gate's own router is deliberately discarded: it returns
    # proceed/revise labels and emits metrics, while the historical
    # subgraph routers return node names / END and emit nothing. Adopting
    # it would change conditional-edge labels (topology snapshot) and
    # observability — out of scope for a behavior-preserving refactor.
    _raw_reflection_node, _canonical_route = make_reflection_gate(
        intent_filter=reflection_intent_filter,
    )

    async def reflection_entry_node(state: AgentState, config: RunnableConfig) -> dict:
        """Answer dangling tool_calls before delegating to the reflection gate.

        should_continue's error-ceiling branch (checked BEFORE the
        tool_calls check — see should_continue) routes straight here
        without ever visiting the tool node, so the trailing AIMessage can
        still carry unanswered tool_calls. Every OTHER edge into this node
        already leaves tool_calls answered or absent — force_synthesis
        never emits tool_calls (no bind_tools) and the interrupt-deny
        branch answers them itself (interrupt_node_fn) — so "last message
        is an AIMessage with tool_calls" unambiguously identifies the
        breaker path; no separate flag needs threading through state.
        Left unrepaired, the dangling call would survive to
        agent_execution_service._run_agent_graph's extraction walk, which
        skips content-less AIMessages and would persist a STALE prior-turn
        answer as this turn's result.
        """
        last = state["messages"][-1] if state.get("messages") else None
        extra_messages: list = []
        if isinstance(last, AIMessage) and last.tool_calls:
            extra_messages = _placeholder_tool_messages(
                last.tool_calls, _TOOL_PLACEHOLDER_CONTENT
            )
            if not (isinstance(last.content, str) and last.content.strip()):
                extra_messages.append(AIMessage(content=_BREAKER_DEGRADED_ANSWER))

        updates = await _raw_reflection_node(state, config)
        if extra_messages:
            updates = {**updates, "messages": extra_messages}
        return updates

    _rename(reflection_entry_node, reflection)

    def route_after_tool_node(state: AgentState) -> str:
        """Route from the tool node: skip the re-plan loop when the batch was fully deduped.

        A fully-deduped batch (every tool call was already executed this
        turn with identical args) carries zero new information. Routing
        through the compactor → LLM would burn another LLM round-trip
        (~8 s Azure p95) for no gain. Instead go straight to forced
        synthesis so it produces a final answer from the cached results
        already in state.
        """
        if state.get("tools_all_deduped"):
            return force_synthesis
        return compactor_name

    _rename(route_after_tool_node, f"route_after_{name}_tool_node")

    def reflection_route(state: AgentState) -> str:
        """Route after reflection: revise loops back to LLM, proceed exits."""
        from src.services.agent.reflection import ReflectionResult

        result: ReflectionResult | None = state.get(
            "_reflection_result"
        )  # type: ignore[arg-type]
        if result is None or result.passed or result.severity == "minor":
            return cast(str, END)
        if result.severity == "major" and state.get("reflection_count", 0) < 2:
            return llm
        return cast(str, END)

    _rename(reflection_route, f"_{name}_reflection_route")

    def build() -> StateGraph:
        """Build the specialist sub-graph.

        Flow:
          planner -> llm -> should_continue ->
            | [interrupt ->] tool -> compactor -> llm (loop)
            | reflection_gate -> END (or revise -> llm)
        """
        from src.services.agent.graph import make_filtered_tool_node

        filtered_tool = make_filtered_tool_node(allowed_tool_names)

        planner = make_planner_node(tool_names_list)
        compactor = make_compactor_node()

        graph = StateGraph(AgentState)

        graph.add_node(planner_name, planner)
        # langgraph's add_node overloads don't accept the Callable aliases
        # the factory closures are typed as (concrete defs infer fine) —
        # runtime shapes are identical to the pre-factory modules.
        graph.add_node(llm, llm_node)  # type: ignore[arg-type]
        graph.add_node(tool, filtered_tool)
        if has_interrupt:
            graph.add_node(interrupt_name, interrupt_node)  # type: ignore[arg-type]
        graph.add_node(compactor_name, compactor)
        graph.add_node(force_synthesis, force_synthesis_node)
        graph.add_node(reflection, reflection_entry_node)

        graph.set_entry_point(planner_name)
        graph.add_edge(planner_name, llm)

        llm_routes: dict[Hashable, str] = {
            tool: tool,
            force_synthesis: force_synthesis,
            reflection: reflection,
        }
        if has_interrupt:
            llm_routes[interrupt_name] = interrupt_name
        graph.add_conditional_edges(llm, should_continue, llm_routes)

        # Forced synthesis always goes to reflection (it produced a final
        # answer).
        graph.add_edge(force_synthesis, reflection)

        if has_interrupt:
            graph.add_conditional_edges(
                interrupt_name,
                after_interrupt,  # type: ignore[arg-type]
                {
                    tool: tool,
                    reflection: reflection,
                },
            )

        # When the entire tool batch was deduped (no fresh calls ran), skip
        # the wasted compactor → llm re-plan hop and go straight to forced
        # synthesis.
        graph.add_conditional_edges(
            tool,
            route_after_tool_node,
            {
                compactor_name: compactor_name,
                force_synthesis: force_synthesis,
            },
        )
        graph.add_edge(compactor_name, llm)

        graph.add_conditional_edges(
            reflection,
            reflection_route,
            {
                END: END,
                llm: llm,
            },
        )

        return graph

    _rename(build, f"build_{name}_subgraph")

    return SpecialistParts(
        should_continue=should_continue,
        route_after_tool_node=route_after_tool_node,
        reflection_route=reflection_route,
        reflection_node=reflection_entry_node,
        force_synthesis_node=force_synthesis_node,
        interrupt_node=interrupt_node,
        after_interrupt=after_interrupt,
        build=build,
    )
