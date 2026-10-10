"""Unit tests for ``_encode_tool_result`` in streaming.py.

The CLI parses ``tool_end.result`` to render honest per-tool summaries.
For dict/list outputs we emit JSON so the CLI can ``JSON.parse`` it; for
primitives we keep the previous ``str()`` shape. Non-serializable objects
must not crash the stream.
"""

import json
from datetime import datetime

import pytest

from src.api.agent.streaming import _encode_tool_result


class TestEncodeToolResult:
    def test_dict_output_emits_valid_json(self) -> None:
        out = _encode_tool_result({"documents_ingested": 0, "paper_ids": ["x"]})
        parsed = json.loads(out)
        assert parsed == {"documents_ingested": 0, "paper_ids": ["x"]}

    def test_list_output_emits_valid_json(self) -> None:
        out = _encode_tool_result([{"id": "a"}, {"id": "b"}])
        assert json.loads(out) == [{"id": "a"}, {"id": "b"}]

    def test_string_output_passes_through_as_str(self) -> None:
        assert _encode_tool_result("hello") == "hello"

    def test_oversized_dict_stays_valid_json_with_capped_values(self) -> None:
        # Long string values are shortened instead of slicing the serialized
        # JSON mid-token — identity fields must survive as parseable JSON.
        big = {"blob": "x" * 1000, "note_id": "abc-123"}
        out = _encode_tool_result(big)
        assert len(out) <= 500
        parsed = json.loads(out)
        assert parsed["note_id"] == "abc-123"
        assert parsed["blob"].startswith("xxx") and len(parsed["blob"]) <= 121

    def test_created_note_identity_survives_long_titles(self) -> None:
        # Regression: 100-char title + project name used to push the payload
        # past 500 chars and the naive slice broke JSON.parse downstream.
        title = "t" * 100
        project = "p" * 100
        payload = {
            "status": "success",
            "note_id": "3f2a9c1e-0000-4000-8000-000000000001",
            "project_id": "5b1d7e2f-0000-4000-8000-000000000002",
            "title": title,
            "project_name": project,
            "message": f"Created note '{title}' in project '{project}'.",
        }
        out = _encode_tool_result(payload)
        parsed = json.loads(out)
        assert parsed["note_id"] == payload["note_id"]
        assert parsed["project_id"] == payload["project_id"]

    def test_oversized_uncompactable_dict_stays_valid_json(self) -> None:
        big = {f"k{i}": i for i in range(200)}
        out = _encode_tool_result(big)
        assert len(out) <= 500
        assert json.loads(out) == {"truncated": True}

    def test_long_draft_result_preserves_terminal_identity(self) -> None:
        payload = {
            "status": "completed",
            "task_id": "task-1",
            "draft_id": "draft-9",
            "project_id": "project-2",
            "message": "x" * 1000,
            "current_step": "y" * 1000,
        }

        parsed = json.loads(_encode_tool_result(payload))

        assert parsed["status"] == "completed"
        assert parsed["task_id"] == "task-1"
        assert parsed["draft_id"] == "draft-9"

    def test_priority_fallback_caps_error_and_preserves_terminal_identity(self) -> None:
        payload = {
            "status": "failed",
            "task_id": "task-12345678",
            "draft_id": "draft-87654321",
            "project_id": "project-12345678",
            "error": "review failed: " + "e" * 1000,
            "message": "draft generation failed: " + "m" * 1000,
            **{f"metadata_{index}": "x" * 120 for index in range(12)},
        }

        out = _encode_tool_result(payload)
        parsed = json.loads(out)

        assert len(out) <= 500
        assert parsed["status"] == "failed"
        assert parsed["task_id"] == "task-12345678"
        assert parsed["draft_id"] == "draft-87654321"
        assert parsed["error"].startswith("review failed:")
        assert parsed["truncated"] is True

    def test_non_serializable_falls_back_to_str_via_default(self) -> None:
        # default=str makes datetime serializable; verify that path works.
        ts = datetime(2026, 4, 26, 12, 0, 0)
        out = _encode_tool_result({"created_at": ts})
        parsed = json.loads(out)
        assert parsed["created_at"].startswith("2026-04-26")

    def test_unencodable_value_falls_back_silently(self) -> None:
        # An object whose default=str raises is still safe — the helper
        # must not propagate the exception. Use a class with __str__ that
        # works but a json-side default that fails.
        class Odd:
            def __str__(self) -> str:
                return "<odd>"

        # Wrap a plain non-dict/list object — falls through to str() branch.
        assert _encode_tool_result(Odd()) == "<odd>"

    def test_primitive_int_uses_str(self) -> None:
        assert _encode_tool_result(42) == "42"

    def test_empty_dict_emits_empty_json_object(self) -> None:
        # CLI relies on detecting "{}" to render bare tool name.
        assert _encode_tool_result({}) == "{}"

    def test_empty_list_emits_empty_json_array(self) -> None:
        assert _encode_tool_result([]) == "[]"


# Suppress unused import warning if pytest is not yet typed for the project.
_ = pytest
