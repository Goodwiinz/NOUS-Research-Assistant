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

# Every code point that starts a new line for a model tokenizer or a
# markdown renderer — not just CR/LF (Codex review on #1594): NEL, VT, FF,
# LINE SEPARATOR, PARAGRAPH SEPARATOR.
_LINE_BREAK_RE = re.compile(r"[\r\n\x0b\x0c\x85\u2028\u2029]")

# Maximum length (chars) for any user-supplied string interpolated into a
# classifier system prompt.  Truncating + neutralising braces/newlines is the
# minimum defence against prompt-injection via previous_turn / prior_tool /
# page_context.  Longer values are clipped with an ellipsis.
_PROMPT_FIELD_MAX_CHARS = 400


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
