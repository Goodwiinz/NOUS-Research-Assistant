"""Bounded, checkpoint-safe tool identity memory contracts."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

pytestmark = pytest.mark.unit


def test_extractor_uses_typed_records_and_keeps_external_namespaces_separate() -> None:
    from src.services.agent.identity_ledger import extract_tool_identities

    project_id, document_id, note_id, draft_id = (str(uuid.uuid4()) for _ in range(4))
    unrelated = str(uuid.uuid4())
    payload = {
        "status": "failed",
        "project_id": project_id,
        "documents": [
            {
                "document_id": document_id,
                "title": "Document A",
                "project_id": project_id,
            }
        ],
        "notes": [{"note_id": note_id, "title": "Note A"}],
        "drafts": [{"draft_id": draft_id, "title": "Draft A"}],
        "task_id": "a7b9c2d4e6f8",
        "papers": [
            {
                "paper_id": "2401.01234v2",
                "title": "Paper A",
                "document_id": document_id,
            }
        ],
        "external_records": [
            {"namespace": "sec_edgar", "id": "0000320193", "title": "Apple 10-K"},
            {"namespace": "uniprot", "id": "0000320193", "title": "Protein row"},
        ],
        "message": f"A prose UUID is not an identity: {unrelated}",
    }

    extraction = extract_tool_identities(
        "search_external_database", payload, "call-7", "turn-3"
    )
    records = extraction["records"]
    keyed = {
        (item["kind"], item.get("namespace"), item["id"]): item for item in records
    }

    assert ("project", None, project_id) in keyed
    assert ("document", None, document_id) in keyed
    assert keyed[("document", None, document_id)]["related"]["project_id"] == project_id
    assert ("note", None, note_id) in keyed
    assert ("draft", None, draft_id) in keyed
    assert ("task", None, "a7b9c2d4e6f8") in keyed
    assert ("arxiv", "arxiv", "2401.01234v2") in keyed
    assert ("external", "sec_edgar", "0000320193") in keyed
    assert ("external", "uniprot", "0000320193") in keyed
    assert all(unrelated != item["id"] for item in records)
    assert all(item["observed_status"] == "failed" for item in records)
    assert all(item["tool_call_id"] == "call-7" for item in records)
    assert extraction["observation_id"]


def test_result_receipt_replays_only_the_retained_task_two_identity_subset() -> None:
    from src.services.agent.identity_ledger import extract_tool_identities

    retained_id, clipped_id = str(uuid.uuid4()), str(uuid.uuid4())
    payload = {
        "documents": [{"document_id": clipped_id, "title": "clipped"}],
        "_tool_result_bounds": {
            "version": 1,
            "identity_entries": [
                {
                    "kind": "document",
                    "id": retained_id,
                    "path": "/documents/0",
                    "label": "retained",
                }
            ],
            "identity_coverage": {
                "observed_entries": 2,
                "retained_entries": 1,
                "omitted_entries": 1,
                "incomplete": True,
                "unseen_count_known": True,
            },
        },
    }
    extracted = extract_tool_identities("list_project_documents", payload, "c1", "t1")
    assert [record["id"] for record in extracted["records"]] == [retained_id]
    assert extracted["overflow"] == {"dropped_count": 1, "incomplete": True}


def test_result_receipt_replays_external_rows_with_exact_source_namespaces() -> None:
    from src.services.agent.identity_ledger import extract_tool_identities

    payload = {
        "results": [
            {"id": "shared-accession-7", "source": "sec_edgar", "title": "SEC"},
            {"id": "shared-accession-7", "source": "uniprot", "title": "UniProt"},
        ],
        "connectors_searched": ["sec_edgar", "uniprot"],
        "_tool_result_bounds": {
            "version": 1,
            "identity_entries": [
                {
                    "kind": "external",
                    "namespace": "sec_edgar",
                    "id": "shared-accession-7",
                    "path": "/results/0",
                    "label": "SEC",
                },
                {
                    "kind": "external",
                    "namespace": "uniprot",
                    "id": "shared-accession-7",
                    "path": "/results/1",
                    "label": "UniProt",
                },
            ],
            "identity_coverage": {
                "observed_entries": 2,
                "retained_entries": 2,
                "omitted_entries": 0,
                "incomplete": False,
            },
        },
    }

    extracted = extract_tool_identities(
        "search_external_database", payload, "external-call", "turn-external"
    )

    assert {
        (record["namespace"], record["id"], record["name"])
        for record in extracted["records"]
    } == {
        ("sec_edgar", "shared-accession-7", "SEC"),
        ("uniprot", "shared-accession-7", "UniProt"),
    }


def test_ledger_merge_is_idempotent_prioritizes_references_and_keeps_latest_status() -> (
    None
):
    from src.services.agent.identity_ledger import (
        extract_tool_identities,
        merge_identity_ledger,
    )

    ids = [str(uuid.uuid4()) for _ in range(3)]
    first = extract_tool_identities(
        "list_projects",
        {
            "projects": [
                {"id": value, "name": f"Project {index}"}
                for index, value in enumerate(ids)
            ]
        },
        "call-1",
        "turn-1",
    )
    ledger = merge_identity_ledger({}, first, [ids[2]])
    replay = merge_identity_ledger(ledger, first, [ids[2]])
    assert replay == ledger
    assert replay["records"][0]["id"] == ids[2]

    later = extract_tool_identities(
        "delete_project",
        {"project_id": ids[2], "status": "deleted"},
        "call-2",
        "turn-2",
    )
    updated = merge_identity_ledger(replay, later, [ids[2]])
    assert updated["records"][0]["observed_status"] == "deleted"
    assert len(updated["processed_observations"]) == 2


def test_ledger_caps_record_bytes_names_related_ids_and_observations() -> None:
    from src.services.agent.identity_ledger import (
        extract_tool_identities,
        merge_identity_ledger,
    )

    ids = [str(uuid.uuid4()) for _ in range(180)]
    extraction = extract_tool_identities(
        "list_projects",
        {
            "projects": [
                {
                    "id": value,
                    "title": "T" * 500,
                    "related_ids": {
                        str(index): str(uuid.uuid4()) for index in range(30)
                    },
                }
                for value in ids
            ]
        },
        "call-many",
        "turn-many",
    )
    ledger = merge_identity_ledger({}, extraction, ids)
    serialized = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    assert len(ledger["records"]) <= 128
    assert len(serialized.encode("utf-8")) <= 24_576
    assert len(ledger["processed_observations"]) <= 256
    assert all(len(item.get("name", "")) <= 240 for item in ledger["records"])
    assert all(len(item.get("related", {})) <= 16 for item in ledger["records"])
    assert ledger["overflow"]["incomplete"] is True


def test_mutation_priority_covers_live_writers_and_excludes_failed_or_pending() -> None:
    from src.services.agent.identity_ledger import (
        _MUTATING_TOOLS,
        extract_tool_identities,
        merge_identity_ledger,
    )
    from src.services.agent.tools import TOOL_REGISTRY

    live_mutations = {
        descriptor.name
        for descriptor in TOOL_REGISTRY.descriptors
        if descriptor.effect_mode.value != "read_only"
    }
    assert _MUTATING_TOOLS == live_mutations

    for status in ("failed", "pending"):
        mutation_id, read_id = str(uuid.uuid4()), str(uuid.uuid4())
        mutation = extract_tool_identities(
            "create_project",
            {"project_id": mutation_id, "status": status},
            f"{status}-call",
            status,
        )
        ledger = merge_identity_ledger({}, mutation, [])
        read = extract_tool_identities(
            "list_projects",
            {"projects": [{"id": read_id, "name": "Later read"}]},
            f"{status}-read",
            status,
        )
        merged = merge_identity_ledger(ledger, read, [])

        assert merged["records"][0]["id"] == read_id
        assert (
            next(record for record in merged["records"] if record["id"] == mutation_id)[
                "observed_status"
            ]
            == status
        )


def test_extractor_stops_at_bounded_legacy_payload_accounting() -> None:
    from src.services.agent.identity_ledger import (
        MAX_SCAN_OBJECTS,
        extract_tool_identities,
    )

    class LargeMapping(Mapping[str, str]):
        def __init__(self) -> None:
            self.visited = 0

        def __getitem__(self, key: str) -> str:
            return "value"

        def __iter__(self) -> Iterator[str]:
            for index in range(MAX_SCAN_OBJECTS * 3):
                self.visited += 1
                yield f"field-{index}"

        def __len__(self) -> int:
            return MAX_SCAN_OBJECTS * 3

    payload = LargeMapping()
    extracted = extract_tool_identities("legacy_tool", payload, "call-1", "turn-1")

    assert payload.visited <= MAX_SCAN_OBJECTS
    assert extracted["records"] == []
    assert extracted["overflow"]["incomplete"] is True


def test_context_projection_marks_persisted_record_omissions_incomplete() -> None:
    from src.services.agent.identity_ledger import render_identity_ledger

    records = [
        {
            "kind": "project",
            "id": str(uuid.uuid4()),
            "observed_status": "observed",
            "source_tool": "list_projects",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
        }
        for _ in range(2)
    ]

    projection = render_identity_ledger(
        {"version": 1, "records": records, "overflow": {}}, max_bytes=100
    )

    assert projection["projected_record_count"] == 0
    assert projection["omitted_record_count"] == 2
    assert projection["incomplete"] is True


def test_hostile_checkpoint_ledger_iteration_is_bounded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import identity_ledger

    records = [
        {
            "kind": "project",
            "id": str(uuid.uuid4()),
            "observed_status": "observed",
            "source_tool": "list_projects",
            "turn_id": "turn-large",
            "tool_call_id": f"call-{index}",
            "observed_order": index,
        }
        for index in range(300)
    ]
    processed = [f"observation-{index}" for index in range(2000)]
    bounded_calls = 0
    original_bounded_record = identity_ledger._bounded_record

    def counted_bounded_record(value: object, order: int) -> Any:
        nonlocal bounded_calls
        bounded_calls += 1
        return original_bounded_record(value, order)  # type: ignore[arg-type]

    monkeypatch.setattr(identity_ledger, "_bounded_record", counted_bounded_record)
    merged = identity_ledger.merge_identity_ledger(
        {
            "version": 1,
            "records": records,
            "processed_observations": processed,
            "overflow": {},
        },
        None,
        [],
    )

    assert bounded_calls <= identity_ledger.MAX_LEDGER_RECORDS
    assert len(merged["records"]) <= identity_ledger.MAX_LEDGER_RECORDS
    assert (
        len(merged["processed_observations"])
        <= identity_ledger.MAX_PROCESSED_OBSERVATIONS
    )
    assert merged["overflow"]["incomplete"] is True
    assert merged["overflow"]["dropped_count"] >= 172

    key_calls = 0
    original_record_key = identity_ledger._record_key

    def counted_record_key(value: object) -> Any:
        nonlocal key_calls
        key_calls += 1
        return original_record_key(value)  # type: ignore[arg-type]

    monkeypatch.setattr(identity_ledger, "_record_key", counted_record_key)
    projection = identity_ledger.render_identity_ledger(
        {"version": 1, "records": records, "overflow": {}}, max_bytes=6144
    )
    assert key_calls <= identity_ledger.MAX_LEDGER_RECORDS
    assert projection["persisted_record_count"] == len(records)
    assert (
        projection["omitted_record_count"]
        >= len(records) - identity_ledger.MAX_LEDGER_RECORDS
    )
    assert projection["incomplete"] is True


def test_reference_extraction_scans_only_a_bounded_user_text_prefix() -> None:
    from src.services.agent.identity_ledger import identity_references_from_text

    early_id, late_id = str(uuid.uuid4()), str(uuid.uuid4())
    text = f"Use source {early_id}. " + ("x" * 40_000) + f"Also use {late_id}."

    references = identity_references_from_text(text)

    assert early_id in references
    assert late_id not in references


def test_projection_rejects_malformed_external_namespace_and_normalizes_arxiv() -> None:
    from src.services.agent.identity_ledger import render_identity_ledger

    projection = render_identity_ledger(
        {
            "version": 1,
            "records": [
                {
                    "kind": "external",
                    "namespace": "bad namespace",
                    "id": "record-1",
                    "observed_status": "completed",
                },
                {
                    "kind": "arxiv",
                    "id": "2401.01234",
                    "observed_status": "observed",
                },
            ],
            "overflow": {"dropped_count": 0, "incomplete": False},
        }
    )

    assert [record["id"] for record in projection["records"]] == ["2401.01234"]
    assert projection["records"][0]["namespace"] == "arxiv"
    assert projection["omitted_record_count"] == 1
    assert projection["incomplete"] is True


def test_legacy_harvest_caps_tool_call_metadata_and_marks_loss() -> None:
    from src.services.agent import identity_ledger

    class CountingCalls(list):
        visited = 0

        def __iter__(self) -> Iterator[Any]:
            for item in super().__iter__():
                self.visited += 1
                yield item

    message = AIMessage(content="")
    calls = CountingCalls(
        {"id": f"call-{index}", "name": "list_projects", "args": {}}
        for index in range(identity_ledger.MAX_HARVEST_TOOL_CALLS * 3)
    )
    message.tool_calls = calls

    harvested = identity_ledger.harvest_legacy_tool_messages([message])

    assert calls.visited <= identity_ledger.MAX_HARVEST_TOOL_CALLS + 1
    assert harvested["overflow"]["incomplete"] is True
    assert harvested["overflow"]["dropped_count"] >= 1


@pytest.mark.parametrize("dropped_count", ["not-an-integer", object(), -1])
def test_identity_projection_fails_closed_on_malformed_overflow_counts(
    dropped_count: object,
) -> None:
    from src.services.agent.identity_ledger import render_identity_ledger

    projection = render_identity_ledger(
        {
            "version": 1,
            "records": [],
            "overflow": {"dropped_count": dropped_count, "incomplete": False},
        }
    )

    assert projection["incomplete"] is True
    assert projection["ledger_loss_count"] >= 1


@pytest.mark.parametrize("node_kind", ["main", "filtered"])
async def test_tool_nodes_checkpoint_fresh_identity_observations(
    monkeypatch: pytest.MonkeyPatch, node_kind: str
) -> None:
    from src.services.agent import _nodes_tools
    from src.services.agent.tools import TOOL_REGISTRY

    document_id = str(uuid.uuid4())
    call = {
        "id": "identity-call",
        "name": "search_documents",
        "args": {"query": "saved"},
    }
    message = ToolMessage(
        content=json.dumps(
            {"documents": [{"document_id": document_id, "title": "Saved source"}]}
        ),
        tool_call_id=call["id"],
    )

    async def execute(tc: dict, *_args: object) -> dict:
        result_payload = {
            "documents": [{"document_id": document_id, "title": "Saved source"}]
        }
        return {
            "message": ToolMessage(
                content=json.dumps(result_payload), tool_call_id=tc["id"]
            ),
            "execution": {
                "id": tc["id"],
                "tool_name": tc["name"],
                "status": "completed",
                "result": result_payload,
            },
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }

    execute_mock = AsyncMock(side_effect=execute)
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute_mock)
    state = {
        "messages": [
            HumanMessage(content="Read the saved source", id="turn-identity"),
            AIMessage(content="", tool_calls=[call]),
        ],
        "tool_executions": [],
        "runtime_tool_names": list(TOOL_REGISTRY.available_descriptor_names()),
        "tool_registry_hash": TOOL_REGISTRY.metadata_snapshot()["hash"],
        "tool_registry_version": TOOL_REGISTRY.metadata_snapshot()["version"],
        "error_count": 0,
        "last_error": "",
        "page_context": {},
        "identity_current_references": [],
        "identity_ledger": {},
        "tool_operation_turn_id": "turn-identity",
    }

    if node_kind == "main":
        result = await _nodes_tools.tool_node(state, {})
    else:
        result = await _nodes_tools.make_filtered_tool_node({"search_documents"})(
            state, {}
        )

    execute_mock.assert_awaited_once()
    assert result["messages"][0].tool_call_id == message.tool_call_id
    assert result["identity_ledger"]["records"][0]["id"] == document_id


@pytest.mark.parametrize("node_kind", ["main", "filtered"])
async def test_tool_nodes_harvest_cached_receipt_identity_subset(
    monkeypatch: pytest.MonkeyPatch, node_kind: str
) -> None:
    from src.services.agent import _nodes_tools
    from src.services.agent.tools import TOOL_REGISTRY

    retained_id, clipped_id = str(uuid.uuid4()), str(uuid.uuid4())
    args = {"query": "repeat"}
    prior_call = {"id": "receipt-call", "name": "search_documents", "args": args}
    current_call = {"id": "replay-call", "name": "search_documents", "args": args}
    receipt = {
        "documents": [{"document_id": clipped_id, "title": "Clipped"}],
        "_tool_result_bounds": {
            "version": 1,
            "identity_entries": [
                {"kind": "document", "id": retained_id, "path": "/documents/0"}
            ],
            "identity_coverage": {
                "observed_entries": 2,
                "retained_entries": 1,
                "omitted_entries": 1,
                "incomplete": True,
            },
        },
    }
    execute_mock = AsyncMock()
    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", execute_mock)
    state = {
        "messages": [
            HumanMessage(content="Repeat the lookup", id="turn-replay"),
            AIMessage(content="", tool_calls=[prior_call]),
            ToolMessage(content=json.dumps(receipt), tool_call_id="receipt-call"),
            AIMessage(content="", tool_calls=[current_call]),
        ],
        "tool_executions": [
            {
                "id": "receipt-call",
                "tool_name": "search_documents",
                "args": args,
                "status": "completed",
                "result": receipt,
            }
        ],
        "runtime_tool_names": list(TOOL_REGISTRY.available_descriptor_names()),
        "tool_registry_hash": TOOL_REGISTRY.metadata_snapshot()["hash"],
        "tool_registry_version": TOOL_REGISTRY.metadata_snapshot()["version"],
        "error_count": 0,
        "last_error": "",
        "page_context": {},
        "identity_current_references": [],
        "identity_ledger": {},
        "tool_operation_turn_id": "turn-replay",
    }

    if node_kind == "main":
        result = await _nodes_tools.tool_node(state, {})
    else:
        result = await _nodes_tools.make_filtered_tool_node({"search_documents"})(
            state, {}
        )

    execute_mock.assert_not_awaited()
    record_ids = {record["id"] for record in result["identity_ledger"]["records"]}
    assert retained_id in record_ids
    assert clipped_id not in record_ids
    assert result["identity_ledger"]["overflow"]["incomplete"] is True


def test_compactor_harvests_middle_tool_identity_without_compacting() -> None:
    from src.services.agent.compactor import make_compactor_node

    document_id = str(uuid.uuid4())
    middle = ToolMessage(
        content=json.dumps(
            {"documents": [{"document_id": document_id, "title": "Middle result"}]}
        ),
        tool_call_id="call-middle",
        id="tool-middle",
    )
    state = {
        "messages": [
            HumanMessage(content="remember the selected document"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-middle", "name": "search_documents", "args": {}}
                ],
            ),
            middle,
            AIMessage(content="answer"),
        ],
        "compaction_count": 0,
        "identity_ledger": {},
        "identity_current_references": [],
    }

    async def run() -> dict:
        return cast(dict[str, Any], await make_compactor_node()(state, {}))

    result = asyncio.run(run())
    assert result["identity_ledger"]["records"][0]["id"] == document_id
    assert result.get("messages") is None


def test_trim_latest_human_fallback_does_not_prevent_ledger_recovery() -> None:
    from src.services.agent.compactor import make_compactor_node, trim_model_history

    document_id = str(uuid.uuid4())
    messages = [
        HumanMessage(content="older request"),
        AIMessage(
            content="",
            tool_calls=[{"id": "old-call", "name": "search_documents", "args": {}}],
        ),
        ToolMessage(
            content=json.dumps(
                {"documents": [{"document_id": document_id, "title": "Recovered"}]}
            ),
            tool_call_id="old-call",
            id="old-result",
        ),
        HumanMessage(content="latest" + "x" * 140_000),
    ]
    assert len(trim_model_history(messages)) == 1
    state = {"messages": messages, "compaction_count": 0}

    async def run() -> dict:
        return cast(dict[str, Any], await make_compactor_node()(state, {}))

    result = asyncio.run(run())
    assert result["identity_ledger"]["records"][0]["id"] == document_id


async def test_preprocessing_harvests_legacy_identity_before_first_model_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_classify

    document_id = str(uuid.uuid4())
    messages = [
        HumanMessage(content="Use the saved document", id="turn-legacy"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-legacy", "name": "search_documents", "args": {}}],
        ),
        ToolMessage(
            content=json.dumps(
                {"documents": [{"document_id": document_id, "title": "Saved"}]}
            ),
            tool_call_id="call-legacy",
            id="result-legacy",
        ),
        HumanMessage(
            content="Continue from that result" + "x" * 140_000, id="turn-current"
        ),
    ]

    async def _empty(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(_nodes_classify, "rag_node", _empty)
    monkeypatch.setattr(_nodes_classify, "_classify_core", _empty)
    monkeypatch.setattr(_nodes_classify, "memory_retrieval_node", _empty)
    monkeypatch.setattr(_nodes_classify, "tag_trace_intent", lambda _intent: None)

    result = await _nodes_classify.preprocessing_node({"messages": messages}, {})

    assert result["identity_ledger"]["records"][0]["id"] == document_id
    assert result["tool_operation_turn_id"] == "turn-current"

    from src.services.agent.compactor import trim_model_history
    from src.services.agent.runtime_context import render_dynamic_context

    trimmed = trim_model_history(messages)
    assert len(trimmed) == 1 and isinstance(trimmed[0], HumanMessage)
    dynamic = render_dynamic_context(
        {"messages": trimmed, **result},
        {},
        resolved_model="test-model",
    )
    assert document_id in dynamic
