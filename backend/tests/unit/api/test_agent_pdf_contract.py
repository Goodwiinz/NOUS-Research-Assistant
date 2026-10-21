"""PDF context scope and public stream provenance contracts."""

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from src.api.agent.streaming import (
    _bound_rag_context_payload,
    _changed_retrieved_context_snapshot,
    _rag_context_event_payload,
)
from src.services.agent.runtime_snapshot import turn_reset_fields
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.agent.tool_registry import (
    AgentIntent,
    AgentSubgraph,
    ToolDescriptor,
    ToolRegistry,
    runtime_tool_descriptors,
)


def _request(**fields: Any) -> AgentExecuteRequest:
    return AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="Explain this page")], **fields
    )


@pytest.mark.parametrize("selection", [[0, 0], [2, 8], [150, 249]])
def test_pdf_range_preserves_zero_based_endpoints(selection: list[int]) -> None:
    request = _request(attachment_ids=[uuid4()], selection_page_range=selection)
    assert request.selection_page_range == tuple(selection)
    assert request.model_dump(mode="json")["selection_page_range"] == selection


@pytest.mark.parametrize(
    "selection", [[-1, 2], [3, 1], [0], [0, 1, 2], [True, 2], [0.5, 2], ["0", 2]]
)
def test_invalid_pdf_ranges_are_rejected(selection: list[Any]) -> None:
    with pytest.raises(ValidationError):
        _request(selection_page_range=selection)


@pytest.mark.parametrize(
    "selection,message",
    [([3, 1], "ascending order"), ([0, 100], "at most 100 pages")],
)
def test_pdf_range_type_rejects_reversed_or_oversized_spans(
    selection: list[int], message: str
) -> None:
    from pydantic import TypeAdapter

    from src.services.agent.schemas import PageFilterResponse, PdfPageRange

    # The constraint lives in the shared type, so every user of it agrees.
    with pytest.raises(ValidationError, match=message):
        TypeAdapter(PdfPageRange).validate_python(tuple(selection))
    with pytest.raises(ValidationError, match=message):
        PageFilterResponse.model_validate(
            {
                "document_id": str(uuid4()),
                "accepted_pages": [],
                "total_pages": 1,
                "short_circuited": True,
                "selection_page_range": selection,
            }
        )
    with pytest.raises(ValidationError, match=message):
        _request(attachment_ids=[uuid4()], selection_page_range=selection)


@pytest.mark.parametrize("count", [0, 2])
def test_pdf_range_rejects_ambiguous_explicit_document_scope(count: int) -> None:
    with pytest.raises(ValidationError, match="one attached document"):
        _request(
            attachment_ids=[uuid4() for _ in range(count)],
            selection_page_range=[0, 1],
        )


def test_pdf_range_can_resolve_implicit_thread_attachment() -> None:
    # The loader subsequently verifies that the authorized thread has exactly
    # one live attachment. The wire model must not reject this follow-up.
    assert _request(selection_page_range=[0, 1]).attachment_ids is None


def test_pdf_range_is_not_silently_ignored_by_external_provider() -> None:
    with pytest.raises(ValidationError, match="NOUS execution"):
        _request(
            execution_provider="codex",
            device_id=uuid4(),
            workspace_id=uuid4(),
            thread_id=str(uuid4()),
            selection_page_range=[0, 1],
        )


def test_document_only_is_not_silently_ignored_by_external_provider() -> None:
    with pytest.raises(ValidationError, match="NOUS execution"):
        _request(
            execution_provider="codex",
            device_id=uuid4(),
            workspace_id=uuid4(),
            thread_id=str(uuid4()),
            document_only=True,
        )


def _filter_event() -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "data": {
            "output": {
                "retrieved_contexts": [{"document_id": str(uuid4()), "page_number": 3}],
                "page_filter": {
                    "document_id": str(uuid4()),
                    "accepted_pages": [3, 5],
                    "total_pages": 12,
                    "short_circuited": False,
                    "model": "rerank-test",
                    "timing_ms": {"average": 10, "min": 10, "max": 10, "count": 1},
                    "priorities": [{"page": 3, "score": 0.9}],
                },
            }
        },
    }


def test_filter_projection_drops_provider_details_and_preserves_pages() -> None:
    event = _filter_event()
    raw = event["data"]["output"]["page_filter"]
    raw["api_key"] = "must-not-leave-service"
    raw["timing_ms"]["provider_url"] = "https://private.invalid"
    contexts = event["data"]["output"]["retrieved_contexts"]
    payload = _bound_rag_context_payload(_rag_context_event_payload(event, contexts))
    assert payload["page_filter"]["accepted_pages"] == [3, 5]
    assert "api_key" not in payload["page_filter"]
    assert "provider_url" not in payload["page_filter"]["timing_ms"]
    assert payload["contexts"][0]["page_number"] == 3


def test_filter_only_change_emits_new_snapshot() -> None:
    event = _filter_event()
    contexts, fingerprint = _changed_retrieved_context_snapshot(event, None)
    event["data"]["output"]["page_filter"]["accepted_pages"] = [3]
    changed, updated = _changed_retrieved_context_snapshot(event, fingerprint)
    assert changed == contexts
    assert updated != fingerprint


def test_invalid_filter_metadata_is_not_exposed() -> None:
    event = _filter_event()
    event["data"]["output"]["page_filter"]["accepted_pages"] = [-1]
    assert "page_filter" not in _rag_context_event_payload(event, [])


def test_new_turn_clears_page_range_filter_and_document_only_mode() -> None:
    checkpoint = {
        "selection_page_range": [0, 2],
        "page_filter_model": "cohere",
        "branch_document_id": str(uuid4()),
        "page_filter": {"accepted_pages": [1]},
        "document_only": True,
    }
    checkpoint.update(turn_reset_fields())
    assert checkpoint["selection_page_range"] is None
    assert checkpoint["page_filter_model"] is None
    assert checkpoint["branch_document_id"] is None
    assert checkpoint["page_filter"] is None
    assert checkpoint["document_only"] is False


@pytest.mark.parametrize("scope", [[], [uuid4(), uuid4()]])
def test_explicit_page_filter_rejects_ambiguous_unbound_scope(scope: list) -> None:
    with pytest.raises(ValidationError, match="one attached document"):
        _request(attachment_ids=scope, page_filter_model="cohere")


def test_bound_paper_selection_allows_distinct_supplementary_files() -> None:
    paper, extra = uuid4(), uuid4()
    request = _request(
        branch_document_id=paper,
        attachment_ids=[paper, extra],
        selection_page_range=[1, 1],
        page_filter_model="cohere",
    )
    assert request.attachment_ids == [paper, extra]
    for attachments in ([extra, paper], [paper, paper], []):
        with pytest.raises(ValidationError, match="bound PDF attachment"):
            _request(branch_document_id=paper, attachment_ids=attachments)


def test_explicit_filter_never_silently_uses_external_execution() -> None:
    with pytest.raises(ValidationError, match="NOUS execution"):
        _request(
            execution_provider="codex",
            device_id=uuid4(),
            workspace_id=uuid4(),
            thread_id=str(uuid4()),
            page_filter_model="cohere",
        )


@tool
def outside_pdf(query: str) -> str:
    """A capability that would leave the selected PDF context."""
    return query


@pytest.mark.parametrize("branch", ["main", "research", "writing", "data"])
def test_document_only_removes_tools_from_every_execution_branch(branch: str) -> None:
    descriptor = ToolDescriptor(
        name=outside_pdf.name,
        tool=outside_pdf,
        intents=frozenset(AgentIntent),
        subgraphs=frozenset(AgentSubgraph),
        subgraph_positions=tuple((item, 0) for item in AgentSubgraph),
        policy_tags=frozenset(),
    )
    registry = ToolRegistry([descriptor])
    unrestricted = {"intent": "research"}
    assert runtime_tool_descriptors(branch, unrestricted, registry) == (descriptor,)
    assert (
        runtime_tool_descriptors(
            branch, {**unrestricted, "document_only": True}, registry
        )
        == ()
    )
