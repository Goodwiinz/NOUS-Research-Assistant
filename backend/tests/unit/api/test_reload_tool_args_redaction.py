"""Persisted tool traces are safely projected at browser-facing boundaries.

#1046 redacted the live SSE ``tool_start`` preview but rows persisted by
``_persist_assistant_message`` keep raw args, and both message-serving
funnels (agent ``get_thread_messages`` and threads ``_format_message_response``)
returned ``msg.tool_executions`` verbatim — so a page reload re-leaked the
PII the live stream had redacted. General workspace APIs additionally need an
allowlist projection because public workspace readers can be cross-tenant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.agent._pii_redact import (
    public_tool_execution_activity,
    redact_tool_executions,
)

pytestmark = pytest.mark.unit

PII_ARGS = {
    "query": "email bob@example.com about doc",
    "nested": {"token": "sk-ant-abcdefghijklmnopqrstuvwxyz1234"},
    "max_results": 5,
}


def test_args_redacted_structure_preserved():
    entries = [
        {
            "id": "te-1",
            "tool_name": "search_documents",
            "args": PII_ARGS,
            "status": "success",
            "result": "ok",
        }
    ]
    out = redact_tool_executions(entries)
    args = out[0]["args"]
    assert "bob@example.com" not in str(args)
    assert "<email>" in args["query"]
    assert "sk-ant-" not in str(args)
    assert args["max_results"] == 5
    # non-args fields untouched; input not mutated in place
    assert out[0]["result"] == "ok"
    assert entries[0]["args"]["query"].startswith("email bob@example.com")


def test_none_and_empty_pass_through():
    assert redact_tool_executions(None) is None
    assert redact_tool_executions([]) == []


def test_legacy_non_dict_entry_survives():
    # A legacy/unknown entry shape must not 500 the messages endpoint.
    out = redact_tool_executions(["opaque-legacy-entry"])
    assert out == ["opaque-legacy-entry"]


def test_public_activity_omits_raw_trace_data_and_unknown_fields():
    entries = [
        {
            "id": "te-1",
            "tool_name": "search_documents",
            "tool_display_name": "Search documents",
            "args": PII_ARGS,
            "status": "success",
            "result": {"chunks": [{"text": "private organization content"}]},
            "error": "secret internal failure",
            "duration_ms": 42,
            "future_sensitive_field": "must not pass through",
        }
    ]

    assert public_tool_execution_activity(entries) == [
        {
            "id": "te-1",
            "tool_name": "search_documents",
            "tool_display_name": "Search documents",
            "status": "success",
            "duration_ms": 42,
        }
    ]


def test_public_activity_fails_closed_for_unknown_legacy_shapes():
    assert public_tool_execution_activity(None) is None
    assert public_tool_execution_activity({"tool_name": "not-a-list"}) == []
    assert public_tool_execution_activity(["opaque", {"result": "secret"}]) == []


def test_serving_funnels_project_public_activity():
    # Pin both general workspace funnels to the allowlist projection. The
    # owner-only agent history endpoint retains its separate args redaction.
    source_root = Path(__file__).resolve().parents[3] / "src" / "api"
    agent_history = (source_root / "agent" / "execute.py").read_text()
    legacy_threads = (source_root / "threads" / "threads.py").read_text()
    presenters = (
        source_root / "threads" / "workspace_routes" / "presenters.py"
    ).read_text()

    assert "redact_tool_executions(msg.tool_executions)" in agent_history
    projection = "public_tool_execution_activity(message.tool_executions)"
    assert projection in legacy_threads
    assert projection in presenters
