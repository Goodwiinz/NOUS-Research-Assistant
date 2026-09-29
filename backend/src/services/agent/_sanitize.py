"""Prompt-field sanitization utilities.

Neutralises user-controlled text before it is interpolated into LLM system
prompts, providing a minimum defence against prompt-injection attacks.

Public API
----------
_PROMPT_FIELD_MAX_CHARS : int
    Maximum character length for any sanitised field.
_sanitize_prompt_field(value: str) -> str
    Truncate, escape braces, and collapse newlines in *value*.
sanitize_page_context(ctx) -> dict
    Recursively apply the above to every string leaf of a client-supplied
    page context (keys included), depth- and width-limited.
wrap_untrusted(text, source, max_chars) -> str
    Fence third-party content (documents, memories) so the model can tell
    data from instructions.
"""

import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

# Every code point that starts a new line for a model tokenizer or a
# markdown renderer — not just CR/LF (Codex review on #1594): NEL, VT, FF,
# LINE SEPARATOR, PARAGRAPH SEPARATOR.
_LINE_BREAK_RE = re.compile(r"[\r\n\x0b\x0c\x85\u2028\u2029]")

# Maximum length (chars) for any user-supplied string interpolated into a
# classifier system prompt.  Truncating + neutralising braces/newlines is the
# minimum defence against prompt-injection via previous_turn / prior_tool /
# page_context.  Longer values are clipped with an ellipsis.
_PROMPT_FIELD_MAX_CHARS = 400

# Synthetic placeholder emitted when a terminal path cannot execute an
# outstanding call. The compactor intentionally ignores this exact payload.
_TOOL_PLACEHOLDER_CONTENT = '{"status": "skipped"}'
_TOOL_ERROR_DEGRADED_ANSWER = (
    "I hit repeated tool errors and I'm stopping here without finishing the "
    "remaining steps. Let me know how you'd like to proceed."
)


def _sanitize_prompt_field(value: str) -> str:
    """Neutralise user-controlled text before interpolating into a prompt.

    Strips characters that could either break the ``str.format()`` call
    (``{`` / ``}``) or attempt to escape the surrounding section header in
    the system prompt (newlines, markdown headings). Truncates to
    ``_PROMPT_FIELD_MAX_CHARS`` so an attacker cannot drown the actual
    classification prompt by stuffing thousands of tokens through one of
    the dynamic context fields.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) > _PROMPT_FIELD_MAX_CHARS:
        text = text[:_PROMPT_FIELD_MAX_CHARS] + "..."
    # ``str.format`` interprets ``{`` / ``}`` as field delimiters — escape
    # them to literal braces.
    text = text.replace("{", "{{").replace("}", "}}")
    # Collapse newlines so dynamic content cannot start a new markdown
    # heading and visually impersonate prompt sections.
    text = _LINE_BREAK_RE.sub(" ", text)
    return text


# ---------------------------------------------------------------------------
# Page context (R7-H1)
# ---------------------------------------------------------------------------

# ``PageContextRequest.metadata`` is an unconstrained ``Dict[str, Any]``, so
# the client can nest arbitrary structures. Depth/width caps stop a nested
# blob from flooding the prompt or blowing the recursion stack.
_PAGE_CONTEXT_MAX_DEPTH = 4
_PAGE_CONTEXT_MAX_KEYS = 20
# Keys the execution service reads for control flow, not for prompt text.
# They survive the width cap so a metadata flood cannot knock out project
# binding (Codex review on #1594).
_PAGE_CONTEXT_KEEP_KEYS = frozenset({"workspace_thread_id"})


def _sanitize_page_value(text: str) -> str:
    """Idempotent leaf sanitizer for page context: cap + line collapse only.

    No brace doubling — page context is rendered through f-strings, never
    ``str.format``, so escaping is unnecessary and would compound on every
    re-entry (resume path, planner). Applying this twice yields the same
    string, which is what makes a sanitize-at-ingress design safe.
    """
    if not text:
        return ""
    text = str(text)
    if len(text) > _PROMPT_FIELD_MAX_CHARS:
        text = text[:_PROMPT_FIELD_MAX_CHARS] + "..."
    return _LINE_BREAK_RE.sub(" ", text)


def _clean(value, depth: int):
    if isinstance(value, str):
        return _sanitize_page_value(value)
    if isinstance(value, dict):
        if depth >= _PAGE_CONTEXT_MAX_DEPTH:
            return {}
        items = list(value.items())
        kept = [kv for kv in items if kv[0] in _PAGE_CONTEXT_KEEP_KEYS]
        rest = [kv for kv in items if kv[0] not in _PAGE_CONTEXT_KEEP_KEYS]
        return {
            _clean(k, depth + 1) if isinstance(k, str) else k: _clean(v, depth + 1)
            for k, v in kept + rest[:_PAGE_CONTEXT_MAX_KEYS]
        }
    if isinstance(value, (list, tuple)):
        if depth >= _PAGE_CONTEXT_MAX_DEPTH:
            return []
        return [_clean(v, depth + 1) for v in list(value)[:_PAGE_CONTEXT_MAX_KEYS]]
    # int / float / bool / None pass through untouched.
    return value


def sanitize_page_context(ctx) -> dict:
    """Sanitize every string leaf of a client-supplied page context.

    Applied once at ingress (``_page_context_to_dict``) so every downstream
    renderer — llm_node's page-context line, the planner, the classifier —
    sees data that cannot forge a prompt section.
    """
    if not isinstance(ctx, dict):
        return {}
    return _clean(ctx, 0)


# ---------------------------------------------------------------------------
# Untrusted content fence (R7-H2)
# ---------------------------------------------------------------------------

_SOURCE_RE = re.compile(r"^[a-z_]+$")
# Case-insensitive: a model reads `</UNTRUSTED_CONTENT>` as a closer too.
_FENCE_TAG_RE = re.compile(r"</?untrusted_content", re.IGNORECASE)
_UNTRUSTED_MAX_CHARS = 2000


def wrap_untrusted(
    text: str, source: str, max_chars: int = _UNTRUSTED_MAX_CHARS
) -> str:
    """Fence third-party content so the model can tell data from instructions.

    ``source`` is a fixed label supplied by the caller (never user data) and
    is validated to ``[a-z_]+`` so it cannot inject attributes. Any forged
    ``<untrusted_content`` / ``</untrusted_content`` substring inside *text*
    is entity-escaped so the content cannot close its own fence.
    """
    if not _SOURCE_RE.match(source or ""):
        raise ValueError(f"invalid untrusted-content source: {source!r}")
    body = str(text or "")
    if len(body) > max_chars:
        body = body[:max_chars] + "..."
    body = _FENCE_TAG_RE.sub(lambda m: "&lt;" + m.group(0)[1:], body)
    return f'<untrusted_content source="{source}">\n{body}\n</untrusted_content>'


def current_turn_final_text(messages: list[BaseMessage]) -> str | None:
    """Return the latest content-bearing assistant message after the last user.

    Checkpointed graph output contains previous turns. Result extraction must
    never use an earlier turn as a fallback when the current turn produced no
    answer.
    """
    last_human = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, HumanMessage)
        ),
        default=-1,
    )
    for message in reversed(messages[last_human + 1 :]):
        if not isinstance(message, AIMessage):
            continue
        text = _assistant_text(message.content)
        if text is not None:
            return text
    return None


def normalize_terminal_messages(
    messages: list[BaseMessage], reason: str
) -> list[BaseMessage]:
    """Repair terminal tool-call history and add an honest answer if absent.

    Every abandoned tool call receives a placeholder ToolMessage. If this
    turn has no content-bearing assistant message, append the supplied reason
    so callers cannot mistake an older turn's answer for this turn's result.
    """
    normalized = _normalize_messages(messages, trim_history=False)
    if current_turn_final_text(normalized) is None and reason.strip():
        normalized.append(AIMessage(content=reason))
    return normalized


def terminal_message_additions(
    messages: list[BaseMessage], normalized: list[BaseMessage]
) -> list[BaseMessage]:
    """Return only newly synthesized terminal messages for a graph update."""
    original_objects = {id(message) for message in messages}
    original_tool_call_ai = [
        message
        for message in messages
        if isinstance(message, AIMessage) and message.tool_calls
    ]
    additions: list[BaseMessage] = []
    for message in normalized:
        if id(message) in original_objects:
            continue
        if isinstance(message, ToolMessage):
            additions.append(message)
        elif isinstance(message, AIMessage) and not any(
            original.content == message.content for original in original_tool_call_ai
        ):
            additions.append(message)
    return additions


def sanitize_model_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return API-valid model input with bounded history."""
    return _normalize_messages(messages, trim_history=True)


def _assistant_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    text = "".join(parts)
    return text if text.strip() else None


def _tool_call_id(tool_call: Any) -> str | None:
    if isinstance(tool_call, dict):
        call_id = tool_call.get("id")
    else:
        call_id = getattr(tool_call, "id", None)
    return call_id if isinstance(call_id, str) and call_id else None


def _normalize_messages(
    raw: list[BaseMessage], *, trim_history: bool
) -> list[BaseMessage]:
    """Rebuild tool-call answers and optionally apply the model history cap."""
    tool_messages: dict[str, ToolMessage] = {}
    for message in raw:
        if isinstance(message, ToolMessage) and message.tool_call_id:
            tool_messages[message.tool_call_id] = message

    rebuilt: list[BaseMessage] = []
    placed_ids: set[str] = set()
    for message in raw:
        if isinstance(message, ToolMessage):
            continue
        if not (isinstance(message, AIMessage) and message.tool_calls):
            rebuilt.append(message)
            continue

        kept_calls: list[dict[str, Any]] = []
        answers: list[ToolMessage] = []
        for tool_call in message.tool_calls:
            call_id = _tool_call_id(tool_call)
            if not call_id or call_id in placed_ids:
                continue
            kept_calls.append(tool_call)
            answers.append(
                tool_messages.get(call_id)
                or ToolMessage(content=_TOOL_PLACEHOLDER_CONTENT, tool_call_id=call_id)
            )
            placed_ids.add(call_id)
        if len(kept_calls) != len(message.tool_calls):
            message = message.model_copy(update={"tool_calls": kept_calls})
        rebuilt.append(message)
        rebuilt.extend(answers)

    superseded: list[BaseMessage] = []
    for message in rebuilt:
        if (
            superseded
            and isinstance(superseded[-1], HumanMessage)
            and isinstance(message, HumanMessage)
        ):
            superseded[-1] = message
        else:
            superseded.append(message)

    if not trim_history:
        return superseded
    from src.services.agent.compactor import trim_model_history

    return trim_model_history(superseded)
