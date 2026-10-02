"""Capability terminal responses only claim work backed by completion evidence."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_terminal_message_excludes_pending_and_unknown_mutation_results() -> None:
    from src.services.agent.capability_terminal import make_capability_terminal_message

    # Real execution shape (R8-A9): tool_node records status "completed" or
    # "failed" (_nodes_tools.py), never "success".

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
                    "status": "completed",
                    "result": {"status": "pending", "task_id": "draft-task"},
                },
                {
                    "tool_name": "add_document_to_project",
                    "status": "completed",
                    "result": {"status": "unknown", "project_id": "project-1"},
                },
                {
                    "tool_name": "search_documents",
                    "status": "completed",
                    "result": {"status": "completed", "documents": []},
                },
                {
                    "tool_name": "search_arxiv",
                    "status": "failed",
                    "result": {"status": "completed", "papers": []},
                },
            ],
        }
    )

    assert "Already completed operations: search_documents." in message.content
    assert "create_draft" not in message.content
    assert "add_document_to_project" not in message.content
    assert "draft-task" not in message.content
    assert "search_arxiv" not in message.content
