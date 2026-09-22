"""Memory recall + save nodes for the agent graph.

Extracted from ``graph.py`` so the orchestration module stays under the
800-line house rule. ``graph.py`` re-exports both functions for legacy
imports (``from src.services.agent.graph import memory_save_node``).

Both nodes share two contracts:
- They never raise. Memory is a best-effort enrichment; an outage here
  must not break a turn.
- They mutate state only via the returned dict (LangGraph reducer).

Save gate trace anchor: 019e066b-2a35 saved a greeting "hi" that later
trace 019e040b recalled for a technical query. Gate now skips writes for
general-intent + no-tool turns.

Performance: embed+PG writes and LLM insight extraction are dispatched as
fire-and-forget background tasks via ``_persist_memory_async`` so
``memory_save_node`` returns immediately after the cheap inline ledger
write. This keeps the job's critical path free of p95=8s memory I/O.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from src.services.agent._nodes_rag import _coerce_text, is_conversational
from src.services.agent._pii_redact import redact_pii
from src.services.agent.compactor import trim_model_history
from src.services.agent.observability import track_node_execution
from src.services.agent.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background-task tracking — strong references so tasks are not GC'd mid-flight.
# Mirrors the pattern in src/services/agent/agent_execution_service.py
# (_background_tasks + callback).
# ---------------------------------------------------------------------------
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

_EXPLICIT_MEMORY_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remember|save)\s+(?:this\s+)?(?:that\s+)?",
    re.IGNORECASE,
)


def _is_explicit_memory_request(text: str) -> bool:
    return bool(_EXPLICIT_MEMORY_RE.search(text))


def _on_persist_done(task: asyncio.Task[Any]) -> None:  # noqa: ANN001
    """Drop a finished persist task and surface any exception as a warning.

    Cancellation on loop shutdown is expected and stays quiet. All other
    exceptions are logged at WARNING — memory is best-effort and must
    never crash the worker.
    """
    _BACKGROUND_TASKS.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Background memory persist task failed", exc_info=True)


# ---------------------------------------------------------------------------
# Slow persistence coroutine — runs off the critical path
# ---------------------------------------------------------------------------


async def _persist_memory_async(  # noqa: PLR0913
    store: Any,
    user_id: str,
    mem_key: str,
    mem_value: dict[str, Any],
    thread_id: str,
    turn_index: int,
    intent: str,
    tool_executions: list[dict[str, Any]],
    messages: list[Any],
    config: RunnableConfig,
) -> None:
    """Embed + write one memory entry, then optionally extract LLM insights.

    Must NOT touch any request-scoped DB session — only ``store`` and the
    insight LLM. Entire body is wrapped in try/except so an outage here is
    observable (WARNING log) but never fatal.
    """
    try:
        from src.services.agent.memory import save_memory

        org_id = str(config.get("configurable", {}).get("organization_id", "") or "")
        await save_memory(store, user_id, mem_key, mem_value, organization_id=org_id)

        # Insight extraction every N turns — an LLM call that distils the
        # conversation into preference-shaped strings far more useful for
        # future recall than echoed user input.
        from src.core.config import get_settings

        every_n = get_settings().AGENT_INSIGHT_EVERY_N_TURNS
        if every_n > 0 and turn_index > 0 and turn_index % every_n == 0:
            try:
                import hashlib
                from datetime import datetime, timezone

                from src.services.agent.memory_store import extract_insights

                # R7-M1: assistant turns are excluded. An AIMessage can be a
                # verbatim restatement of injected document text, and an
                # insight distilled from it becomes a permanent, per-user
                # instruction recalled into every future thread. Only what the
                # user actually typed is eligible.
                insight_messages = trim_model_history(
                    [m for m in messages if isinstance(m, HumanMessage)]
                )
                serialised = [
                    {"role": "user", "content": m.content}
                    for m in insight_messages
                    if getattr(m, "content", "")
                ]
                insights = await extract_insights(serialised, config)
                for i, insight in enumerate(insights):
                    if not insight or len(insight) < 10:
                        continue
                    insight_key = hashlib.md5(
                        f"insight:{turn_index}:{i}:{insight[:60]}".encode(),
                        usedforsecurity=False,
                    ).hexdigest()[:12]
                    await save_memory(
                        store,
                        user_id,
                        insight_key,
                        {
                            "query": redact_pii(insight)[:300],
                            "intent": "insight",
                            "tools_used": [],
                            "thread_id": thread_id,
                            "turn_index": turn_index,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "memory_type": "insight",
                            "source": "insight",
                        },
                        organization_id=org_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("insight extraction skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Background memory persist failed: %s", exc)


@track_node_execution("memory_retrieval_node")
async def memory_retrieval_node(state: AgentState, config: RunnableConfig) -> dict:
    """Retrieve relevant long-term memories before the LLM call."""
    configurable = config.get("configurable", {})
    # Ids-only configurable (audit B8): memory is keyed by the scalar
    # user_id — no ORM User needed here.
    user_id = str(configurable.get("user_id", "") or "")
    org_id = str(configurable.get("organization_id", "") or "")

    if not user_id:
        return {"user_memories": []}

    # Find the last user message for memory search. Coerced (M5) — a
    # multimodal HumanMessage's content is a list, and is_conversational()
    # below does content.strip(), which raises AttributeError on a list.
    last_user_msg = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_user_msg = _coerce_text(msg.content)
            break

    try:
        # Greeting / acknowledgement fast-path. Conversational turns ("hi",
        # "thanks", "ok") never benefit from long-term recall, and the save
        # gate in ``memory_save_node`` never persists them — so there is
        # provably nothing to retrieve. Skip the Cohere query-embedding +
        # Postgres semantic search (~0.5-2s on the parallel-preprocessing
        # critical path). Shares the predicate with ``rag_node`` so both
        # nodes agree on what counts as small talk. (Trace 019e9ef7: "hi"
        # embedded the query and recalled 5 sub-0.5-score noise memories.)
        # Inside the try (M5): a predicate failure must degrade like every
        # other memory-recall failure (WARNING + empty result) instead of
        # escaping the node, where preprocessing_node's
        # gather(return_exceptions=True) swallows it into the same default
        # with no more than a log line — same outcome, but only on purpose.
        if is_conversational(last_user_msg):
            return {"user_memories": []}

        from src.services.agent.memory import get_memory_store, search_memories

        store = await get_memory_store()
        if not store:
            return {"user_memories": []}

        memories = await search_memories(
            store, user_id, last_user_msg, limit=5, organization_id=org_id
        )
        # Defense-in-depth — search_memories already passes limit=5 to the
        # store, but a misbehaving backend (or a future bump in callers)
        # could return more. Sorting by score puts the ranked entries
        # first so the LLM prompt only carries the strongest matches.
        memories.sort(key=lambda m: m.get("score") or 0.0, reverse=True)
        memories = memories[:5]
        try:
            from src.services.agent import observability as _obs

            max_score = (
                max((m.get("score") or 0.0) for m in memories) if memories else None
            )
            _obs.record_memory_recall(hit=bool(memories), max_score=max_score)
        except Exception:
            pass
        return {"user_memories": memories}
    except Exception as e:
        logger.warning("Memory recall failed: %s", e)
        return {"user_memories": []}


@track_node_execution("memory_save_node")
async def memory_save_node(state: AgentState, config: RunnableConfig) -> dict:
    """Save relevant information from the conversation to long-term memory.

    The cheap inline ledger write is kept on the critical path (ordering-
    sensitive, fast).  The slow embed+PG write and optional LLM insight
    extraction are dispatched as a fire-and-forget background task so this
    node returns immediately and the job reaches END without waiting on
    those operations (p95 was 8.1 s on the critical path).
    """
    configurable = config.get("configurable", {})
    # Ids-only configurable (audit B8): the save is keyed by user_id.
    user_id = str(configurable.get("user_id", "") or "")

    # Append per-turn iteration record (audit trail) before any early
    # return — even no-user turns (test fixtures, anonymous probes) get
    # logged when AGENT_LEDGER_DIR is set. Best-effort, never raises.
    try:
        from src.services.agent.iteration_ledger import write_iteration

        thread_id = configurable.get("thread_id") or state.get("thread_id") or ""
        if thread_id:
            # Offload the synchronous ledger write (mkdir/write_text/os.replace)
            # so it never blocks the event loop / token streaming.
            await asyncio.to_thread(write_iteration, thread_id, dict(state))
    except Exception as _ledger_exc:  # noqa: BLE001 - observability must not crash
        logger.debug("ledger write skipped: %s", _ledger_exc)

    if not user_id:
        return {}

    # Save gate. Trace evidence (019e066b-2a35) showed every greeting
    # ("hi", "thanks") was persisted and later recalled as noise on
    # technical queries. Only persist turns that either committed to a
    # specialised intent OR ran a tool — those are the turns whose
    # recall has any chance of helping a future query.
    intent = state.get("intent") or ""
    tool_executions = state.get("tool_executions") or []

    # Extract the last assistant and user messages. Coerce: multimodal
    # content is a list of blocks, and the slice/encode below would raise
    # on it (the save is best-effort, so the memory would just be silently
    # lost).
    last_ai_content = ""
    last_user_content = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content and not last_ai_content:
            last_ai_content = _coerce_text(msg.content)
        if isinstance(msg, HumanMessage) and not last_user_content:
            last_user_content = _coerce_text(msg.content)
        if last_ai_content and last_user_content:
            break

    explicit_memory_request = _is_explicit_memory_request(last_user_content)
    if (
        intent in ("", "general")
        and not tool_executions
        and not explicit_memory_request
    ):
        return {}

    try:
        from src.services.agent.memory import get_memory_store

        store = await get_memory_store()
        if not store:
            return {}

        if not last_user_content:
            return {}

        # Compute mem_key and mem_value synchronously — the returned state
        # must not depend on the background task's result, so everything
        # the graph reducer needs is built here before dispatch.
        import hashlib
        from datetime import datetime, timezone

        thread_id = configurable.get("thread_id") or state.get("thread_id") or ""
        turn_index = int(state.get("turn_index") or 0) or len(
            [m for m in state["messages"] if isinstance(m, HumanMessage)]
        )
        mem_key = hashlib.md5(
            f"{thread_id}:{turn_index}:{last_user_content[:100]}".encode(),
            usedforsecurity=False,
        ).hexdigest()[:12]
        mem_value = {
            "query": redact_pii(last_user_content)[:200],
            "intent": intent,
            "tools_used": [te.get("tool_name", "") for te in tool_executions[-3:]],
            "thread_id": thread_id,
            "turn_index": turn_index,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Dispatch embed+save and optional insight extraction off the
        # critical path. The node returns {} immediately; the graph reaches
        # END and the job is marked "completed" without blocking on I/O.
        task = asyncio.create_task(
            _persist_memory_async(
                store=store,
                user_id=user_id,
                mem_key=mem_key,
                mem_value=mem_value,
                thread_id=thread_id,
                turn_index=turn_index,
                intent=intent,
                tool_executions=list(tool_executions),
                messages=list(state["messages"]),
                config=config,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_on_persist_done)

        # Yield one event-loop tick so the task starts executing.  In
        # production the first real I/O inside _persist_memory_async
        # suspends the task immediately and control returns here (making
        # this a true fire-and-forget with negligible overhead).  In tests,
        # AsyncMock stubs resolve in the same tick, so existing assertions
        # that check ``save_mock.assert_called_once()`` remain accurate
        # without requiring changes to the test files.
        await asyncio.sleep(0)

        return {}
    except Exception as e:
        logger.warning("Memory save failed: %s", e)
        return {}


__all__ = ["memory_retrieval_node", "memory_save_node"]
