"""Per-turn tool-call deduplication.

Trace 019e18f0 showed the agent firing 13+ near-identical search_arxiv
calls across 4 LLM rounds (95s wall, 4224+ reasoning tokens). Even with
``parallel_tool_calls=False`` and a tighter loop ceiling, the model can
still repeat exact-match queries when the synthesis step decides "let me
try one more refinement." Dedupe catches those exact repeats and returns
the cached result instead of re-executing.

Public API:
    dedupe_key(tool_name, args) -> str
    find_cached_tool_results(tool_calls, messages, tool_executions) -> dict

Turn boundary is defined by walking messages backward to the most recent
HumanMessage. Anything after that boundary is "this turn." We then keep
only tool_executions whose ``id`` matches a ``ToolMessage.tool_call_id``
inside that slice — guarding against stale entries that leaked across
turns (long-running threads with a 20-entry retention window).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

# Defensive cap on candidate count, even when tool_executions is large
# (e.g. checkpoint resume on a long-lived thread). 50 covers MAX_TOOL_LOOPS=10
# × 5 parallel calls = 50 with no headroom needed beyond that.
_CACHE_LOOKUP_BUDGET = 50


def _canonicalize(args: Any) -> Any:
    """Recursively sort dict keys and drop None values so semantically
    identical argument shapes hash to the same key.

    Strings are stripped of surrounding whitespace; lists keep their order
    (order is semantically meaningful for most tool args).
    """
    if isinstance(args, dict):
        return {k: _canonicalize(v) for k, v in sorted(args.items()) if v is not None}
    if isinstance(args, list):
        return [_canonicalize(v) for v in args]
    if isinstance(args, str):
        return args.strip()
    return args


def dedupe_key(tool_name: str, args: dict) -> str:
    """Return a stable sha256 hex key for ``(tool_name, canonical(args))``."""
    payload = json.dumps(
        {"tool": tool_name, "args": _canonicalize(args or {})},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_turn_message_boundary(messages: list) -> int:
    """Index of the most recent ``HumanMessage`` in *messages*, or -1.

    Shared by `_current_turn_tool_call_ids` (here) and the iteration
    ledger's `_build_record` — both need the same "what counts as this
    turn" semantics. Caller decides whether to include the boundary
    message itself (`messages[boundary:]`) or only what came after
    (`messages[boundary + 1:]`).
    """
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return -1


def _current_turn_tool_call_ids(messages: list) -> set[str]:
    """Tool call ids that belong to the current turn.

    Walk messages backward; the slice between the most recent HumanMessage
    (exclusive) and the end is "this turn." Collect every
    ``ToolMessage.tool_call_id`` found in that slice.
    """
    boundary = _current_turn_message_boundary(messages)
    in_turn = messages[boundary + 1 :] if boundary >= 0 else list(messages)
    ids: set[str] = set()
    for m in in_turn:
        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", None)
            if isinstance(tc_id, str):
                ids.add(tc_id)
    return ids


def find_cached_tool_results(
    tool_calls: list[dict],
    messages: list,
    tool_executions: list[dict],
) -> dict[str, dict]:
    """Map ``tool_call_id`` → cached execution entry for repeat calls.

    A tool_call is "cached" when its ``(name, args)`` dedupe key matches a
    prior execution **within the current turn**. Cross-turn repeats are
    intentional (user is asking again) and never cached.

    Args:
        tool_calls: the ``tool_calls`` array from the latest AIMessage
        messages: ``state["messages"]`` — used to locate the turn boundary
        tool_executions: ``state["tool_executions"]`` — full history slice

    Returns:
        ``{tool_call_id: cached_execution_dict}`` for every input tool_call
        whose key already appeared this turn. Callers should short-circuit
        these and only execute the rest.
    """
    if not tool_calls:
        return {}

    in_turn_ids = _current_turn_tool_call_ids(messages)
    if not in_turn_ids:
        return {}

    # Single O(E) pre-filter with cheap predicates only — no hash compute.
    # Only completed in-turn executions are cache candidates. Failed,
    # interrupted, and already-deduped entries are skipped here so the
    # inner hash loop only runs on real candidates.
    candidates = [
        te
        for te in tool_executions
        if te.get("status") == "completed" and te.get("id") in in_turn_ids
    ]
    if not candidates:
        return {}

    # Tail-keep the most recent N candidates. Bounds worst-case CPU
    # regardless of upstream prune ordering or checkpoint resume.
    if len(candidates) > _CACHE_LOOKUP_BUDGET:
        candidates = candidates[-_CACHE_LOOKUP_BUDGET:]

    by_key: dict[str, dict] = {}
    for te in candidates:
        key = dedupe_key(te.get("tool_name", ""), te.get("args") or {})
        # First match wins. Candidates are in execution order, so we cite
        # the earliest concrete result.
        by_key.setdefault(key, te)

    cached: dict[str, dict] = {}
    for tc in tool_calls:
        tc_id = tc.get("id")
        if not isinstance(tc_id, str):
            continue
        key = dedupe_key(tc.get("name", ""), tc.get("args") or {})
        prior = by_key.get(key)
        if prior is not None:
            cached[tc_id] = prior
    return cached


def build_deduped_tool_message(
    tool_call_id: str, cached_execution: dict
) -> ToolMessage:
    """Construct a ToolMessage that gives the LLM both the cached data and
    a clear "you already did this" signal so it stops refining.

    The content is the cached ``result`` (json-encoded if needed) prefixed
    with ``[deduped: same args as call <prev_id>]\\n``. OpenAI requires
    every ``tool_calls[i].id`` be answered, so we emit a fresh ToolMessage
    bound to the new id but carrying the older payload.
    """
    prev_id = cached_execution.get("id", "<unknown>")
    raw_result = cached_execution.get("result")
    if isinstance(raw_result, (dict, list)):
        body = json.dumps(raw_result)
    elif raw_result is None:
        body = "null"
    else:
        body = str(raw_result)
    content = f"[deduped: same args as call {prev_id}]\n{body}"
    return ToolMessage(content=content, tool_call_id=tool_call_id)


# How many identical failed attempts are allowed per turn before the
# circuit breaker stops re-executing the same (tool, args). Traces
# 019f2a48-9083 / 019f2245-cf9d showed the model re-issuing an identical
# failing call at steps 3/6/9/12 until the loop cap killed the turn —
# completed-only dedupe (above) deliberately lets failures re-run once
# for genuine transients, but a third identical attempt is a storm.
FAILED_RETRY_THRESHOLD = 2


def find_repeated_failures(
    tool_calls: list[dict],
    messages: list,
    tool_executions: list[dict],
    threshold: int = FAILED_RETRY_THRESHOLD,
) -> dict[str, dict]:
    """Map ``tool_call_id`` → last failed execution for circuit-broken calls.

    A tool_call is circuit-broken when its ``(name, args)`` dedupe key has
    already FAILED ``threshold`` or more times within the current turn.
    One retry is allowed unless execution already exhausted the tool's
    retry budget. In that case re-planning must not restart internal retries.
    """
    if not tool_calls:
        return {}

    in_turn_ids = _current_turn_tool_call_ids(messages)
    if not in_turn_ids:
        return {}

    failed = [
        te
        for te in tool_executions
        if te.get("status") == "failed" and te.get("id") in in_turn_ids
    ]
    if not failed:
        return {}
    if len(failed) > _CACHE_LOOKUP_BUDGET:
        failed = failed[-_CACHE_LOOKUP_BUDGET:]

    counts: dict[str, int] = {}
    last_by_key: dict[str, dict] = {}
    for te in failed:
        key = dedupe_key(te.get("tool_name", ""), te.get("args") or {})
        counts[key] = counts.get(key, 0) + 1
        if te.get("retry_exhausted"):
            counts[key] = max(counts[key], threshold)
        last_by_key[key] = te

    capped: dict[str, dict] = {}
    for tc in tool_calls:
        tc_id = tc.get("id")
        if not isinstance(tc_id, str):
            continue
        key = dedupe_key(tc.get("name", ""), tc.get("args") or {})
        if counts.get(key, 0) >= threshold:
            capped[tc_id] = last_by_key[key]
    return capped


def build_failure_capped_tool_message(
    tool_call_id: str, tc: dict, prior_failure: dict, attempts: int
) -> ToolMessage:
    """ToolMessage telling the LLM to stop retrying identical failing args.

    Carries the prior error so the model can reason about it, plus an
    explicit instruction to change strategy — without this the model
    keeps re-planning the exact same call until the tool-loop cap kills
    the turn.
    """
    result = prior_failure.get("result")
    prior_error = (
        prior_failure.get("error")
        or (result.get("error") if isinstance(result, dict) else None)
        or "unknown error"
    )
    failure_summary = (
        "exhausted its retry budget"
        if prior_failure.get("retry_exhausted")
        else f"failed {attempts} times"
    )
    content = json.dumps(
        {
            "error": (
                f"{tc.get('name', 'tool')} {failure_summary} with these "
                f"exact arguments (last error: {prior_error}). Do NOT retry "
                "with the same arguments this turn — change the arguments, "
                "use a different tool, or answer with the information you "
                "already have."
            ),
            "error_category": "repeated_failure",
        }
    )
    return ToolMessage(content=content, tool_call_id=tool_call_id, status="error")


def build_failure_capped_execution_entry(
    tool_call_id: str, tc: dict, prior_failure: dict
) -> dict:
    """Append-ready ``tool_executions`` entry for a circuit-broken call."""
    return {
        "id": tool_call_id,
        "tool_name": tc.get("name", ""),
        "tool_display_name": tc.get("name", "").replace("_", " ").title(),
        "args": dict(tc.get("args") or {}),
        "status": "failed",
        "result": None,
        "error": "repeated_failure: identical args already failed this turn",
        "duration_ms": 0,
        "capped_from": prior_failure.get("id"),
    }


def build_deduped_execution_entry(
    tool_call_id: str, tc: dict, cached_execution: dict
) -> dict:
    """Append-ready ``tool_executions`` entry tagged ``status='deduped'``.

    Keeps the same shape as concrete executions so downstream consumers
    (the chat UI, reflection prompt rendering) don't need a schema branch.
    """
    return {
        "id": tool_call_id,
        "tool_name": tc.get("name", ""),
        "tool_display_name": tc.get("name", "").replace("_", " ").title(),
        "args": dict(tc.get("args") or {}),
        "status": "deduped",
        "result": cached_execution.get("result"),
        "duration_ms": 0,
        "deduped_from": cached_execution.get("id"),
    }
