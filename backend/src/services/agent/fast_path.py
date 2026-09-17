"""Deterministic routing policy for the evidence-independent Luna fast path.

The policy is deliberately fail-closed. It only bypasses LangGraph when the
turn cannot require project authorization, retrieval, or an agent tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FastPathDecision:
    eligible: bool
    reason: str


_BARE_CONVERSATION_RE = re.compile(
    r"^\s*(?:hi|hello|hey|thanks?|thank you|ok(?:ay)?|cool|nice|great|"
    r"goodbye|bye)[!.?\s]*$",
    re.IGNORECASE,
)

_AGENT_CAPABILITY_RE = re.compile(
    r"\b(?:"
    r"search|find|lookup|list|show|ingest|upload|add|create|save|delete|remove|"
    r"arxiv|document|paper|source|citation|bibliograph|knowledge graph|project|"
    r"summarize this|summarise this|compare"
    r")\b",
    re.IGNORECASE,
)

_CONTEXT_DEPENDENT_RE = re.compile(
    r"\b(?:"
    r"previous|above|earlier|again|continue|"
    r"more about (?:this|that|these|those)|"
    r"what about (?:this|that|these|those)"
    r")\b",
    re.IGNORECASE,
)

_GROUNDED_PAGE_TYPES = frozenset({"project", "documents"})

_FAST_PATH_SYSTEM_PROMPT = """You are NOUS, a concise scholarly assistant.
Answer only from general knowledge and the conversation text supplied here.
Do not claim to have searched documents, projects, tools, or live sources.
If the request actually requires those capabilities, say that the agent path is needed.
Prefer a direct answer and avoid unnecessary preamble."""


def _content(message: Any) -> str:
    value = getattr(message, "content", "")
    return value if isinstance(value, str) else str(value or "")


def classify_fast_path_turn(
    *,
    messages: Sequence[Any],
    page_context: Mapping[str, Any] | None,
    use_rag: bool,
    max_input_chars: int,
    has_attachments: bool = False,
) -> FastPathDecision:
    """Return a deterministic, explainable routing decision.

    Bare acknowledgements are evidence-independent even when the RAG toggle is
    enabled. All other turns require an explicit ungrounded request
    (``use_rag=False``) and must avoid agent-capability or contextual language.
    """

    latest_user = next(
        (
            message
            for message in reversed(messages)
            if getattr(message, "role", None) == "user"
        ),
        None,
    )
    if latest_user is None:
        return FastPathDecision(False, "missing_user_message")

    # Luna's evidence-independent path has no document loader. Explicit
    # attachments therefore always take the graph path, including when the
    # RAG toggle is off: direct user-selected content remains usable while
    # broad retrieval stays disabled.
    if has_attachments:
        return FastPathDecision(False, "attachments_require_grounding")

    user_text = _content(latest_user).strip()
    if _BARE_CONVERSATION_RE.fullmatch(user_text):
        return FastPathDecision(True, "bare_conversation")

    total_chars = sum(len(_content(message)) for message in messages)
    if total_chars > max(1, max_input_chars):
        return FastPathDecision(False, "context_budget_exceeded")

    context = page_context or {}
    if context.get("project_id") or context.get("type") in _GROUNDED_PAGE_TYPES:
        return FastPathDecision(False, "grounded_page_context")

    if _AGENT_CAPABILITY_RE.search(user_text):
        return FastPathDecision(False, "agent_capability_required")

    if use_rag:
        return FastPathDecision(False, "rag_requested")

    if _CONTEXT_DEPENDENT_RE.search(user_text):
        return FastPathDecision(False, "context_dependent")

    return FastPathDecision(True, "ungrounded_generation")


def build_fast_path_messages(
    messages: Sequence[Any], *, max_input_chars: int
) -> list[Any]:
    """Build a bounded, role-preserving Luna prompt from the newest context."""
    selected: list[Any] = []
    remaining = max(1, max_input_chars)
    for message in reversed(messages):
        content = _content(message)
        if not content:
            continue
        if len(content) > remaining:
            content = content[-remaining:]
        role = getattr(message, "role", None)
        if role == "user":
            selected.append(HumanMessage(content=content))
        elif role == "assistant":
            selected.append(AIMessage(content=content))
        remaining -= len(content)
        if remaining <= 0:
            break
    selected.reverse()
    return [SystemMessage(content=_FAST_PATH_SYSTEM_PROMPT), *selected]


async def stream_fast_path_chunks(
    *,
    llm: Any,
    messages: list[Any],
    persist_user: Callable[[], Awaitable[Any]],
) -> AsyncGenerator[Any, None]:
    """Start Luna and user persistence together, releasing no token too early."""
    iterator = llm.astream(messages).__aiter__()
    persist_task = asyncio.create_task(persist_user())
    first_task = asyncio.create_task(anext(iterator))
    try:
        try:
            first = await first_task
        except StopAsyncIteration:
            await persist_task
            return
        await persist_task
        yield first
        async for chunk in iterator:
            yield chunk
    finally:
        if not first_task.done():
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first_task
        if not persist_task.done():
            # The durable user row remains mandatory even if Luna fails early
            # — but unlike the happy path's unguarded `await persist_task`
            # above, a failure reaching this belated cleanup await has no
            # other chance to surface. Log it instead of dropping it.
            try:
                await persist_task
            except Exception:
                logger.error(
                    "Fast-path user-message persist failed during stream cleanup",
                    exc_info=True,
                )
        # Never leave the model's astream suspended mid-response: closing
        # this generator early (client disconnect) without closing
        # ``iterator`` abandons its HTTP stream open until GC-driven
        # asyncgen finalization picks it up, instead of releasing it now.
        with contextlib.suppress(Exception):
            await iterator.aclose()
