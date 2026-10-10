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
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.models.workspace import Workspace
from src.services.agent._pii_redact import (
    public_tool_execution_activity,
    redact_tool_args,
    redact_tool_executions,
    tool_executions_for_viewer,
)
from src.services.threads import workspace_access

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


TRACE_ENTRY = {
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


def test_viewer_funnel_trusted_keeps_redacted_args_result_and_error():
    # Owner/member: what develop always served — PII-redacted args, raw
    # result/error — but never unknown persisted keys.
    [entry] = tool_executions_for_viewer([TRACE_ENTRY], trusted=True)

    assert entry["args"] == redact_tool_args(PII_ARGS)
    assert "bob@example.com" not in entry["args"]["query"]
    assert "sk-ant-" not in str(entry["args"])
    assert entry["result"] == {"chunks": [{"text": "private organization content"}]}
    assert entry["error"] == "secret internal failure"
    assert entry["tool_name"] == "search_documents"
    assert entry["duration_ms"] == 42
    assert "future_sensitive_field" not in entry


def test_viewer_funnel_untrusted_is_the_public_projection():
    assert tool_executions_for_viewer([TRACE_ENTRY], trusted=False) == (
        public_tool_execution_activity([TRACE_ENTRY])
    )
    [entry] = tool_executions_for_viewer([TRACE_ENTRY], trusted=False)
    assert not ({"args", "result", "error"} & entry.keys())


def test_viewer_funnel_fails_closed_for_malformed_entries_in_both_tiers():
    malformed = ["opaque", {"result": "secret"}, {"tool_name": 7, "error": "x"}]
    for trusted in (True, False):
        assert tool_executions_for_viewer(None, trusted=trusted) is None
        assert tool_executions_for_viewer({"tool_name": "x"}, trusted=trusted) == []
        assert tool_executions_for_viewer(malformed, trusted=trusted) == []


def test_trust_helpers_fail_closed_without_the_access_graph():
    # No ``thread.conversation.workspace`` graph (wrong shape, None, or an
    # unloaded relationship) must resolve to the PUBLIC projection, never
    # raise and never default to trusted.
    uid = uuid4()
    assert workspace_access.thread_viewer_is_trusted(SimpleNamespace(), uid) is False
    assert workspace_access.thread_viewer_is_trusted(None, uid) is False
    assert workspace_access.message_viewer_is_trusted(SimpleNamespace(), uid) is False
    assert (
        workspace_access.message_viewer_is_trusted(SimpleNamespace(thread=None), uid)
        is False
    )


def test_workspace_trust_orders_owner_before_public_and_deleted_grants_nothing():
    owner, stranger = uuid4(), uuid4()
    ws = Workspace(
        id=uuid4(), name="w", owner_id=owner, organization_id=uuid4(), is_public=True
    )
    thread = SimpleNamespace(id=uuid4(), conversation=SimpleNamespace(workspace=ws))

    # The owner of a public workspace is "owner", not "public".
    assert workspace_access.workspace_trust(ws, owner) == "owner"
    assert workspace_access.thread_viewer_is_trusted(thread, owner) is True
    assert workspace_access.workspace_trust(ws, stranger) == "public"
    assert workspace_access.thread_viewer_is_trusted(thread, stranger) is False
    # Same inputs as user_can_access_workspace, so the two can never disagree.
    assert workspace_access.user_can_access_workspace(ws, stranger) is True

    ws.is_deleted = True
    assert workspace_access.workspace_trust(ws, owner) is None
    assert workspace_access.thread_viewer_is_trusted(thread, owner) is False
    assert workspace_access.user_can_access_workspace(ws, owner) is False


def test_serving_funnels_project_by_viewer_trust():
    # Pin all three browser-serving emitters to the viewer-aware funnel so none
    # can drift back to raw ``msg.tool_executions`` or to a projection that
    # ignores the access grant (public-only viewer vs. owner/member).
    source_root = Path(__file__).resolve().parents[3] / "src" / "api"
    emitters = {
        "agent history": (source_root / "agent" / "execute.py").read_text(),
        "legacy threads": (source_root / "threads" / "threads.py").read_text(),
        "presenters": (
            source_root / "threads" / "workspace_routes" / "presenters.py"
        ).read_text(),
    }
    for name, source in emitters.items():
        assert "tool_executions_for_viewer(" in source, name
        assert "trusted=trusted" in source, name
        assert "redact_tool_executions(msg.tool_executions)" not in source, name
        assert "public_tool_execution_activity(message." not in source, name
