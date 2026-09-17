"""Intent classification + parallel preprocessing for the agent graph.

Extracted from ``graph.py`` so the orchestration module stays under the
800-line house rule. ``graph.py`` re-exports the public names so legacy
imports keep working.

Two execution paths live here:

* **``_classify_core``** — the bare LLM-with-keyword-fallback intent
  classifier. Also exposed as ``intent_classifier_node`` (the
  ``@track_node_execution``-wrapped variant) for callers that prefer
  per-node Prometheus metrics.
* **``preprocessing_node``** — kicks off RAG retrieval, classification,
  and memory recall in parallel via ``asyncio.gather`` and resets per-
  turn ephemeral state so values carried by the checkpointer cannot
  poison the new turn.

``route_by_intent`` lives here too because it is the conditional edge
fired immediately after ``preprocessing_node``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.services.agent._nodes_memory import memory_retrieval_node
from src.services.agent._nodes_rag import _coerce_text, rag_node
from src.services.agent.observability import (
    record_node_duration,
    tag_trace_intent,
    track_node_execution,
)
from src.services.agent.state import AgentState

logger = logging.getLogger(__name__)

MAX_CHECKPOINT_USER_TURNS = 20
# ponytail: recent-turn window only; add durable summaries if long-thread recall
# quality regresses beyond the existing memory/insight path.


def _prune_checkpoint_history(messages: List[Any]) -> List[RemoveMessage]:
    """Delete complete turns older than the recent checkpoint window."""
    user_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage)
    ]
    if len(user_indexes) <= MAX_CHECKPOINT_USER_TURNS:
        return []
    cutoff = user_indexes[-MAX_CHECKPOINT_USER_TURNS]
    return [
        RemoveMessage(id=message.id)
        for message in messages[:cutoff]
        if getattr(message, "id", None)
    ]


def _extract_prior_tool(messages: List[Any]) -> Optional[Dict[str, Any]]:
    """Return bounded outcomes from the latest batch, not just its first call."""
    for ai_idx in range(len(messages) - 1, -1, -1):
        msg = messages[ai_idx]
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue
        results = {}
        # Only match within this batch; never reuse a stale result with the
        # same call ID from an earlier/later assistant or user turn.
        for follow in messages[ai_idx + 1 :]:
            if isinstance(follow, (AIMessage, HumanMessage)):
                break
            if isinstance(follow, ToolMessage):
                results[follow.tool_call_id] = follow
        calls = []
        for call in tool_calls[-8:]:
            result = results.get(call.get("id"))
            calls.append(
                {
                    "name": call.get("name", ""),
                    "result": result.content if result is not None else "",
                    "status": getattr(result, "status", "missing"),
                }
            )
        return {"calls": calls, "truncated": len(tool_calls) > 8}
    return None


async def _classify_core(state: AgentState, config: RunnableConfig) -> dict:
    """Classify user intent using LLM with keyword fallback.

    Used directly inside ``preprocessing_node`` (which composes its own
    parallel tracking) and indirectly via ``intent_classifier_node``,
    which wraps this with ``@track_node_execution`` for callers that
    invoke it as a graph node.
    """
    from src.services.agent.classifier import classify_intent_with_fallback

    # Coerce: multimodal content is a list of blocks; the classifier's
    # keyword path calls .lower()/.split() on it, which would raise and be
    # swallowed by the gather(return_exceptions=True) in preprocessing_node,
    # silently defaulting the intent to "general".
    last_user_msg = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_user_msg = _coerce_text(msg.content)
            break

    if not last_user_msg:
        return {"intent": "general", "intent_confidence": 0.0}

    previous_turn = ""
    found_user = False
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            if found_user:
                break
            found_user = True
            continue
        if found_user and isinstance(msg, AIMessage) and msg.content:
            previous_turn = _coerce_text(msg.content)
            break

    prior_tool = _extract_prior_tool(state["messages"])
    page_context = config.get("configurable", {}).get("page_context", {})
    result = await classify_intent_with_fallback(
        query=last_user_msg,
        page_context=page_context,
        previous_turn=previous_turn,
        prior_tool=prior_tool,
    )
    logger.debug(
        "Classified intent: %s (confidence=%.2f, source=%s)",
        result.intent,
        result.confidence,
        result.source,
    )
    try:
        from src.services.agent.observability import (
            record_classifier_source,
            record_intent_confidence,
        )

        record_classifier_source(source=result.source, intent=result.intent)
        record_intent_confidence(source=result.source, confidence=result.confidence)
    except Exception:
        pass
    return {"intent": result.intent, "intent_confidence": result.confidence}


# ``intent_classifier_node`` is the tracked graph-node version of
# ``_classify_core``. Production wiring uses ``_classify_core`` directly via
# ``preprocessing_node``; the tracked alias is kept for tests and any future
# wiring that wants the per-node Prometheus metrics.
intent_classifier_node = track_node_execution("intent_classifier_node")(_classify_core)


async def _timed_subtask(label: str, coro):
    """Await ``coro``, logging + recording its wall-clock under
    ``preprocessing:<label>`` in the shared node histogram.

    The three ``preprocessing_node`` subtasks run concurrently and otherwise
    emit no traced run, so this is the only place their individual latency is
    visible. Re-raises so ``gather(return_exceptions=True)`` captures failures
    exactly as before — pure instrumentation, no behaviour change.
    """
    t0 = time.monotonic()
    status = "success"
    try:
        return await coro
    except BaseException:
        status = "error"
        raise
    finally:
        dur = time.monotonic() - t0
        logger.info("preprocessing subtask %s: %.3fs (%s)", label, dur, status)
        record_node_duration(f"preprocessing:{label}", status, dur)


@track_node_execution("preprocessing_node")
async def preprocessing_node(state: AgentState, config: RunnableConfig) -> dict:
    """Run RAG retrieval, intent classification, and memory retrieval in parallel.

    Also resets per-turn ephemeral state (``plan``, ``reflection_count``,
    ``_reflection_result``, ``tool_loop_count``, ``error_count``,
    ``user_confirmed``) so that values carried over from the previous turn
    via the checkpointer cannot:

    - block the planner from re-planning against the new query (H-11);
    - trigger a spurious revision at the start of the next turn from a
      stale ``_reflection_result`` (H-01 / M-06);
    - bypass the destructive-tool HITL gate via a stale ``user_confirmed``
      flag (H-17);
    - count this turn's first error against last turn's accumulated
      ``error_count``.
    """
    results = await asyncio.gather(
        _timed_subtask("rag", rag_node(state, config)),
        _timed_subtask("classify", _classify_core(state, config)),
        _timed_subtask("memory", memory_retrieval_node(state, config)),
        return_exceptions=True,
    )

    defaults = [
        {"retrieved_contexts": []},
        {"intent": "general", "intent_confidence": 0.0},
        {"user_memories": []},
    ]
    merged: dict = {
        # Per-turn resets — must come BEFORE merging subtask results so a
        # subtask that explicitly sets one of these keys still wins.
        "plan": [],
        "plan_reasoning": "",
        "reflection_count": 0,
        "_reflection_result": None,
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "last_error_info": {},
        "user_confirmed": False,
        "pending_confirmation": {},
        "turn_index": int(state.get("turn_index") or 0) + 1,
        # compaction_count is documented "reset per turn" (state.py) but was
        # omitted, so it accumulated for the life of the thread. _force_-
        # synthesis_fired is last-write-wins and, left True from a prior turn,
        # permanently disables forced synthesis (empty-content + orphan
        # tool_calls "no response" exit). Reset both per turn.
        "compaction_count": 0,
        "_force_synthesis_fired": False,
    }
    removals = _prune_checkpoint_history(list(state.get("messages", [])))
    if removals:
        merged["messages"] = removals
    for result, default in zip(results, defaults):
        if isinstance(result, asyncio.CancelledError):
            # CancelledError is BaseException (not Exception) since 3.8, so the
            # check below would skip it and merged.update(<exc>) would raise
            # TypeError. The caller aborted — propagate, don't swallow (house
            # pattern: jobs.py / classifier.py / error_recovery.py).
            raise result
        if isinstance(result, Exception):
            logger.warning("Preprocessing subtask failed: %s", result)
            merged.update(default)
        else:
            merged.update(result)

    # Tag the LangSmith ROOT run with the classified intent so top-level
    # traces are filterable by intent in the UI.  Best-effort — never raises.
    tag_trace_intent(merged.get("intent", ""))

    return merged


def route_by_intent(state: AgentState) -> str:
    """Route to the appropriate sub-graph based on classified intent."""
    intent = state.get("intent", "general")
    if intent == "research":
        return "research_subgraph"
    if intent == "writing":
        return "writing_subgraph"
    if intent == "knowledge_graph":
        return "data_subgraph"
    return "llm_node"


__all__ = [
    "_extract_prior_tool",
    "_classify_core",
    "intent_classifier_node",
    "_prune_checkpoint_history",
    "preprocessing_node",
    "route_by_intent",
]
