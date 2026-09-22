"""Per-turn append-only iteration ledger.

Inspired by K-Dense's `rowan-autosearch`: capture every agent turn as a
versioned JSON record on disk so the full reasoning trail (intent, plan,
tool executions, RAG context, response, reflection verdict, tokens) is
auditable, replayable, and post-hoc evaluable.

Layout::

    <AGENT_LEDGER_DIR>/
        <thread_id>/
            iterations/
                0001.json
                0002.json
                ...

Each turn appends one numbered JSON file. The format mirrors the K-Dense
shape: `turn`, `timestamp`, plus a `state_snapshot` of the meaningful
agent state and a compact `summary` for quick scans.

Disabled when ``settings.AGENT_LEDGER_DIR`` is empty/None — zero-cost
when the user doesn't want it.

Everything written here goes through ``_pii_redact`` first (R7-L4): the
records are plain files on a shared volume that outlive the run, and they
carry raw prompts, model output and tool arguments.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.core.config import get_settings
from src.services.agent._pii_redact import redact_nested_pii, redact_pii

logger = logging.getLogger(__name__)


def _ledger_root() -> Path | None:
    settings = get_settings()
    base = settings.AGENT_LEDGER_DIR
    if not base:
        return None
    return Path(base).expanduser()


def _next_turn_number(thread_dir: Path) -> int:
    """Atomically claim the next turn number via exclusive file creation.

    Scans iterations/ for the highest existing turn N, then attempts to
    create turn_N+1.json with O_CREAT|O_EXCL|O_WRONLY. If another process
    wins the race and creates that file first (FileExistsError), increment
    and retry — up to 100 attempts. Using O_EXCL closes the TOCTOU window
    between scanning and writing that exists in a plain scan-then-write
    approach under concurrent uvicorn workers.

    Returns the claimed turn number (the caller's _atomic_write_json will
    replace the placeholder file with the real record via os.replace).
    """
    iter_dir = thread_dir / "iterations"
    iter_dir.mkdir(parents=True, exist_ok=True)

    existing = []
    for p in iter_dir.iterdir():
        if p.suffix != ".json":
            continue
        try:
            # Skip 0-byte placeholders left behind by a crashed claim so they
            # don't permanently inflate the turn counter. A live in-flight
            # claim is still protected by the O_EXCL check below.
            if p.stat().st_size == 0:
                continue
            existing.append(int(p.stem))
        except (ValueError, OSError):
            continue
    candidate = (max(existing) + 1) if existing else 1

    for _ in range(100):
        path = iter_dir / f"{candidate:04d}.json"
        try:
            # O_EXCL guarantees atomic creation; raises FileExistsError if
            # another worker already claimed this turn number.
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate += 1

    # Fallback: return the candidate even without a claim. Surface it — a
    # collision here means the record may overwrite a concurrent turn, which
    # is otherwise invisible on this write-only ledger.
    logger.warning(
        "ledger: turn-number claim exhausted after 100 attempts in %s "
        "(candidate=%d may overwrite a concurrent turn)",
        iter_dir,
        candidate,
    )
    return candidate


def _serialize_message(msg: Any) -> dict:
    """Compact dict for one LangChain message — drops bulky fields."""
    if isinstance(msg, HumanMessage):
        return {"role": "human", "content": redact_pii(msg.content)[:2000]}
    if isinstance(msg, AIMessage):
        out: dict[str, Any] = {
            "role": "ai",
            "content": redact_pii(msg.content)[:4000],
        }
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            out["tool_calls"] = [
                {
                    "name": tc.get("name"),
                    "args": redact_nested_pii(tc.get("args")),
                    "id": tc.get("id"),
                }
                for tc in tool_calls
            ]
        meta = getattr(msg, "response_metadata", None) or {}
        if meta.get("model_name"):
            out["model"] = meta["model_name"]
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            out["usage"] = {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "reasoning": (usage.get("output_token_details") or {}).get("reasoning"),
                "cache_read": (usage.get("input_token_details") or {}).get(
                    "cache_read"
                ),
            }
        return out
    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": getattr(msg, "tool_call_id", None),
            "content": redact_pii(msg.content)[:2000],
        }
    return {
        "role": getattr(msg, "type", "unknown"),
        "content": redact_pii(str(msg))[:500],
    }


def _build_record(state: dict, turn: int) -> dict:
    """Build the JSON-serializable record for one turn."""
    messages = state.get("messages") or []
    # Slice to current turn — same boundary the dedupe walks (most recent
    # HumanMessage to end). Includes the boundary HumanMessage itself so
    # the ledger record shows what the user asked.
    from src.services.agent.tool_dedupe import _current_turn_message_boundary

    boundary = _current_turn_message_boundary(messages)
    in_turn_msgs = messages[boundary:] if boundary >= 0 else list(messages)

    user_msg = ""
    ai_content = ""
    for m in in_turn_msgs:
        if isinstance(m, HumanMessage) and not user_msg:
            user_msg = redact_pii(m.content)[:1000]
        if isinstance(m, AIMessage) and m.content:
            ai_content = redact_pii(m.content)[:4000]

    tool_executions = redact_nested_pii(list(state.get("tool_executions") or []))

    # Compact retrieved_contexts: titles + scores only, drop full chunk text.
    retrieved = state.get("retrieved_contexts") or []
    retrieved_summary = [
        {
            "document_id": ctx.get("document_id"),
            "title": ctx.get("title"),
            "score": ctx.get("score"),
        }
        for ctx in retrieved[:10]
        if isinstance(ctx, dict)
    ]

    reflection = state.get("_reflection_result")
    if reflection is not None and not isinstance(reflection, dict):
        reflection = {
            "passed": getattr(reflection, "passed", None),
            "issues": getattr(reflection, "issues", None),
            "severity": getattr(reflection, "severity", None),
        }

    return {
        "turn": turn,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "user_query": user_msg,
            "intent": state.get("intent"),
            "intent_confidence": state.get("intent_confidence"),
            "tool_loop_count": state.get("tool_loop_count"),
            "error_count": state.get("error_count"),
            "tools_used": [te.get("tool_name") for te in tool_executions[-5:]],
            "ai_response_chars": len(ai_content),
        },
        "state_snapshot": {
            "messages": [_serialize_message(m) for m in in_turn_msgs],
            "page_context": state.get("page_context"),
            "current_project_id": state.get("current_project_id"),
            "model": state.get("model"),
            "plan": state.get("plan") or [],
            "tool_executions": tool_executions,
            "retrieved_contexts": retrieved_summary,
            "user_memories_keys": [
                m.get("key") for m in (state.get("user_memories") or [])
            ],
            "reflection_result": reflection,
            "last_error": state.get("last_error") or None,
            "last_error_info": state.get("last_error_info") or None,
            "compaction_count": state.get("compaction_count"),
            "reflection_count": state.get("reflection_count"),
        },
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """tmp+rename atomic write so partial writes don't poison the ledger."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _maybe_write_config(thread_dir: Path, state: dict) -> None:
    """Write runs/<thread_id>/config.json on the first turn only.

    Captures the immutable run setup: thread_id, user_id, page_context
    (project/paper/chat type at first turn), model override, and the
    initial user query. Subsequent turns leave it alone — the file is
    a record of how the run STARTED, not its current state.
    """
    config_path = thread_dir / "config.json"
    if config_path.exists():
        return
    messages = state.get("messages") or []
    first_user = ""
    for m in messages:
        if isinstance(m, HumanMessage) and m.content:
            first_user = redact_pii(m.content)[:1000]
            break
    config = {
        "thread_id": (state.get("thread_id") or thread_dir.name),
        "user_id": state.get("user_id"),
        "page_context": state.get("page_context"),
        "current_project_id": state.get("current_project_id"),
        "model": state.get("model"),
        "initial_query": first_user,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _atomic_write_json(config_path, config)
    except Exception as exc:  # noqa: BLE001 - observability only
        logger.debug("ledger config write skipped: %s", exc)


def write_iteration(thread_id: str, state: dict) -> Path | None:
    """Append one iteration record for *thread_id*. Returns the path
    written, or None if the ledger is disabled or write fails (never
    raises — agent flow must not break on observability errors).

    On first turn also writes ``config.json`` (immutable run setup) and
    on every turn rewrites ``final.json`` (latest summary; fast lookup
    without scanning iterations/).
    """
    root = _ledger_root()
    if not root or not thread_id:
        return None
    claimed_path: Path | None = None
    try:
        thread_dir = root / thread_id
        iter_dir = thread_dir / "iterations"
        iter_dir.mkdir(parents=True, exist_ok=True)

        # Write config.json on the first turn (idempotent — checks existence).
        _maybe_write_config(thread_dir, state)

        turn = _next_turn_number(thread_dir)
        # _next_turn_number reserved this path with an empty placeholder.
        claimed_path = iter_dir / f"{turn:04d}.json"
        record = _build_record(state, turn)

        path = claimed_path
        _atomic_write_json(path, record)

        # Rewrite final.json with the latest summary on every turn so
        # consumers (HTML report, dashboard) can fetch the run's current
        # state in one read.
        final_path = thread_dir / "final.json"
        try:
            _atomic_write_json(
                final_path,
                {
                    "thread_id": thread_id,
                    "latest_turn": turn,
                    "updated_at": record["timestamp"],
                    "summary": record["summary"],
                    "page_context": record["state_snapshot"].get("page_context"),
                    "current_project_id": record["state_snapshot"].get(
                        "current_project_id"
                    ),
                    "tool_executions_count": len(
                        record["state_snapshot"].get("tool_executions") or []
                    ),
                    "reflection_result": record["state_snapshot"].get(
                        "reflection_result"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ledger final.json write skipped: %s", exc)

        logger.debug("ledger: wrote %s (turn %d)", path, turn)
        return path
    except Exception as exc:  # noqa: BLE001 - never crash the agent
        logger.warning("ledger write failed for thread %s: %s", thread_id, exc)
        # Best-effort: remove the empty placeholder left by the turn claim so
        # it doesn't inflate future turn numbers or get silently skipped on
        # replay. Only unlink if it's still 0 bytes (never clobber a record).
        if claimed_path is not None:
            try:
                if claimed_path.exists() and claimed_path.stat().st_size == 0:
                    claimed_path.unlink()
            except OSError:
                pass
        return None
