"""Bounded, JSON-serializable identity memory for agent checkpoints.

The ledger records observed tool identities so compacted or older conversation
history can still support scoped follow-ups. It is evidence only: every tool
call must perform its ordinary ownership and scope checks again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any, TypedDict
from uuid import UUID

LEDGER_VERSION = 1
MAX_LEDGER_RECORDS = 128
MAX_LEDGER_BYTES = 24_576
MAX_LABEL_CHARS = 240
MAX_RELATED_IDS = 16
MAX_PROCESSED_OBSERVATIONS = 256
MAX_PAYLOAD_BYTES = 1_048_576
MAX_SCAN_OBJECTS = 10_000
MAX_SCAN_DEPTH = 12
MAX_CONTEXT_BYTES = 6_144
MAX_REFERENCE_TEXT_CHARS = 16_384
MAX_REFERENCE_IDS = 128
MAX_HARVEST_MESSAGES = 512
MAX_HARVEST_TOOL_CALLS = 512

_UUID_KINDS = frozenset({"project", "document", "note", "draft"})
_KNOWN_ROOT_IDS = {
    "project_id": "project",
    "document_id": "document",
    "note_id": "note",
    "draft_id": "draft",
    "task_id": "task",
}
_COLLECTIONS = {
    "projects": "project",
    "documents": "document",
    "notes": "note",
    "drafts": "draft",
    "papers": "arxiv",
    "failed_papers": "arxiv",
}
_ID_LISTS = {
    "project_ids": "project",
    "document_ids": "document",
    "note_ids": "note",
    "draft_ids": "draft",
    "task_ids": "task",
    "paper_ids": "arxiv",
}
_ROW_ID_KEYS = {
    "project": ("project_id", "id"),
    "document": ("document_id", "id"),
    "note": ("note_id", "id"),
    "draft": ("draft_id", "id"),
    "task": ("task_id", "id"),
    "arxiv": ("paper_id", "arxiv_id", "id"),
}
_RELATION_KEYS = (
    "project_id",
    "document_id",
    "note_id",
    "draft_id",
    "task_id",
    "paper_id",
    "arxiv_id",
)
_ARXIV_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?\Z")
_EXTERNAL_NAMESPACE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z")
_MUTATING_TOOLS = frozenset(
    {
        "add_document_to_project",
        "create_draft",
        "create_project",
        "create_project_note",
        "ingest_arxiv_papers",
        "revise_draft",
        "execute_code",
        "forget_memory",
    }
)
_COMPLETED_STATES = frozenset(
    {
        "completed",
        "created",
        "deleted",
        "ingestion_complete",
        "success",
        "succeeded",
        "updated",
    }
)


class IdentityRecord(TypedDict, total=False):
    """One typed, observed identity; all values are safe JSON scalars/maps."""

    kind: str
    namespace: str
    id: str
    name: str
    observed_status: str
    source_tool: str
    source_path: str
    turn_id: str
    tool_call_id: str
    observation_id: str
    observed_order: int
    related: dict[str, str]


def _json_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return MAX_PAYLOAD_BYTES + 1


def _string_bytes_if_bounded(value: str, remaining: int) -> int | None:
    """Measure one string without allocating a second encoded copy of it."""
    total = 0
    for char in value:
        codepoint = ord(char)
        total += (
            1
            if codepoint < 0x80
            else 2 if codepoint < 0x800 else 3 if codepoint < 0x10000 else 4
        )
        if total > remaining:
            return None
    return total


def _payload_within_scan_bounds(payload: Mapping[str, Any]) -> bool:
    """Bound legacy traversal work before walking a result for identities."""
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    size = 2
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > MAX_SCAN_OBJECTS or depth > MAX_SCAN_DEPTH:
            return False
        if isinstance(value, str):
            measured = _string_bytes_if_bounded(value, MAX_PAYLOAD_BYTES - size)
            if measured is None:
                return False
            size += measured
        elif isinstance(value, Mapping):
            if len(value) > MAX_SCAN_OBJECTS - visited - len(stack):
                return False
            size += 2
            if size > MAX_PAYLOAD_BYTES:
                return False
            for key, child in value.items():
                if not isinstance(key, str):
                    return False
                measured = _string_bytes_if_bounded(key, MAX_PAYLOAD_BYTES - size)
                if measured is None:
                    return False
                size += measured + 1
                if size > MAX_PAYLOAD_BYTES:
                    return False
                if visited + len(stack) >= MAX_SCAN_OBJECTS:
                    return False
                stack.append((child, depth + 1))
        elif isinstance(value, (list, tuple)):
            if len(value) > MAX_SCAN_OBJECTS - visited - len(stack):
                return False
            size += 2
            if size > MAX_PAYLOAD_BYTES:
                return False
            stack.extend((child, depth + 1) for child in value)
        elif value is None or isinstance(value, (bool, int, float)):
            size += 16
            if size > MAX_PAYLOAD_BYTES:
                return False
        else:
            return False
    return True


def _valid_identifier(kind: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if kind in _UUID_KINDS:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            return None
    if kind == "arxiv":
        return value if _ARXIV_RE.fullmatch(value) else None
    if kind == "external":
        return value
    if kind == "task":
        return value
    return None


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char if char.isprintable() else " " for char in value).strip()
    return cleaned[:limit]


def _record_from_values(
    *,
    kind: str,
    identifier: Any,
    namespace: str | None,
    row: Mapping[str, Any] | None,
    path: str,
    root_status: str,
    source_tool: str,
    turn_id: str,
    tool_call_id: str,
    observation_id: str,
) -> IdentityRecord | None:
    identifier = _valid_identifier(kind, identifier)
    if identifier is None:
        return None
    if kind in {"arxiv", "external"}:
        namespace = namespace or ("arxiv" if kind == "arxiv" else None)
        if not namespace or not _EXTERNAL_NAMESPACE_RE.fullmatch(namespace):
            return None
    else:
        namespace = None

    name = ""
    status = root_status
    related: dict[str, str] = {}
    if row is not None:
        for label_key in ("name", "title", "label"):
            name = _clean_text(row.get(label_key), MAX_LABEL_CHARS)
            if name:
                break
        status = _clean_text(row.get("status"), 64) or root_status
        for relation_key in _RELATION_KEYS:
            if relation_key not in row:
                continue
            relation_kind = relation_key.removesuffix("_id")
            if relation_kind == "paper" or relation_kind == "arxiv":
                relation_kind = "arxiv"
            normalized = _valid_identifier(relation_kind, row.get(relation_key))
            if normalized and relation_key != f"{kind}_id":
                related[relation_key] = normalized
                if len(related) >= MAX_RELATED_IDS:
                    break

    record: IdentityRecord = {
        "kind": kind,
        "id": identifier,
        "observed_status": status or "observed",
        "source_tool": source_tool[:64],
        "source_path": path[:192],
        "turn_id": turn_id[:128],
        "tool_call_id": tool_call_id[:128],
        "observation_id": observation_id,
    }
    if namespace:
        record["namespace"] = namespace
    if name:
        record["name"] = name
    if related:
        record["related"] = dict(list(related.items())[:MAX_RELATED_IDS])
    return record


def _bounds_extraction(
    payload: Mapping[str, Any],
    *,
    source_tool: str,
    turn_id: str,
    tool_call_id: str,
    observation_id: str,
) -> dict[str, Any] | None:
    """Read only Task 2's explicitly retained identity subset when present."""
    bounds = payload.get("_tool_result_bounds")
    if not isinstance(bounds, Mapping) or bounds.get("version") != 1:
        return None
    entries = bounds.get("identity_entries")
    coverage = bounds.get("identity_coverage")
    if not isinstance(entries, list) or not isinstance(coverage, Mapping):
        return {"records": [], "overflow": {"dropped_count": 1, "incomplete": True}}
    root_status = _clean_text(payload.get("status"), 64)
    if not root_status:
        root_status = "failed" if payload.get("error") else "observed"
    records: list[IdentityRecord] = []
    for index, entry in enumerate(entries[:32]):
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        if kind == "paper":
            kind = "arxiv"
        if kind not in {
            "project",
            "document",
            "note",
            "draft",
            "task",
            "arxiv",
            "external",
        }:
            continue
        label = entry.get("label")
        row = {"title": label, "status": entry.get("status")}
        relation = entry.get("related")
        if isinstance(relation, Mapping):
            row.update(relation)
        record = _record_from_values(
            kind=str(kind),
            identifier=entry.get("id"),
            namespace=(
                "arxiv"
                if kind == "arxiv"
                else (
                    _clean_text(entry.get("namespace"), 64)
                    if kind == "external"
                    else None
                )
            ),
            row=row,
            path=_clean_text(entry.get("path"), 192) or f"/_tool_result_bounds/{index}",
            root_status=root_status,
            source_tool=source_tool,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            observation_id=observation_id,
        )
        if record:
            records.append(record)
    omitted = coverage.get("omitted_entries", 0)
    dropped = (
        omitted if isinstance(omitted, int) and not isinstance(omitted, bool) else 1
    )
    incomplete = coverage.get("incomplete") is True
    return {
        "records": records,
        "overflow": {
            "dropped_count": max(0, dropped),
            "incomplete": incomplete,
        },
    }


def extract_tool_identities(
    tool_name: str,
    payload: Any,
    tool_call_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """Extract identities only from typed result fields and bounded records.

    The return object is a transport for `merge_identity_ledger`; it contains
    records, one stable observation ID, and scan-loss metadata.
    """
    tool_name = _clean_text(tool_name, 64) or "unknown_tool"
    tool_call_id = _clean_text(tool_call_id, 128) or "unknown_call"
    turn_id = _clean_text(turn_id, 128) or "unknown_turn"
    observation_id = hashlib.sha256(
        f"{turn_id}\0{tool_call_id}\0{tool_name}".encode("utf-8")
    ).hexdigest()[:32]
    empty = {
        "version": 1,
        "observation_id": observation_id,
        "records": [],
        "overflow": {"dropped_count": 0, "incomplete": False},
    }
    if not isinstance(payload, Mapping):
        return empty

    root_status = _clean_text(payload.get("status"), 64)
    if not root_status:
        root_status = "failed" if payload.get("error") else "observed"
    bounded_result = _bounds_extraction(
        payload,
        source_tool=tool_name,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        observation_id=observation_id,
    )
    if bounded_result is not None:
        return {
            "version": 1,
            "observation_id": observation_id,
            **bounded_result,
        }

    if not _payload_within_scan_bounds(payload):
        return {
            "version": 1,
            "observation_id": observation_id,
            "records": [],
            "overflow": {"dropped_count": 1, "incomplete": True},
        }

    records: list[IdentityRecord] = []
    seen: set[tuple[str, str, str]] = set()
    visited = 0
    scanned_bytes = 0
    incomplete = False

    def add(
        kind: str,
        identifier: Any,
        path: str,
        row: Mapping[str, Any] | None = None,
        namespace: str | None = None,
    ) -> None:
        record = _record_from_values(
            kind=kind,
            identifier=identifier,
            namespace=namespace,
            row=row,
            path=path,
            root_status=root_status,
            source_tool=tool_name,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            observation_id=observation_id,
        )
        if not record:
            return
        key = (record["kind"], record.get("namespace", ""), record["id"])
        if key not in seen:
            seen.add(key)
            records.append(record)

    def inspect_row(row: Any, kind: str, path: str) -> None:
        if isinstance(row, str):
            if kind == "arxiv":
                add(kind, row, path, namespace="arxiv")
            else:
                add(kind, row, path)
            return
        if not isinstance(row, Mapping):
            return
        identifier = next(
            (row.get(key) for key in _ROW_ID_KEYS[kind] if row.get(key) is not None),
            None,
        )
        add(kind, identifier, path, row, namespace="arxiv" if kind == "arxiv" else None)
        if kind == "arxiv":
            linked_document = _valid_identifier("document", row.get("document_id"))
            paper_id = _valid_identifier("arxiv", identifier)
            if linked_document and paper_id:
                add(
                    "document",
                    linked_document,
                    f"{path}/document_id",
                    {"title": row.get("title"), "arxiv_id": paper_id},
                )

    def root_row(field_name: str) -> Mapping[str, Any] | None:
        if tool_name == "create_project" and field_name == "project_id":
            return {"name": payload.get("name")}
        if tool_name == "create_project_note":
            if field_name == "note_id":
                return {
                    "title": payload.get("title"),
                    "project_id": payload.get("project_id"),
                }
            if field_name == "project_id":
                return {"name": payload.get("project_name")}
        if tool_name == "create_draft":
            if field_name == "project_id":
                return {"name": payload.get("project_name")}
            if field_name == "draft_id":
                return {
                    "title": payload.get("draft_title"),
                    "project_id": payload.get("project_id"),
                }
            if field_name == "task_id":
                return {
                    "title": payload.get("draft_title"),
                    "project_id": payload.get("project_id"),
                    "draft_id": payload.get("draft_id"),
                }
        if tool_name == "create_task" and field_name == "task_id":
            return {
                "title": payload.get("task_title"),
                "project_id": payload.get("project_id"),
            }
        return None

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal visited, scanned_bytes, incomplete
        if not isinstance(node, (Mapping, list)):
            if isinstance(node, str):
                measured = _string_bytes_if_bounded(
                    node, MAX_PAYLOAD_BYTES - scanned_bytes
                )
                scanned_bytes = (
                    MAX_PAYLOAD_BYTES + 1
                    if measured is None
                    else scanned_bytes + measured
                )
            if scanned_bytes > MAX_PAYLOAD_BYTES:
                incomplete = True
            return
        if (
            depth > MAX_SCAN_DEPTH
            or visited >= MAX_SCAN_OBJECTS
            or scanned_bytes > MAX_PAYLOAD_BYTES
        ):
            incomplete = True
            return
        visited += 1
        if isinstance(node, list):
            for index, item in enumerate(node):
                if visited >= MAX_SCAN_OBJECTS:
                    incomplete = True
                    return
                walk(item, f"{path}/{index}"[:192], depth + 1)
            return

        for key, value in node.items():
            if not isinstance(key, str):
                continue
            child_path = f"{path}/{key}"[:192]
            collection_kind = _COLLECTIONS.get(key)
            list_kind = _ID_LISTS.get(key)
            if isinstance(value, list) and collection_kind:
                for index, item in enumerate(value):
                    if visited >= MAX_SCAN_OBJECTS:
                        incomplete = True
                        break
                    visited += 1
                    inspect_row(item, collection_kind, f"{child_path}/{index}"[:192])
                continue
            if isinstance(value, list) and list_kind:
                for index, item in enumerate(value):
                    if visited >= MAX_SCAN_OBJECTS:
                        incomplete = True
                        break
                    visited += 1
                    inspect_row(item, list_kind, f"{child_path}/{index}"[:192])
                continue
            if key in _KNOWN_ROOT_IDS and path == "":
                add(_KNOWN_ROOT_IDS[key], value, child_path, root_row(key))
                continue
            if key == "document_ids" and isinstance(value, Mapping):
                # Ingestion adapters may return an explicit paper-ID → document-ID map.
                for paper_id, document_id in list(value.items())[
                    : MAX_SCAN_OBJECTS - visited
                ]:
                    visited += 1
                    paper = _valid_identifier("arxiv", paper_id)
                    document = _valid_identifier("document", document_id)
                    if paper and document:
                        add(
                            "arxiv",
                            paper,
                            f"{child_path}/{paper_id}",
                            namespace="arxiv",
                        )
                        add(
                            "document",
                            document,
                            f"{child_path}/{paper_id}",
                            {"arxiv_id": paper},
                        )
                if len(value) > MAX_SCAN_OBJECTS - visited:
                    incomplete = True
                continue
            if isinstance(value, (Mapping, list)):
                walk(value, child_path, depth + 1)
            elif isinstance(value, str):
                measured = _string_bytes_if_bounded(
                    value, MAX_PAYLOAD_BYTES - scanned_bytes
                )
                scanned_bytes = (
                    MAX_PAYLOAD_BYTES + 1
                    if measured is None
                    else scanned_bytes + measured
                )
                if scanned_bytes > MAX_PAYLOAD_BYTES:
                    incomplete = True

    # Root fields are exact typed IDs; arbitrary prose values are never searched.
    for field_name, kind in _KNOWN_ROOT_IDS.items():
        if field_name in payload:
            add(kind, payload[field_name], f"/{field_name}", root_row(field_name))
    walk(payload, "", 0)

    if tool_name == "search_external_database":
        connector = (
            payload.get("connector") or payload.get("database") or payload.get("source")
        )
        connector = _clean_text(connector, 64)
        rows = (
            payload.get("external_records")
            or payload.get("results")
            or payload.get("records")
        )
        if isinstance(rows, list):
            for index, row in enumerate(rows[:MAX_SCAN_OBJECTS]):
                if not isinstance(row, Mapping):
                    continue
                namespace = (
                    _clean_text(row.get("namespace"), 64)
                    or _clean_text(row.get("source"), 64)
                    or connector
                )
                if not namespace or not _EXTERNAL_NAMESPACE_RE.fullmatch(namespace):
                    continue
                identifier = next(
                    (
                        row.get(key)
                        for key in ("external_id", "accession", "record_id", "id")
                        if row.get(key) is not None
                    ),
                    None,
                )
                add(
                    "external",
                    identifier,
                    f"/external_records/{index}",
                    row,
                    namespace=namespace,
                )

    if len(records) > MAX_LEDGER_RECORDS:
        incomplete = True
    dropped = max(0, len(records) - MAX_LEDGER_RECORDS)
    return {
        "version": 1,
        "observation_id": observation_id,
        "records": records[:MAX_LEDGER_RECORDS],
        "overflow": {
            "dropped_count": dropped or (1 if incomplete else 0),
            "incomplete": incomplete,
        },
    }


def _normalize_reference_set(references: Any) -> set[tuple[str, str, str]]:
    normalized: set[tuple[str, str, str]] = set()
    if not isinstance(references, (list, tuple, set, frozenset)):
        return normalized
    for reference in islice(iter(references), MAX_REFERENCE_IDS):
        if isinstance(reference, str):
            normalized.add(("", "", reference))
        elif isinstance(reference, Mapping):
            identifier = reference.get("id")
            if isinstance(identifier, str):
                normalized.add(
                    (
                        str(reference.get("kind") or ""),
                        str(reference.get("namespace") or ""),
                        identifier,
                    )
                )
        elif isinstance(reference, (list, tuple)) and len(reference) == 3:
            normalized.add(
                (
                    str(reference[0] or ""),
                    str(reference[1] or ""),
                    str(reference[2] or ""),
                )
            )
    return normalized


def _record_key(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    kind, identifier = record.get("kind"), record.get("id")
    if not isinstance(kind, str) or not isinstance(identifier, str):
        return None
    namespace = record.get("namespace", "")
    return kind, namespace if isinstance(namespace, str) else "", identifier


def _is_pinned(record: Mapping[str, Any], refs: set[tuple[str, str, str]]) -> bool:
    return _is_pinned_key(_record_key(record), refs)


def _is_pinned_key(
    key: tuple[str, str, str] | None,
    refs: set[tuple[str, str, str]],
) -> bool:
    if key is None:
        return False
    kind, namespace, identifier = key
    return ("", "", identifier) in refs or key in refs or (kind, "", identifier) in refs


def _completed_mutation(record: Mapping[str, Any], current_turn_id: str | None) -> bool:
    return (
        isinstance(current_turn_id, str)
        and bool(current_turn_id)
        and record.get("turn_id") == current_turn_id
        and record.get("source_tool") in _MUTATING_TOOLS
        and str(record.get("observed_status", "")).casefold() in _COMPLETED_STATES
    )


def _bounded_record(value: Mapping[str, Any], order: int) -> IdentityRecord | None:
    key = _record_key(value)
    if key is None:
        return None
    kind, namespace, identifier = key
    valid = _valid_identifier(kind, identifier)
    if not valid:
        return None
    record: IdentityRecord = {
        "kind": kind,
        "id": valid,
        "observed_status": _clean_text(value.get("observed_status"), 64) or "observed",
        "source_tool": _clean_text(value.get("source_tool"), 64) or "unknown_tool",
        "source_path": _clean_text(value.get("source_path"), 192),
        "turn_id": _clean_text(value.get("turn_id"), 128),
        "tool_call_id": _clean_text(value.get("tool_call_id"), 128),
        "observation_id": _clean_text(value.get("observation_id"), 32),
        "observed_order": order,
    }
    if namespace:
        if kind == "arxiv":
            record["namespace"] = "arxiv"
        elif kind == "external" and _EXTERNAL_NAMESPACE_RE.fullmatch(namespace):
            record["namespace"] = namespace
        else:
            return None
    name = _clean_text(value.get("name"), MAX_LABEL_CHARS)
    if name:
        record["name"] = name
    related = value.get("related")
    if isinstance(related, Mapping):
        pairs: dict[str, str] = {}
        for relation_key in _RELATION_KEYS:
            if relation_key not in related:
                continue
            relation_kind = relation_key.removesuffix("_id")
            if relation_kind in {"paper", "arxiv"}:
                relation_kind = "arxiv"
            value_id = _valid_identifier(relation_kind, related.get(relation_key))
            if value_id:
                pairs[relation_key] = value_id
                if len(pairs) >= MAX_RELATED_IDS:
                    break
        if pairs:
            record["related"] = pairs
    return record


def _ledger_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def merge_identity_ledger(
    existing: Any,
    incoming: Any,
    current_references: Any,
    *,
    current_turn_id: str | None = None,
) -> dict[str, Any]:
    """Merge observations into one bounded last-value checkpoint envelope."""
    valid_existing = (
        isinstance(existing, Mapping) and existing.get("version") == LEDGER_VERSION
    )
    records_in = existing.get("records", []) if valid_existing else []
    processed_in = existing.get("processed_observations", []) if valid_existing else []
    overflow_in = existing.get("overflow", {}) if valid_existing else {}
    records: list[IdentityRecord] = []
    by_key: dict[tuple[str, str, str], IdentityRecord] = {}
    next_order = 0
    existing_record_count = len(records_in) if isinstance(records_in, list) else 0
    existing_records_omitted = max(0, existing_record_count - MAX_LEDGER_RECORDS)
    if isinstance(records_in, list):
        for value in records_in[:MAX_LEDGER_RECORDS]:
            if not isinstance(value, Mapping):
                continue
            raw_order = value.get("observed_order")
            order = (
                min(max(raw_order, 0), 2**31 - 1)
                if isinstance(raw_order, int) and not isinstance(raw_order, bool)
                else next_order
            )
            record = _bounded_record(value, order)
            if record is None:
                continue
            next_order = max(next_order, order + 1)
            key = _record_key(record)
            if key is not None:
                by_key[key] = record
    records = list(by_key.values())
    processed_count = len(processed_in) if isinstance(processed_in, list) else 0
    processed_omitted = max(0, processed_count - MAX_PROCESSED_OBSERVATIONS)
    old_processed = (
        [
            item[:32]
            for item in processed_in[-MAX_PROCESSED_OBSERVATIONS:]
            if isinstance(item, str) and item
        ]
        if isinstance(processed_in, list)
        else []
    )
    processed = list(dict.fromkeys(old_processed))[-MAX_PROCESSED_OBSERVATIONS:]
    processed_set = set(processed)
    prior_dropped = (
        overflow_in.get("dropped_count", 0) if isinstance(overflow_in, Mapping) else 0
    )
    if (
        not isinstance(prior_dropped, int)
        or isinstance(prior_dropped, bool)
        or prior_dropped < 0
    ):
        prior_dropped = 1
    incomplete = (
        bool(overflow_in.get("incomplete"))
        if isinstance(overflow_in, Mapping)
        else False
    )
    newly_dropped = existing_records_omitted
    if processed_omitted:
        newly_dropped += 1
        incomplete = True
    if (
        isinstance(current_references, (list, tuple, set, frozenset))
        and len(current_references) > MAX_REFERENCE_IDS
    ):
        newly_dropped += 1
        incomplete = True

    if isinstance(incoming, Mapping):
        raw_observation = incoming.get("observation_id")
        observation_id = _clean_text(raw_observation, 32)
        if observation_id and observation_id not in processed_set:
            raw_records = incoming.get("records", [])
            if isinstance(raw_records, list):
                retained_records = raw_records[:MAX_LEDGER_RECORDS]
                newly_dropped += max(0, len(raw_records) - len(retained_records))
                if len(raw_records) > len(retained_records):
                    incomplete = True
                for value in retained_records:
                    if not isinstance(value, Mapping):
                        continue
                    record_value = dict(value)
                    record_value.setdefault("observation_id", observation_id)
                    record = _bounded_record(record_value, next_order)
                    if record is None:
                        newly_dropped += 1
                        incomplete = True
                        continue
                    key = _record_key(record)
                    if key is None:
                        newly_dropped += 1
                        incomplete = True
                        continue
                    next_order += 1
                    # A later real observation always replaces prior status; no
                    # global success/failure ranking can hide deletion/failure.
                    by_key[key] = record
            incoming_overflow = incoming.get("overflow", {})
            if isinstance(incoming_overflow, Mapping):
                drop_count = incoming_overflow.get("dropped_count", 0)
                if isinstance(drop_count, int) and not isinstance(drop_count, bool):
                    newly_dropped += max(0, drop_count)
                incomplete = incomplete or incoming_overflow.get("incomplete") is True
            processed.append(observation_id)
            processed = list(dict.fromkeys(processed))[-MAX_PROCESSED_OBSERVATIONS:]
            processed_set = set(processed)

    refs = _normalize_reference_set(current_references)
    candidates = list(by_key.values())
    candidates.sort(
        key=lambda record: (
            _is_pinned(record, refs),
            _completed_mutation(record, current_turn_id),
            int(record.get("observed_order", 0)),
            record.get("kind", ""),
            record.get("namespace", ""),
            record.get("id", ""),
        ),
        reverse=True,
    )

    chosen: list[IdentityRecord] = []
    for record in candidates:
        if len(chosen) >= MAX_LEDGER_RECORDS:
            newly_dropped += 1
            incomplete = True
            continue
        proposed = {
            "version": LEDGER_VERSION,
            "records": [*chosen, record],
            "overflow": {
                "dropped_count": prior_dropped + newly_dropped,
                "incomplete": incomplete,
            },
            "processed_observations": processed,
        }
        if _ledger_size(proposed) <= MAX_LEDGER_BYTES:
            chosen.append(record)
        else:
            newly_dropped += 1
            incomplete = True

    result: dict[str, Any] = {
        "version": LEDGER_VERSION,
        "records": chosen,
        "overflow": {
            "dropped_count": min(2**31 - 1, prior_dropped + newly_dropped),
            "incomplete": incomplete,
        },
        "processed_observations": processed,
    }
    while processed and _ledger_size(result) > MAX_LEDGER_BYTES:
        processed.pop(0)
        result["processed_observations"] = processed
        result["overflow"]["incomplete"] = True
    while chosen and _ledger_size(result) > MAX_LEDGER_BYTES:
        chosen.pop()
        newly_dropped += 1
        result["records"] = chosen
        result["overflow"]["dropped_count"] = min(
            2**31 - 1, prior_dropped + newly_dropped
        )
        result["overflow"]["incomplete"] = True
    if not valid_existing and existing not in (None, {}):
        result["overflow"]["incomplete"] = True
        result["overflow"]["dropped_count"] = max(
            1, result["overflow"]["dropped_count"]
        )
    return result


def identity_references_from_text(
    text: Any,
    *server_ids: Any,
    existing_ledger: Any = None,
) -> list[str]:
    """Return explicit IDs plus bounded exact-name evidence references.

    Matching a complete existing label only prioritizes a record in prompt
    context. It is advisory evidence and never grants access; retrieval tools
    must still apply their normal ownership and scope checks.
    """
    values: list[str] = []
    bounded_text = ""
    if isinstance(text, str):
        bounded_text = text[:MAX_REFERENCE_TEXT_CHARS]
        values.extend(
            re.findall(
                r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                bounded_text,
            )[:MAX_REFERENCE_IDS]
        )
        values.extend(
            re.findall(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", bounded_text)[:MAX_REFERENCE_IDS]
        )
    if (
        bounded_text
        and isinstance(existing_ledger, Mapping)
        and existing_ledger.get("version") == LEDGER_VERSION
    ):
        raw_records = existing_ledger.get("records")
        if isinstance(raw_records, list):
            text_folded = bounded_text.casefold()
            for order, raw_record in enumerate(raw_records[:MAX_LEDGER_RECORDS]):
                if len(values) >= MAX_REFERENCE_IDS:
                    break
                if not isinstance(raw_record, Mapping):
                    continue
                record = _bounded_record(raw_record, order)
                if record is None:
                    continue
                name = record.get("name")
                identifier = record.get("id")
                # Long, complete labels avoid ranking ordinary words or short
                # names. Matching does not authorize later tool access.
                if (
                    isinstance(name, str)
                    and len(name) >= 16
                    and name.casefold() in text_folded
                    and isinstance(identifier, str)
                ):
                    values.append(identifier)
    for value in server_ids:
        if len(values) >= MAX_REFERENCE_IDS:
            break
        if isinstance(value, str) and _valid_identifier("project", value):
            values.append(str(UUID(value)))
    return list(dict.fromkeys(values))[:MAX_REFERENCE_IDS]


def harvest_legacy_tool_messages(
    messages: Sequence[Any],
    existing_ledger: Any = None,
    current_references: Any = None,
    *,
    current_turn_id: str | None = None,
) -> dict[str, Any]:
    """Recover typed identities from bounded, un-compacted legacy tool JSON."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    history_size = len(messages)
    messages = messages[-MAX_HARVEST_MESSAGES:]
    history_omitted = max(0, history_size - len(messages))
    tool_calls: dict[str, tuple[str, str]] = {}
    tool_call_metadata_seen = 0
    tool_metadata_omitted = False
    active_turn_id = ""
    ledger = merge_identity_ledger(
        existing_ledger,
        None,
        current_references,
        current_turn_id=current_turn_id,
    )
    for message in messages:
        if isinstance(message, HumanMessage):
            message_id = getattr(message, "id", None)
            active_turn_id = message_id if isinstance(message_id, str) else ""
        elif isinstance(message, AIMessage):
            raw_calls = getattr(message, "tool_calls", ()) or ()
            remaining = max(0, MAX_HARVEST_TOOL_CALLS - tool_call_metadata_seen)
            for index, call in enumerate(islice(raw_calls, remaining + 1)):
                if index >= remaining:
                    tool_metadata_omitted = True
                    break
                tool_call_metadata_seen += 1
                if not isinstance(call, Mapping):
                    continue
                call_id, tool_name = call.get("id"), call.get("name")
                if isinstance(call_id, str) and isinstance(tool_name, str):
                    tool_calls[call_id] = (tool_name, active_turn_id)
        elif isinstance(message, ToolMessage):
            kwargs = getattr(message, "additional_kwargs", None) or {}
            if kwargs.get("compacted"):
                continue
            content = getattr(message, "content", None)
            if not isinstance(content, str):
                continue
            if content.startswith("[Compacted]"):
                continue
            match = tool_calls.get(getattr(message, "tool_call_id", ""))
            if match is None:
                continue
            tool_name, turn_id = match
            if not turn_id:
                turn_id = f"legacy:{getattr(message, 'tool_call_id', '')}"
            measured = _string_bytes_if_bounded(content, MAX_PAYLOAD_BYTES)
            if measured is None:
                observation_id = hashlib.sha256(
                    f"{turn_id}\0{message.tool_call_id}\0{tool_name}".encode("utf-8")
                ).hexdigest()[:32]
                incoming = {
                    "version": 1,
                    "observation_id": observation_id,
                    "records": [],
                    "overflow": {"dropped_count": 1, "incomplete": True},
                }
            else:
                body = content
                if body.startswith("[deduped"):
                    _marker, separator, body = body.partition("\n")
                    if not separator:
                        continue
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError, RecursionError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                incoming = extract_tool_identities(
                    tool_name,
                    payload,
                    getattr(message, "tool_call_id", ""),
                    turn_id,
                )
            ledger = merge_identity_ledger(
                ledger,
                incoming,
                current_references,
                current_turn_id=current_turn_id,
            )
    if (history_omitted or tool_metadata_omitted) and isinstance(ledger, Mapping):
        ledger = dict(ledger)
        raw_overflow = ledger.get("overflow", {})
        overflow = dict(raw_overflow) if isinstance(raw_overflow, Mapping) else {}
        overflow["dropped_count"] = min(
            2**31 - 1, int(overflow.get("dropped_count", 0) or 0) + 1
        )
        overflow["incomplete"] = True
        ledger["overflow"] = overflow
    return dict(ledger) if isinstance(ledger, Mapping) else {}


def render_identity_ledger(
    ledger: Any,
    *,
    max_bytes: int = MAX_CONTEXT_BYTES - 384,
    current_turn_id: str | None = None,
    current_references: Any = None,
) -> dict[str, Any]:
    """Build a bounded context projection without granting identity authority."""
    valid = isinstance(ledger, Mapping) and ledger.get("version") == LEDGER_VERSION
    records = ledger.get("records", []) if valid else []
    overflow = ledger.get("overflow", {}) if valid else {}
    records = records if isinstance(records, list) else []
    overflow_valid = isinstance(overflow, Mapping)
    overflow = overflow if overflow_valid else {}
    raw_loss = overflow.get("dropped_count", 0)
    if isinstance(raw_loss, int) and not isinstance(raw_loss, bool) and raw_loss >= 0:
        loss = min(2**31 - 1, raw_loss)
    elif (
        isinstance(raw_loss, str)
        and len(raw_loss) <= 10
        and raw_loss.isascii()
        and raw_loss.isdecimal()
    ):
        loss = min(2**31 - 1, int(raw_loss))
    else:
        loss = 1
        overflow_valid = False
    raw_incomplete = overflow.get("incomplete", False)
    if isinstance(raw_incomplete, bool):
        overflow_incomplete = raw_incomplete
    else:
        overflow_incomplete = True
        overflow_valid = False
    projected: list[dict[str, Any]] = []
    scanned_record_count = min(len(records), MAX_LEDGER_RECORDS)
    refs = _normalize_reference_set(current_references)
    bounded_records = [
        (
            index,
            value,
            _record_key(value) if isinstance(value, Mapping) else None,
        )
        for index, value in enumerate(records[:MAX_LEDGER_RECORDS])
    ]
    bounded_records.sort(
        key=lambda item: (
            _is_pinned_key(item[2], refs),
            (
                _completed_mutation(item[1], current_turn_id)
                if isinstance(item[1], Mapping)
                else False
            ),
            (
                item[1].get("observed_order", item[0])
                if isinstance(item[1], Mapping)
                and isinstance(item[1].get("observed_order", item[0]), int)
                and not isinstance(item[1].get("observed_order", item[0]), bool)
                else item[0]
            ),
            -item[0],
        ),
        reverse=True,
    )
    for _, value, key in bounded_records:
        if not isinstance(value, Mapping):
            continue
        if key is None or not _valid_identifier(key[0], key[2]):
            continue
        kind, namespace, identifier = key
        if kind == "external":
            if not _EXTERNAL_NAMESPACE_RE.fullmatch(namespace):
                continue
        elif kind == "arxiv":
            if namespace not in {"", "arxiv"}:
                continue
            namespace = "arxiv"
        elif namespace:
            continue
        record: dict[str, Any] = {
            "kind": kind,
            **({"namespace": namespace} if namespace else {}),
            "id": identifier,
            "observed_status": _clean_text(value.get("observed_status"), 64),
            "source_tool": _clean_text(value.get("source_tool"), 64),
            "turn_id": _clean_text(value.get("turn_id"), 128),
            "tool_call_id": _clean_text(value.get("tool_call_id"), 128),
        }
        name = _clean_text(value.get("name"), MAX_LABEL_CHARS)
        if name:
            record["name"] = name
        related = value.get("related")
        if isinstance(related, Mapping):
            safe_related = {
                key: _valid_identifier(
                    (
                        "arxiv"
                        if key in {"paper_id", "arxiv_id"}
                        else key.removesuffix("_id")
                    ),
                    item,
                )
                for key, item in related.items()
                if key in _RELATION_KEYS
            }
            safe_related = {key: item for key, item in safe_related.items() if item}
            if safe_related:
                record["related"] = dict(list(safe_related.items())[:MAX_RELATED_IDS])
        proposed = {
            "version": LEDGER_VERSION,
            "records": [*projected, record],
            "persisted_record_count": len(records),
            "projected_record_count": len(projected) + 1,
            "omitted_record_count": max(0, len(records) - len(projected) - 1),
            "ledger_loss_count": loss,
            "incomplete": overflow_incomplete or not overflow_valid or not valid,
        }
        if _ledger_size(proposed) <= max_bytes:
            projected.append(record)

    incomplete = (
        overflow_incomplete
        or not overflow_valid
        or not valid
        or len(projected) < len(records)
        or len(records) > scanned_record_count
    )
    return {
        "version": LEDGER_VERSION,
        "records": projected,
        "persisted_record_count": len(records),
        "projected_record_count": len(projected),
        "omitted_record_count": max(0, len(records) - len(projected)),
        "ledger_loss_count": loss,
        "incomplete": incomplete,
    }


__all__ = [
    "IdentityRecord",
    "LEDGER_VERSION",
    "MAX_CONTEXT_BYTES",
    "MAX_HARVEST_TOOL_CALLS",
    "MAX_LEDGER_BYTES",
    "MAX_LEDGER_RECORDS",
    "extract_tool_identities",
    "harvest_legacy_tool_messages",
    "identity_references_from_text",
    "merge_identity_ledger",
    "render_identity_ledger",
]
