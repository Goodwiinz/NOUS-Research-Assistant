"""Context compactor for the agent graph.

Summarises older ToolMessage payloads using a lightweight LLM so that the
conversation stays within the context window while preserving all
referenced IDs (UUIDs and arXiv IDs).
"""

import logging
import re
from typing import Any, Callable, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.runnables import RunnableConfig

from src.services.agent.llm_factory import build_lightweight_llm
from src.services.agent.state import AgentState
from src.services.agent.trace_metadata import internal_llm_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ARXIV_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")

_COMPACTED_PREFIX = "[Compacted]"

# Synthetic placeholder content emitted by ``_sanitize_messages`` when an
# AIMessage tool_call has no matching ToolMessage. These placeholders are
# already minimal (~25 chars) and contain no useful content to summarise,
# so they're filtered out of compaction candidates.
_PLACEHOLDER_CONTENTS: frozenset[str] = frozenset({'{"status": "skipped"}'})

# Cap the LLM compaction summary length. Generous enough that summaries do not
# get cut mid-sentence, but small enough to deliver meaningful token savings.
_COMPACT_MAX_TOKENS = 320

# Cap what we hand the compactor LLM. Tool results are attacker-influenced
# (document text, web pages); without a bound one oversized result can blow
# the lightweight model's context and burn the budget. Well above any real
# tool payload we want summarised faithfully.
_COMPACT_INPUT_MAX_CHARS = 12_000

# Marker for "this ToolMessage is already a compaction summary". Tracked in
# additional_kwargs rather than by the display prefix, which any tool result
# (hence any document the tool read) could spoof to exempt itself forever.
_COMPACTED_FLAG = "compacted"

# Maximum number of compaction rounds per conversation. Without a cap,
# should_compact re-fires every turn once the token threshold is crossed,
# burning LLM calls on already-compacted context.
_MAX_COMPACTION_ROUNDS = 3

# Keep every model-bound path below a predictable context ceiling even after
# tool compaction stops. The latest complete turns win; durable checkpoints
# are pruned separately so this is a request-time safety net, not storage GC.
MODEL_HISTORY_TOKEN_BUDGET = 24_000
_FALLBACK_HUMAN_CHARS = 32_000


def trim_model_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return the newest complete user-led turns that fit the model budget."""
    trimmed = trim_messages(
        messages,
        max_tokens=MODEL_HISTORY_TOKEN_BUDGET,
        token_counter="approximate",
        strategy="last",
        allow_partial=False,
    )
    if len(trimmed) == len(messages) or not any(
        isinstance(message, HumanMessage) for message in messages
    ):
        return trimmed
    human_start = trim_messages(
        messages,
        max_tokens=MODEL_HISTORY_TOKEN_BUDGET,
        token_counter="approximate",
        strategy="last",
        start_on="human",
        allow_partial=False,
    )
    if human_start:
        return human_start

    # One tool-heavy turn can exceed the whole budget. Never invoke the model
    # with only its system prompt, and never return an orphan ToolMessage.
    latest_human = next(
        message for message in reversed(messages) if isinstance(message, HumanMessage)
    )
    if (
        isinstance(latest_human.content, str)
        and len(latest_human.content) > _FALLBACK_HUMAN_CHARS
    ):
        latest_human = latest_human.model_copy(
            update={"content": latest_human.content[-_FALLBACK_HUMAN_CHARS:]}
        )
    return [latest_human]


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tool_message_tokens(messages: list[ToolMessage]) -> int:
    """Estimate total tokens across all ToolMessages.

    Uses a simple heuristic of ``len(content) // 4`` per message.
    """
    total = 0
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else ""
        total += len(content) // 4
    return total


# ---------------------------------------------------------------------------
# ID extraction
# ---------------------------------------------------------------------------


def extract_ids(text: str) -> tuple[set[str], set[str]]:
    """Extract UUIDs and arXiv IDs from *text*.

    Returns ``(uuids, arxiv_ids)`` as two sets of strings.
    """
    uuids = set(_UUID_RE.findall(text))
    arxiv_ids = set(_ARXIV_RE.findall(text))
    return uuids, arxiv_ids


# ---------------------------------------------------------------------------
# Compaction predicate
# ---------------------------------------------------------------------------


def should_compact(
    messages: list[BaseMessage],
    compaction_count: int,
    threshold: int = 8000,
) -> bool:
    """Return ``True`` if estimated tool-message tokens exceed *threshold*.

    Also returns ``False`` once ``compaction_count`` reaches
    ``_MAX_COMPACTION_ROUNDS`` to prevent re-compacting already-compacted
    context on every subsequent turn.
    """
    if compaction_count >= _MAX_COMPACTION_ROUNDS:
        return False
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    return estimate_tool_message_tokens(tool_msgs) > threshold


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def find_compaction_candidates(messages: list[BaseMessage]) -> list[ToolMessage]:
    """Find ToolMessages that are safe to compact.

    The most recent AIMessage with ``tool_calls`` and its corresponding
    ToolMessages are *protected* (never compacted).  All older ToolMessages
    that are not already flagged as compaction output are returned as
    candidates. Candidacy is decided by ``additional_kwargs["compacted"]``,
    never by the ``[Compacted]`` display prefix — that prefix is just text a
    tool result could contain.
    """
    # Walk backward to find the most recent AIMessage with tool_calls.
    latest_ai_idx: int | None = None
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            latest_ai_idx = idx
            break

    if latest_ai_idx is None:
        return []

    # Collect tool_call_ids for the latest protected batch.
    latest_ai: AIMessage = messages[latest_ai_idx]  # type: ignore[assignment]
    protected_tc_ids: set[str] = {tc["id"] for tc in latest_ai.tool_calls}

    candidates: list[ToolMessage] = []
    for msg in messages[:latest_ai_idx]:
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if (getattr(msg, "additional_kwargs", None) or {}).get(_COMPACTED_FLAG):
            continue
        if content.strip() in _PLACEHOLDER_CONTENTS:
            # Synthetic skipped-tool placeholder — nothing to summarise.
            continue
        if msg.tool_call_id in protected_tc_ids:
            continue
        candidates.append(msg)

    return candidates


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_and_fix_compacted(
    compacted: str,
    original_ids: tuple[set[str], set[str]],
) -> str:
    """Ensure all IDs from *original_ids* appear in *compacted*.

    If any are missing, append a ``Referenced IDs:`` line listing them.
    """
    uuids, arxiv_ids = original_ids
    all_original = uuids | arxiv_ids
    if not all_original:
        return compacted

    present_uuids, present_arxiv = extract_ids(compacted)
    present = present_uuids | present_arxiv
    missing = all_original - present

    if not missing:
        return compacted

    return f"{compacted}\nReferenced IDs: {', '.join(sorted(missing))}"


# ---------------------------------------------------------------------------
# LLM helper — delegates to shared factory
# ---------------------------------------------------------------------------

_COMPACTOR_LLM = None


def _build_compactor_llm():
    """Return a cached lightweight LLM for compaction summaries.

    The if-None-then-build below is not locked, but build_lightweight_llm()
    caches by args, so a concurrent double-build just returns the same factory
    instance — at worst one redundant dict lookup, never a duplicate client.
    """
    global _COMPACTOR_LLM
    if _COMPACTOR_LLM is not None:
        return _COMPACTOR_LLM
    _COMPACTOR_LLM = build_lightweight_llm(max_tokens=_COMPACT_MAX_TOKENS)
    return _COMPACTOR_LLM


# ---------------------------------------------------------------------------
# Compaction logic
# ---------------------------------------------------------------------------

_COMPACTION_SYSTEM_PROMPT = (
    "You are a context compactor. Summarise the following tool output in "
    "under 200 tokens. Preserve ALL document IDs (UUIDs), arXiv IDs, "
    "titles, status codes, and key facts. Omit verbose formatting."
)


_COMPACTION_PARALLELISM = 5


async def compact_messages(
    candidates: list[ToolMessage],
    config: RunnableConfig,
) -> list[ToolMessage]:
    """Compact each candidate ToolMessage via the lightweight LLM.

    For each candidate:
    1. Extract IDs from original content
    2. Call lightweight LLM to summarise (failure is isolated per-item — a
       single LLM error skips that candidate but allows the rest to
       proceed)
    3. Validate/fix compacted output
    4. Return new ToolMessage with ``[Compacted]`` prefix and the same
       ``id`` as the original (so the LangGraph reducer can replace it).

    Per-message compaction calls run concurrently bounded by
    ``_COMPACTION_PARALLELISM`` to avoid burst-rate-limiting.
    """
    import asyncio

    from langchain_core.messages import HumanMessage, SystemMessage

    llm = _build_compactor_llm()
    semaphore = asyncio.Semaphore(_COMPACTION_PARALLELISM)

    def _bound_for_summary(text: str) -> str:
        # Head + tail sample rather than a prefix: a do_kb_retrieve result
        # lists chunks in order, so a pure prefix would drop the last chunks
        # from the summary for good (review on #1595).
        if len(text) <= _COMPACT_INPUT_MAX_CHARS:
            return text
        head = _COMPACT_INPUT_MAX_CHARS * 2 // 3
        tail = _COMPACT_INPUT_MAX_CHARS - head
        omitted = len(text) - head - tail
        return f"{text[:head]}\n…[{omitted} chars omitted]…\n{text[-tail:]}"

    async def _compact_one(msg: ToolMessage) -> Optional[ToolMessage]:
        original_content = msg.content if isinstance(msg.content, str) else ""
        original_ids = extract_ids(original_content)
        llm_input = _bound_for_summary(original_content)

        try:
            async with semaphore:
                response = await llm.ainvoke(
                    [
                        SystemMessage(content=_COMPACTION_SYSTEM_PROMPT),
                        HumanMessage(content=llm_input),
                    ],
                    config=internal_llm_config(config),
                )
        except asyncio.CancelledError:
            # User abort / shutdown — propagate, never swallow into a
            # "leave intact" no-op (house pattern, see classifier).
            raise
        except Exception:
            logger.warning(
                "Compaction LLM call failed for tool_call_id=%s; "
                "leaving original message intact",
                msg.tool_call_id,
                exc_info=True,
            )
            return None

        summary = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
        summary = validate_and_fix_compacted(summary, original_ids)

        return ToolMessage(
            content=f"{_COMPACTED_PREFIX} {summary}",
            tool_call_id=msg.tool_call_id,
            id=msg.id,
            additional_kwargs={
                **(getattr(msg, "additional_kwargs", None) or {}),
                _COMPACTED_FLAG: True,
            },
            # Preserve the original status — compaction must not reset an
            # error message to LangChain's default status="success".
            status=getattr(msg, "status", "success") or "success",
        )

    results = await asyncio.gather(*[_compact_one(msg) for msg in candidates])
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Graph node factory
# ---------------------------------------------------------------------------


def make_compactor_node() -> Callable:
    """Return a graph node function for context compaction.

    The returned async function:
    1. Checks ``should_compact()`` — passes through if no.
    2. Finds compaction candidates.
    3. Compacts them via LLM.
    4. Returns replacement messages and bumped ``compaction_count``.
    """

    async def compactor_node(
        state: AgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        messages: list[BaseMessage] = state["messages"]
        compaction_count: int = state.get("compaction_count", 0)  # type: ignore[arg-type]

        if not should_compact(messages, compaction_count):
            return {}

        candidates = find_compaction_candidates(messages)
        if not candidates:
            return {}

        compacted = await compact_messages(candidates, config)

        if not compacted:
            return {}

        # The ``messages`` channel uses the ``add_messages`` reducer which
        # APPENDS new messages by default and only replaces existing ones
        # when the new message's ``id`` matches an existing message's ``id``.
        # Returning the full reconstructed list here would have doubled the
        # history (every untouched message is appended again — and the
        # `add_messages` dedup-by-id only catches messages with explicit
        # IDs). The correct pattern is to emit ``RemoveMessage`` deletions
        # for each compacted candidate followed by the replacement
        # ToolMessages.
        compacted_ids = {m.id for m in compacted if m.id is not None}
        removes: list[BaseMessage] = [
            RemoveMessage(id=msg.id)
            for msg in candidates
            if msg.id is not None and msg.id in compacted_ids
        ]

        return {
            "messages": removes + list(compacted),
            "compaction_count": compaction_count + 1,
        }

    return compactor_node
