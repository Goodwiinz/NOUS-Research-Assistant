"""Complete-request UTF-8 budgeting and deterministic source excerpting."""

import hashlib
import json
from typing import Any

MAX_PROMPT_BYTES = 16_384
MAX_PARTS_PER_BATCH = 8


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _prompt_text(
    records: list[dict[str, Any]], fixed_context: dict[str, Any]
) -> tuple[str, str]:
    system_prompt = str(fixed_context.get("system_prompt") or "")
    context = {
        key: value for key, value in fixed_context.items() if key != "system_prompt"
    }
    prompt = _compact_json({"context": context, "records": records})
    return system_prompt, prompt


def _request_bytes(records: list[dict[str, Any]], fixed_context: dict[str, Any]) -> int:
    system_prompt, prompt = _prompt_text(records, fixed_context)
    return len(system_prompt.encode("utf-8")) + len(prompt.encode("utf-8"))


def _split_record(
    record: dict[str, Any], fixed_context: dict[str, Any], max_bytes: int
) -> list[dict[str, Any]]:
    """Split text at deterministic Unicode boundaries until each part fits."""
    text = record["text"]
    parts: list[dict[str, Any]] = []
    start = 0
    part_number = 1
    while start < len(text):
        low = 1
        high = len(text) - start
        best = 0
        while low <= high:
            mid = (low + high) // 2
            candidate = {
                "source_id": record["source_id"],
                "title": record["title"],
                "evidence_level": record["evidence_level"],
                "part_id": f"p{part_number:04d}",
                "text": text[start : start + mid],
            }
            if _request_bytes([candidate], fixed_context) <= max_bytes:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        if best == 0:
            raise ValueError(
                "fixed instructions leave no room for a usable evidence excerpt"
            )
        end = start + best
        if end < len(text):
            # Prefer the last paragraph or line boundary that still fits.
            boundary = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end))
            if boundary > start + best // 2:
                end = boundary
        part = {
            "source_id": record["source_id"],
            "title": record["title"],
            "evidence_level": record["evidence_level"],
            "part_id": f"p{part_number:04d}",
            "text": text[start:end],
        }
        parts.append(part)
        start = end
        part_number += 1
    return parts


def build_prompt_batches(
    records: list[dict[str, Any]],
    fixed_context: dict[str, Any],
    max_bytes: int = MAX_PROMPT_BYTES,
    *,
    structured_records: bool = False,
) -> list[dict[str, Any]]:
    """Pack projected evidence records under a combined UTF-8 request ceiling.

    Provenance, aliases, original text and timestamps are intentionally not
    copied from input records. Callers supply their stage parameters in
    ``fixed_context`` and receive audit metadata separate from provider input.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    empty_size = _request_bytes([], fixed_context)
    if empty_size > max_bytes:
        raise ValueError("fixed prompt instructions exceed the UTF-8 byte limit")

    projected: list[dict[str, Any]] = []
    for original in records:
        if not isinstance(original, dict):
            continue
        source_id = original.get("source_id")
        title = original.get("title") or "Untitled source"
        full_text = original.get("full_text")
        text = (
            full_text
            if isinstance(full_text, str) and full_text
            else original.get("abstract")
        )
        evidence_level = original.get("evidence_level")
        if not evidence_level:
            evidence_level = (
                "full_text" if isinstance(full_text, str) and full_text else "abstract"
            )
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(text, str) or not text:
            continue
        projected.append(
            {
                "source_id": source_id,
                "title": str(title),
                "evidence_level": str(evidence_level),
                "part_id": (
                    original.get("part_id")
                    if isinstance(original.get("part_id"), str)
                    else "p0001"
                ),
                "text": text,
            }
        )

    parts: list[dict[str, Any]] = []
    if structured_records:
        for record in projected:
            try:
                decoded = json.loads(record["text"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "structured evidence record is malformed JSON"
                ) from exc
            values = decoded if isinstance(decoded, list) else [decoded]
            if not values or any(not isinstance(value, dict) for value in values):
                raise ValueError("structured evidence records must be JSON objects")

            # Structured evidence and claims are indivisible. Array payloads can
            # be grouped at object boundaries; an individual oversized object
            # fails before the first provider request.
            group: list[dict[str, Any]] = []
            for value in values:
                candidate_group = group + [value]
                structured_candidate = {
                    **record,
                    "text": _compact_json(
                        candidate_group if isinstance(decoded, list) else value
                    ),
                }
                if _request_bytes([structured_candidate], fixed_context) <= max_bytes:
                    group = candidate_group
                    continue
                if group:
                    parts.append(candidate_for_group(record, group))
                    group = []
                    structured_candidate = {
                        **record,
                        "text": _compact_json(
                            [value] if isinstance(decoded, list) else value
                        ),
                    }
                if _request_bytes([structured_candidate], fixed_context) > max_bytes:
                    raise ValueError(
                        "oversized structured evidence/claim object exceeds UTF-8 byte limit"
                    )
                group = [value]
            if group:
                parts.append(candidate_for_group(record, group))
    else:
        for record in projected:
            if _request_bytes([record], fixed_context) <= max_bytes:
                parts.append(record)
            else:
                parts.extend(_split_record(record, fixed_context, max_bytes))

    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for record in parts:
        batch_candidate = current + [record]
        if (
            len(batch_candidate) <= MAX_PARTS_PER_BATCH
            and _request_bytes(batch_candidate, fixed_context) <= max_bytes
        ):
            current = batch_candidate
            continue
        if not current:
            raise ValueError(
                "fixed instructions leave no room for a usable evidence record"
            )
        batches.append(_make_batch(current, fixed_context, len(batches)))
        current = [record]
        if _request_bytes(current, fixed_context) > max_bytes:
            raise ValueError(
                "fixed instructions leave no room for a usable evidence record"
            )
    if current:
        batches.append(_make_batch(current, fixed_context, len(batches)))
    return batches


def candidate_for_group(
    record: dict[str, Any],
    values: list[dict[str, Any]],
) -> dict[str, Any]:
    decoded_was_array = json.loads(record["text"])
    text_value: Any = values if isinstance(decoded_was_array, list) else values[0]
    return {
        **record,
        "text": _compact_json(text_value),
    }


def _make_batch(
    records: list[dict[str, Any]], fixed_context: dict[str, Any], index: int
) -> dict[str, Any]:
    system_prompt, prompt = _prompt_text(records, fixed_context)
    input_hash = hashlib.sha256(
        json.dumps(
            [system_prompt, prompt], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "index": index,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "records": records,
        "source_ids": list(dict.fromkeys(record["source_id"] for record in records)),
        "part_ids": [record["part_id"] for record in records],
        "input_bytes": len(system_prompt.encode("utf-8")) + len(prompt.encode("utf-8")),
        "input_hash": input_hash,
    }
