from __future__ import annotations

import pytest

from src.api.agent.streaming import _changed_retrieved_context_snapshot

pytestmark = pytest.mark.unit


def _end(name: str, contexts: list[dict]) -> dict:
    return {
        "event": "on_chain_end",
        "name": name,
        "data": {"output": {"retrieved_contexts": contexts}},
    }


def test_changed_snapshots_are_node_agnostic_cumulative_and_deduplicated() -> None:
    first = [{"document_id": "one", "title": "First", "content": "a"}]
    second = [*first, {"document_id": "two", "title": "Second", "content": "b"}]

    snapshot, fingerprint = _changed_retrieved_context_snapshot(
        _end("rag_node", first), None
    )
    assert snapshot == first

    duplicate, fingerprint = _changed_retrieved_context_snapshot(
        _end("wrapper_node", first), fingerprint
    )
    assert duplicate is None

    snapshot, fingerprint = _changed_retrieved_context_snapshot(
        _end("filtered_tool_node", second), fingerprint
    )
    assert snapshot == second

    absent, final_fingerprint = _changed_retrieved_context_snapshot(
        {"event": "on_tool_end", "data": {}}, fingerprint
    )
    assert absent is None
    assert final_fingerprint == fingerprint
