"""Phase 3 regression: rag_node skips retrieval for conversational queries.

Trace 019e168a showed "hi" in an active project (ML in Health Care)
triggering KB retrieval that returned 5 cross-project chunks (Copilot
productivity PDFs) and bloated input to 7152 tokens. Conversational
queries must skip retrieval even when a project context is present.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from src.services.agent.graph import rag_node


def _state(query: str, project_id: str | None = None) -> dict:
    page_context = {}
    if project_id:
        page_context = {"type": "project", "project_id": project_id}
    return {
        "messages": [HumanMessage(content=query)],
        "page_context": page_context,
        "current_project_id": project_id,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_for_hi_with_active_project():
    user = MagicMock()
    user.id = "user-1"
    project_id = "5ed25258-5ad2-4b06-9678-4a4abe5ecac1"

    with (
        patch(
            "src.services.agent._nodes_rag._try_primary_do_kb_read",
            new=AsyncMock(side_effect=AssertionError("retrieval should be skipped")),
        ),
        patch(
            "src.services.agent._nodes_rag._hybrid_search_fallback_with_status",
            new=AsyncMock(side_effect=AssertionError("fallback should be skipped")),
        ),
    ):
        result = await rag_node(
            _state("hi", project_id=project_id),
            config={"configurable": {"user_id": str(user.id)}},
        )

    assert result["retrieved_contexts"] == []
    assert result.get("current_project_id") == project_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_for_thanks_no_project():
    user = MagicMock()
    user.id = "user-1"

    with patch(
        "src.services.agent._nodes_rag._try_primary_do_kb_read",
        new=AsyncMock(side_effect=AssertionError("retrieval should be skipped")),
    ):
        result = await rag_node(
            _state("thanks!"),
            config={"configurable": {"user_id": str(user.id)}},
        )

    assert result["retrieved_contexts"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_substantive_query_with_project_forwards_project_id():
    user = MagicMock()
    user.id = "user-1"
    project_id = "5ed25258-5ad2-4b06-9678-4a4abe5ecac1"

    with patch(
        "src.services.agent._nodes_rag._try_primary_do_kb_read",
        new=AsyncMock(
            return_value=[
                {"document_id": "d", "title": "t", "content": "c", "score": 1.0}
            ]
        ),
    ) as mock_primary:
        result = await rag_node(
            _state(
                "Find recent transformer architecture papers please",
                project_id=project_id,
            ),
            config={"configurable": {"user_id": str(user.id)}},
        )

    mock_primary.assert_awaited_once()
    call_kwargs = mock_primary.await_args.kwargs
    assert call_kwargs.get("project_id") == project_id
    assert result["retrieved_contexts"]
