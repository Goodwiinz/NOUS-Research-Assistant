"""Capability terminal responses only claim work backed by completion evidence."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_terminal_message_excludes_pending_and_unknown_mutation_results() -> None:
    from src.services.agent.capability_terminal import make_capability_terminal_message

    message = make_capability_terminal_message(
        {
            "capability_limitation": {
                "branch": "writing",
                "kind": "execution",
                "unavailable_tools": [],
            },
            "tool_executions": [
                {
                    "tool_name": "create_draft",
                    "status": "success",
                    "result": {"status": "pending", "task_id": "draft-task"},
                },
                {
                    "tool_name": "add_document_to_project",
                    "status": "success",
                    "result": {"status": "unknown", "project_id": "project-1"},
                },
                {
                    "tool_name": "search_documents",
                    "status": "success",
                    "result": {"status": "completed", "documents": []},
                },
            ],
        }
    )

    assert "Already completed operations: search_documents." in message.content
    assert "create_draft" not in message.content
    assert "add_document_to_project" not in message.content
    assert "draft-task" not in message.content
