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


_GREETING_WORDS = r"hi|hello|hey|goodbye|bye"
_ACK_WORDS = r"thanks?|thank you|ok(?:ay)?|cool|nice|great|yes|yep|sure|no|nope|go ahead|proceed|do it|cancel"


def _bare(words: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*(?:{words})[!.?\s]*$", re.IGNORECASE)


_BARE_GREETING_RE = _bare(_GREETING_WORDS)
_BARE_ACK_RE = _bare(_ACK_WORDS)
_BARE_CONVERSATION_RE = _bare(f"{_GREETING_WORDS}|{_ACK_WORDS}")

# An assistant turn that asks or offers something: a bare ack after it may be
# the user accepting a proposed tool action (R8-A4, trace 019f33ca), which a
# tool-less Luna reply cannot carry out. Declarative offers count too ("I can
# ingest these for you.", "... if you'd like."); refusals ("I can't", "I
# cannot", "I could not") do not.
_AWAITS_REPLY_RE = re.compile(
    r"\?\s*$|\b(?:"
    r"shall i|should i|(?:do|would) you (?:like|want)|want me to|let me know|"
    r"confirm|proceed|i can(?!['\u2019]t)|i could(?! not)|happy to|"
    r"if you(?:['\u2019]d| would)? (?:like|want|prefer)"
    r")\b",
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

    Bare greetings are evidence-independent even when the RAG toggle is
    enabled. Bare acks ("ok", "thanks") are too, unless they may be accepting
    something: the previous assistant message asks/offers, or a grounded page
    is open. All other turns require an explicit ungrounded request
    (``use_rag=False``) and must avoid agent-capability or contextual language.
    """

    latest_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if getattr(messages[index], "role", None) == "user"
        ),
        None,
    )
    if latest_index is None:
        return FastPathDecision(False, "missing_user_message")
    latest_user = messages[latest_index]

    # Luna's evidence-independent path has no document loader. Explicit
    # attachments therefore always take the graph path, including when the
    # RAG toggle is off: direct user-selected content remains usable while
    # broad retrieval stays disabled.
    if has_attachments:
        return FastPathDecision(False, "attachments_require_grounding")

    total_chars = sum(len(_content(message)) for message in messages)
    if total_chars > max(1, max_input_chars):
        return FastPathDecision(False, "context_budget_exceeded")

    user_text = _content(latest_user).strip()
    if _BARE_GREETING_RE.fullmatch(user_text):
        return FastPathDecision(True, "bare_conversation")

    context = page_context or {}
    grounded_page = bool(context.get("project_id")) or (
        context.get("type") in _GROUNDED_PAGE_TYPES
    )
    if _BARE_ACK_RE.fullmatch(user_text):
        if grounded_page:
            return FastPathDecision(False, "grounded_page_context")
        previous_assistant = next(
            (
                message
                for message in reversed(messages[:latest_index])
                if getattr(message, "role", None) == "assistant"
            ),
            None,
        )
        # ponytail: a client that omits history hides the proposal and the
        # ack stays eligible; the web client sends its last 50 messages.
        if previous_assistant is not None and _AWAITS_REPLY_RE.search(
            _content(previous_assistant).strip()
        ):
            return FastPathDecision(False, "ack_may_accept_proposal")
        return FastPathDecision(True, "bare_conversation")

    if grounded_page:
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
