"""rag_node records why retrieval is empty, and the prompt says so.

Before this, a turn with no active project (retrieval never ran) and a turn
whose search raised both rendered NO_RETRIEVAL_GUIDANCE ("state plainly that
nothing relevant was found in their corpus"), so the agent told users their
indexed documents did not exist.

Mutation check: return ``"empty"`` instead of ``"skipped"`` from the
no-project branch of ``rag_node``, or ``"empty"`` instead of ``"error"`` from
the except branch of ``_hybrid_search_fallback_with_status``, and this file
fails.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from src.services.agent import _nodes_rag
from src.services.agent.retrieval_provenance import (
    NO_RETRIEVAL_GUIDANCE,
    RETRIEVAL_FAILED_GUIDANCE,
    RETRIEVAL_SKIPPED_GUIDANCE,
    no_retrieval_guidance,
)
from src.services.agent.runtime_snapshot import turn_reset_fields

pytestmark = pytest.mark.unit

PROJECT_ID = "5ed25258-5ad2-4b06-9678-4a4abe5ecac1"
CONFIG: dict[str, Any] = {
    "configurable": {"user_id": "user-1", "organization_id": "org-1"}
}


def _state(query: str, project_id: str | None = None) -> dict[str, Any]:
    page_context: dict[str, Any] = {}
    if project_id:
        page_context = {"type": "project", "project_id": project_id}
    return {
        "messages": [HumanMessage(content=query)],
        "page_context": page_context,
        "current_project_id": project_id,
    }


@pytest.mark.asyncio
async def test_no_project_turn_is_skipped_not_empty() -> None:
    result = await _nodes_rag.rag_node(
        _state("Summarize the attention mechanism papers"), CONFIG
    )

    assert result["retrieved_contexts"] == []
    assert result["retrieval_status"] == "skipped"


@pytest.mark.asyncio
async def test_failed_fallback_search_is_reported_as_error() -> None:
    raising = MagicMock(side_effect=RuntimeError("db down"))
    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        raising,
    ):
        contexts, status = await _nodes_rag._hybrid_search_fallback_with_status(
            "attention", "11111111-1111-4111-8111-111111111111", "org-1"
        )

    assert contexts == []
    assert status == "error"


@pytest.mark.asyncio
async def test_rag_node_carries_fallback_error_into_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _nodes_rag, "_try_primary_do_kb_read", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        _nodes_rag,
        "_hybrid_search_fallback_with_status",
        AsyncMock(return_value=([], "error")),
    )

    result = await _nodes_rag.rag_node(
        _state("Find papers on attention mechanisms", PROJECT_ID), CONFIG
    )

    assert result["retrieved_contexts"] == []
    assert result["retrieval_status"] == "error"


@pytest.mark.asyncio
async def test_rag_node_marks_found_contexts_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts = [{"document_id": "d1", "title": "t", "content": "c", "score": 0.9}]
    monkeypatch.setattr(
        _nodes_rag, "_try_primary_do_kb_read", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        _nodes_rag,
        "_hybrid_search_fallback_with_status",
        AsyncMock(return_value=(contexts, "ok")),
    )

    result = await _nodes_rag.rag_node(
        _state("Find papers on attention mechanisms", PROJECT_ID), CONFIG
    )

    assert result["retrieved_contexts"] == contexts
    assert result["retrieval_status"] == "ok"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("skipped", RETRIEVAL_SKIPPED_GUIDANCE),
        ("error", RETRIEVAL_FAILED_GUIDANCE),
        ("empty", NO_RETRIEVAL_GUIDANCE),
        (None, NO_RETRIEVAL_GUIDANCE),
        ("unexpected", NO_RETRIEVAL_GUIDANCE),
    ],
)
def test_guidance_matches_status(status: Any, expected: str) -> None:
    assert no_retrieval_guidance(status) == expected


def test_skipped_and_failed_guidance_never_claim_an_empty_corpus() -> None:
    for text in (RETRIEVAL_SKIPPED_GUIDANCE, RETRIEVAL_FAILED_GUIDANCE):
        assert "nothing relevant was found" not in text
        assert "do_kb_retrieve" in text
        assert "Do NOT invent citations" in text


def test_dynamic_context_renders_guidance_for_the_status() -> None:
    from src.services.agent.runtime_context import render_dynamic_context
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    base: dict[str, Any] = {
        "intent": "general",
        "page_context": {},
        "messages": [],
        "retrieved_contexts": [],
        "attachment_status": [],
        "user_memories": [],
        "project_memories": [],
        "runtime_snapshot_id": "snapshot-1",
        "runtime_tool_names": list(TOOL_REGISTRY.available_descriptor_names()),
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "runtime_projection_unavailable": False,
        "project_skill_catalog": [],
        "loaded_skill_versions": [],
        "plan": [],
        "capability_limitation": {},
    }

    skipped = render_dynamic_context(
        {**base, "retrieval_status": "skipped"}, {}, resolved_model="m"
    )
    empty = render_dynamic_context(
        {**base, "retrieval_status": "empty"}, {}, resolved_model="m"
    )

    assert RETRIEVAL_SKIPPED_GUIDANCE in skipped
    assert NO_RETRIEVAL_GUIDANCE not in skipped
    assert NO_RETRIEVAL_GUIDANCE in empty


def test_status_is_reset_every_turn() -> None:
    assert turn_reset_fields()["retrieval_status"] is None
