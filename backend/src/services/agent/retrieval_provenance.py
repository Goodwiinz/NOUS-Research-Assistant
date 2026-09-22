"""Canonical provenance extracted from successful knowledge-base tool results."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import structlog
from langchain_core.messages import BaseMessage, ToolMessage

from src.services.agent._sanitize import _sanitize_prompt_field, wrap_untrusted

logger = structlog.get_logger(__name__)

MAX_RETRIEVED_CONTEXTS = 20
# Optional identifiers are copied onto every live citation snapshot. Bound
# their serialized size at ingestion so twenty canonical identity records fit
# the shared 16 KiB SSE payload without clipping (which would change identity).
MAX_IDENTIFIER_JSON_BYTES = 192
NO_RETRIEVAL_GUIDANCE = (
    "No documents were retrieved for this turn. If the question concerns "
    "the user's documents, state plainly that nothing relevant was found "
    "in their corpus. You may answer from general knowledge ONLY if you "
    "label it as such — do NOT invent citations, paper references, or a "
    "bibliography."
)


def _reject(execution: Mapping[str, Any], category: str) -> None:
    logger.warning(
        "agent_tool_context_rejected",
        tool_name=str(execution.get("tool_name") or ""),
        call_id=str(execution.get("id") or ""),
        category=category,
    )


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 2_147_483_647
    ):
        raise ValueError("invalid locator")
    return int(value)


def _valid_optional_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(json.dumps(value).encode("utf-8")) <= MAX_IDENTIFIER_JSON_BYTES
    )


def _normalize_chunk(
    chunk: Any, execution: Mapping[str, Any], position: int
) -> dict[str, Any] | None:
    if not isinstance(chunk, Mapping):
        _reject(execution, "malformed_chunk")
        return None
    try:
        document_id = str(UUID(str(chunk.get("document_id"))))
    except (TypeError, ValueError, AttributeError):
        _reject(execution, "invalid_document_id")
        return None

    title = chunk.get("title")
    content = chunk.get("text")
    score = chunk.get("score")
    if not isinstance(title, str) or not title.strip():
        _reject(execution, "invalid_title")
        return None
    if not isinstance(content, str) or not content.strip():
        _reject(execution, "invalid_content")
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        _reject(execution, "invalid_score")
        return None
    try:
        numeric_score = float(score)
    except (OverflowError, TypeError, ValueError):
        _reject(execution, "invalid_score")
        return None
    if not math.isfinite(numeric_score):
        _reject(execution, "invalid_score")
        return None

    metadata = chunk.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    try:
        chunk_index = _optional_nonnegative_int(
            chunk.get("chunk_index", metadata.get("chunk_index"))
        )
        page_number = _optional_nonnegative_int(
            chunk.get("page_number", metadata.get("page_number"))
        )
    except ValueError:
        _reject(execution, "invalid_locator")
        return None
    chunk_id = chunk.get("chunk_id", metadata.get("chunk_id"))
    if chunk_id is not None and not _valid_optional_identifier(chunk_id):
        _reject(execution, "invalid_locator")
        return None

    context: dict[str, Any] = {
        "document_id": document_id,
        # Citation.document_title is VARCHAR(500). The retrieval tool already
        # redacts titles, and replacement tokens can make a valid source title
        # longer than its input; retain the context with a persistence-safe
        # display title instead of failing the whole assistant transaction.
        "title": title[:500],
        "content": content,
        "score": numeric_score,
        "score_source": chunk.get("score_source") or chunk.get("source"),
        "tool_call_id": str(execution.get("id") or ""),
        "tool_chunk_position": position,
        "context_origin": "tool",
    }
    if chunk_id is not None:
        context["chunk_id"] = chunk_id
    if chunk_index is not None:
        context["chunk_index"] = chunk_index
    if page_number is not None:
        context["page_number"] = page_number
    return context


def contexts_from_tool_execution(
    execution: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize citations from one newly completed ``do_kb_retrieve`` call."""
    if (
        execution.get("tool_name") != "do_kb_retrieve"
        or execution.get("status") != "completed"
    ):
        return []
    result = execution.get("result")
    if not isinstance(result, Mapping) or result.get("error"):
        _reject(execution, "invalid_result")
        return []
    chunks = result.get("chunks")
    if not isinstance(chunks, list):
        _reject(execution, "invalid_chunks")
        return []
    return [
        normalized
        for position, chunk in enumerate(chunks)
        if (normalized := _normalize_chunk(chunk, execution, position)) is not None
    ]


def _dedupe_key(context: Mapping[str, Any]) -> tuple[str, str] | None:
    try:
        document_id = str(UUID(str(context.get("document_id"))))
    except (TypeError, ValueError, AttributeError):
        return None
    content = context.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    collapsed = " ".join(content.split())
    return document_id, hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def merge_retrieved_contexts(
    existing: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_RETRIEVED_CONTEXTS,
) -> list[dict[str, Any]]:
    """Merge first-seen canonical contexts without renumbering prior sources."""
    # Prior state may include legacy RAG contexts without a document UUID and
    # may already have been emitted as numbered sources. Preserve every prior
    # slot verbatim: filtering or deduping here would renumber citations.
    merged: list[dict[str, Any]] = [dict(item) for item in existing[:limit]]
    seen: set[tuple[str, str]] = set()
    for item in merged:
        key = _dedupe_key(item)
        if key is not None:
            seen.add(key)

    for execution in executions:
        for context in contexts_from_tool_execution(execution):
            if len(merged) >= limit:
                return merged
            key = _dedupe_key(context)
            if key is not None and key not in seen:
                seen.add(key)
                merged.append(context)
    return merged


def _parse_tool_result(content: Any) -> Mapping[str, Any] | None:
    if not isinstance(content, str):
        return None
    body = content.strip()
    if body.startswith("[deduped:"):
        _prefix, separator, body = body.partition("\n")
        if not separator:
            return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _tool_message_has_original_chunk(
    context: Mapping[str, Any], messages: Sequence[BaseMessage]
) -> bool:
    call_id = context.get("tool_call_id")
    position = context.get("tool_chunk_position")
    if (
        not isinstance(call_id, str)
        or isinstance(position, bool)
        or not isinstance(position, int)
    ):
        return False
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.tool_call_id != call_id:
            continue
        if (message.additional_kwargs or {}).get("compacted"):
            return False
        result = _parse_tool_result(message.content)
        chunks = result.get("chunks") if result is not None else None
        if not isinstance(chunks, list) or not (0 <= position < len(chunks)):
            return False
        chunk = chunks[position]
        if not isinstance(chunk, Mapping):
            return False
        candidate = {
            "document_id": chunk.get("document_id"),
            "content": chunk.get("text"),
        }
        return _dedupe_key(candidate) == _dedupe_key(context)
    return False


def _source_label(context: Mapping[str, Any]) -> str:
    title = _sanitize_prompt_field(str(context.get("title") or "Untitled"))
    lines = [f"title: {title}"]
    for key, label in (
        ("page_number", "page"),
        ("chunk_index", "chunk_index"),
        ("chunk_id", "chunk_id"),
    ):
        value = context.get(key)
        if value is not None:
            lines.append(f"{label}: {_sanitize_prompt_field(str(value))}")
    if context.get("context_origin") == "tool":
        call_id = context.get("tool_call_id")
        chunk_position = context.get("tool_chunk_position")
        if call_id:
            lines.append(f"tool_call: {_sanitize_prompt_field(str(call_id))}")
        if isinstance(chunk_position, int) and not isinstance(chunk_position, bool):
            lines.append(f"result_chunk: {chunk_position + 1}")
    return "\n".join(lines)


def render_retrieval_prompt(
    contexts: Sequence[Mapping[str, Any]], messages: Sequence[BaseMessage]
) -> str:
    """Render one stable numbered source map for every model synthesis path."""
    if not contexts:
        return NO_RETRIEVAL_GUIDANCE
    blocks: list[str] = []
    for source_position, context in enumerate(contexts, 1):
        body = _source_label(context)
        is_tool_context = context.get("context_origin") == "tool"
        if not is_tool_context or not _tool_message_has_original_chunk(
            context, messages
        ):
            body += f"\nevidence: {str(context.get('content') or '')}"
        blocks.append(
            f"[Doc {source_position}]\n"
            + wrap_untrusted(
                body,
                "retrieved_document",
                2300 if is_tool_context else 3100,
            )
        )
    return "Retrieved context:\n" + "\n\n".join(blocks)


__all__ = [
    "MAX_RETRIEVED_CONTEXTS",
    "NO_RETRIEVAL_GUIDANCE",
    "contexts_from_tool_execution",
    "merge_retrieved_contexts",
    "render_retrieval_prompt",
]
