"""Regex PII redactor for memory values.

Run on every string we are about to persist to the long-term memory
store. Replaces emails, phone numbers, US SSNs, UUIDs, postgres
connection strings, and bearer tokens / API keys with bracketed
sentinels so the literal value never ends up in the recall index.

Intentionally conservative — false positives (a UUID-shaped string in
prose) are cheap; false negatives (a real email stored verbatim) cost
us GDPR posture.
"""

from __future__ import annotations

import re
from typing import Any, Final

_EMAIL_RE: Final = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
# US-ish phone with optional country code + separators.
# (?<!\d\.) prevents matching mid-IPv4 ("192.168.100.1001") or mid-version
# ("v1.234.567.8901") where a digit-group is preceded by "<digit>.".
# The country-code prefix REQUIRES an explicit '+' so a bare leading "1."
# (e.g. version string "1.234.567.8901") cannot be consumed as country code
# and then have the remaining 3-3-4 digits matched as area/exchange/line.
_PHONE_RE: Final = re.compile(
    r"(?<!\w)(?<!\d\.)(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_SSN_RE: Final = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_UUID_RE: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# postgres://user:pass@host:port/db or postgresql:// variant.
_PG_URL_RE: Final = re.compile(r"\bpostgres(?:ql)?://\S+\b")
# Bearer tokens / API keys: JWT, OpenAI (sk-proj-), Anthropic (sk-ant-),
# GitHub (ghp_ PAT, gho_ OAuth, ghu_ user-to-server, ghs_ server-to-server,
# ghr_ refresh, github_pat_ fine-grained).
# The prefix class is the full GitHub family on purpose: it previously read
# ``gh[ps]_`` while this comment already claimed OAuth coverage, and a live
# ``gho_`` marker reached model-visible retrieval context as a result
# (evals/AGENT_FLOW_BASELINE.md, P1). Keep the letters enumerated rather than
# widening to ``gh\w_`` so unrelated ``gh``-prefixed identifiers stay intact.
_TOKEN_RE: Final = re.compile(
    r"\b(?:eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]{20,}"  # JWT
    r"|sk-(?:proj|ant)-[A-Za-z0-9_-]{20,}"  # OpenAI / Anthropic
    r"|gh[oprsu]_[A-Za-z0-9]{30,}"  # GitHub PAT / OAuth / user / server / refresh
    r"|github_pat_[A-Za-z0-9_]{30,})\b"
)

_MAX_PUBLIC_TOOL_EXECUTIONS: Final = 100
_PUBLIC_TOOL_TEXT_LIMITS: Final = {
    "id": 128,
    "tool_name": 128,
    "tool_display_name": 200,
    "status": 32,
}


# NOTE: ``text`` is intentionally ``Any``. LangGraph HumanMessage.content
# can be ``list[dict]`` for multimodal messages, and upstream callers may
# pass int/bytes through Any-typed state dicts. Narrowing to ``str | None``
# would mark the isinstance guard below as unreachable + invite a future
# maintainer to remove it, reintroducing the TypeError this guard catches.
def redact_pii(text: Any) -> str:
    """Return *text* with emails, phones, SSNs, UUIDs, PG URLs, and tokens replaced."""
    if not text:
        return ""
    if not isinstance(text, str):
        # Best-effort: non-str inputs (multimodal HumanMessage.content as
        # list[dict], ints, bytes, etc.) cannot be regex-redacted safely.
        # Coerce to empty so the caller's save still succeeds with a
        # placeholder query rather than raising mid-re.sub.
        return ""
    out = _PG_URL_RE.sub("<postgres-url>", text)
    out = _TOKEN_RE.sub("<token>", out)
    out = _EMAIL_RE.sub("<email>", out)
    out = _PHONE_RE.sub("<phone>", out)
    out = _SSN_RE.sub("[REDACTED_SSN]", out)
    out = _UUID_RE.sub("<uuid>", out)
    return out


def redact_nested_pii(value: Any) -> Any:
    """Recursively redact string values without the browser-facing size cap.

    This is for trusted server-side model boundaries where preserving complete
    metadata matters. Dictionary keys are intentionally left unchanged: this
    function sanitizes values while retaining the metadata schema.
    """
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {key: redact_nested_pii(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_nested_pii(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_nested_pii(item) for item in value)
    return value


def redact_tool_args(value: Any) -> Any:
    """Recursively redact PII in a tool-args value while preserving structure.

    Strings are redacted and capped; dicts/lists recurse; JSON-safe scalars
    (int/float/bool/None) carry no PII and pass through unchanged. Anything
    else (datetime, Decimal, a custom object, …) is stringified and redacted —
    ``str(tool_input)`` used to tolerate those, so a bare passthrough here
    would make a downstream ``json.dumps`` raise. Keeping the JSON shape is
    what lets the frontend's args summarizer render it.

    Single source for every place tool args leave the server for a browser:
    live SSE ``tool_start`` previews, the confirm-path ``done`` payload, and
    persisted ``tool_executions`` served back on thread reload.
    """
    if isinstance(value, str):
        return redact_pii(value)[:500]
    if isinstance(value, dict):
        return {k: redact_tool_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tool_args(v) for v in value]
    if value is None or isinstance(value, (int, float)):  # bool is an int
        return value
    return redact_pii(str(value))[:500]


def redact_tool_executions(entries: Any) -> Any:
    """Redact the ``args`` field of persisted ``tool_executions`` entries.

    ChatMessage.tool_executions rows were written with raw args before (and
    after) live-SSE redaction shipped, so redaction must happen at serve
    time to cover historical rows. Non-dict entries pass through untouched
    rather than risking a 500 on a legacy shape.
    """
    if not entries:
        return entries
    return [
        {**e, "args": redact_tool_args(e.get("args"))} if isinstance(e, dict) else e
        for e in entries
    ]


def public_tool_execution_activity(entries: Any) -> list[dict[str, Any]] | None:
    """Project persisted tool traces onto the display-safe public activity DTO.

    General workspace APIs can be read under public-workspace access rules, so
    they must never expose raw arguments, results, errors, or unknown fields.
    Invalid legacy entries are ignored rather than reflected or allowed to
    break a message response.
    """
    if entries is None:
        return None
    if not isinstance(entries, list):
        return []

    activity = []
    for entry in entries[:_MAX_PUBLIC_TOOL_EXECUTIONS]:
        if not isinstance(entry, dict) or not isinstance(entry.get("tool_name"), str):
            continue

        item: dict[str, Any] = {}
        for field, limit in _PUBLIC_TOOL_TEXT_LIMITS.items():
            value = entry.get(field)
            if isinstance(value, str):
                item[field] = value[:limit]

        duration_ms = entry.get("duration_ms")
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
            item["duration_ms"] = max(0, duration_ms)
        activity.append(item)

    return activity


__all__ = [
    "public_tool_execution_activity",
    "redact_nested_pii",
    "redact_pii",
    "redact_tool_args",
    "redact_tool_executions",
]
