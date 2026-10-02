"""A tool's own error classification must reach the model.

``_nodes_tools`` does not forward what a tool returns. It rebuilds the
ToolMessage::

    tool_error = classify_error_from_payload(tool_name, result)
    result_content = tool_error.to_tool_message_content()

and ``classify_error_from_payload`` read only ``payload["error"]``. So every
hand-written hint in ``tools_impl.py`` was discarded, and — worse — re-derived
as ``fatal``, which tells the agent not to recover. Measured before the fix::

    tool returns : error_type="recoverable", suggestion="list_project_documents"
    model receives: {"error": "…", "error_type": "fatal"}

Both known casualties are covered below: ``summarize_document``'s project-id
branch and its "not in your library" arXiv branch. Both were written
specifically to steer recovery, and neither survived the trip.

Assertions run against ``to_tool_message_content()`` — the string the model
actually sees. Asserting on the tool's return value is what let this hide: the
returned dict was always correct; it just never got delivered.
"""

from __future__ import annotations

import json
from typing import Any, Dict, cast

import pytest

pytestmark = pytest.mark.unit


def _delivered(tool: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """What the model receives for this tool payload."""
    from src.services.agent.error_recovery import classify_error_from_payload

    return cast(
        Dict[str, Any],
        json.loads(
            classify_error_from_payload(tool, payload).to_tool_message_content()
        ),
    )


class TestDeclaredCategorySurvives:
    def test_project_id_branch_arrives_recoverable(self) -> None:
        delivered = _delivered(
            "summarize_document",
            {
                "error": "'abc' is a project id, not a document id. Call "
                'list_project_documents(project_id="abc") to get the ids.',
                "error_type": "recoverable",
                "suggestion": "list_project_documents",
            },
        )

        assert delivered["error_type"] == "recoverable", (
            "arriving as 'fatal' tells the agent not to recover, discarding a "
            "request the tool told it exactly how to satisfy"
        )
        assert delivered["suggestion"] == "list_project_documents"

    def test_arxiv_not_ingested_branch_arrives_recoverable(self) -> None:
        delivered = _delivered(
            "summarize_document",
            {
                "error": "Document with arXiv ID '2401.12345' is not in your library.",
                "error_type": "recoverable",
                "suggestion": "ingest_arxiv_papers",
            },
        )

        assert delivered["error_type"] == "recoverable"
        assert delivered["suggestion"] == "ingest_arxiv_papers"

    def test_transient_is_not_declarable(self) -> None:
        """It would let a tool zero its own error-ceiling contribution.

        ``_nodes_tools`` sets ``error_increment = 0`` for transient, and
        reflection skips its response-quality gate for transient failures. A
        returned payload also means the call completed, so transience is not
        knowable here — that lives on the raised-exception path.
        """
        delivered = _delivered(
            "some_tool", {"error": "upstream hiccup", "error_type": "transient"}
        )

        assert delivered["error_type"] == "fatal"


class TestDeclarationCannotWeakenSafety:
    """A tool must not be able to make an auth failure look retryable."""

    def test_credentials_stay_user_fixable(self) -> None:
        delivered = _delivered(
            "x", {"error": "invalid api key", "error_type": "recoverable"}
        )

        assert delivered["error_type"] == "user_fixable"

    def test_permission_stays_user_fixable(self) -> None:
        delivered = _delivered(
            "x", {"error": "permission denied", "error_type": "transient"}
        )

        assert delivered["error_type"] == "user_fixable"

    def test_curated_hints_still_win(self) -> None:
        """TOOL_ERROR_HINTS is the deliberate central override."""
        delivered = _delivered(
            "create_project_note",
            {"error": "project_id is required", "error_type": "fatal"},
        )

        assert delivered["error_type"] == "recoverable"
        assert "list_projects" in delivered["suggestion"]


class TestGarbageIsIgnored:
    @pytest.mark.parametrize(
        "declared", ["Recoverable", "retryable", "transient", "", None, 42, {"a": 1}]
    )
    def test_unknown_values_fall_through_to_the_heuristics(self, declared) -> None:  # type: ignore[no-untyped-def]
        """A typo must not become a category.

        The message keyword-classifies to *recoverable*, not to the ``fatal``
        default: asserting the default cannot tell "declaration ignored" from
        "declaration honoured as fatal", and passes vacuously for "" / None
        against a naive ``declared or heuristic``.
        """
        delivered = _delivered(
            "x", {"error": "widget not found", "error_type": declared}
        )

        assert delivered["error_type"] == "recoverable"
        assert delivered["suggestion"] == "Check the input and try again."

    def test_a_non_string_suggestion_is_dropped_not_rendered(self) -> None:
        delivered = _delivered(
            "x", {"error": "boom", "error_type": "recoverable", "suggestion": 7}
        )

        assert delivered["error_type"] == "recoverable"
        assert "suggestion" not in delivered


class TestNoRegression:
    def test_keyword_classification_still_applies_without_a_declaration(self) -> None:
        assert _delivered("x", {"error": "rate limited (429)"})["error_type"] == (
            "transient"
        )
        assert _delivered("x", {"error": "not found"})["error_type"] == "recoverable"
        assert _delivered("x", {"error": "kernel panic"})["error_type"] == "fatal"


class TestMissingProjectErrorIsDelivered:
    """What the write-tool impls return when no project_id resolves.

    The old ``tools._missing_project_error`` payload lived in the @tool
    wrapper bodies, which production never ran (agent audit round 8). The
    model sees the impls' "project_id is required" instead.
    """

    @pytest.mark.parametrize(
        "tool",
        [
            "add_document_to_project",
            "create_project_note",
            "list_project_documents",
            "get_current_draft",
            "create_draft",
            "revise_draft",
        ],
    )
    def test_missing_project_error_is_recoverable(self, tool: str) -> None:
        delivered = _delivered(tool, {"error": "project_id is required"})

        assert delivered["error_type"] == "recoverable"
