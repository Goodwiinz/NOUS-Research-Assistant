"""PDF-only turns exclude recalled memories while ordinary recall stays intact."""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from src.services.agent._nodes_memory import memory_retrieval_node
from src.services.agent.runtime_context import render_dynamic_context
from src.services.agent.state import AgentState


@pytest.mark.parametrize("document_only", [True, False, None])
@pytest.mark.parametrize("branch", ["main", "research", "writing", "data"])
def test_rendered_context_excludes_checkpoint_memories_only_for_pdf_turns(
    branch: str, document_only: bool | None
) -> None:
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="Explain the attached page")],
        "intent": "general",
        "user_memories": [{"value": {"query": "user-memory-outside-pdf"}}],
        "project_memories": [{"query": "project-memory-outside-pdf"}],
        "retrieved_contexts": [
            {
                "document_id": "selected-document",
                "title": "Selected paper",
                "content": "selected-pdf-page-evidence",
                "page_number": 2,
            }
        ],
    }
    if document_only is not None:
        state["document_only"] = document_only

    rendered = render_dynamic_context(
        state, {}, resolved_model="test-model", branch=branch
    )

    assert "selected-pdf-page-evidence" in rendered
    assert ("user-memory-outside-pdf" in rendered) is not bool(document_only)
    assert ("project-memory-outside-pdf" in rendered) is not bool(document_only)
    # Rendering must not destructively remove history or checkpoint memory.
    assert state["messages"][0].content == "Explain the attached page"
    assert state["user_memories"]
    assert state["project_memories"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document_only", [True, False, None])
async def test_document_only_skips_memory_store_and_search(
    monkeypatch: pytest.MonkeyPatch, document_only: bool | None
) -> None:
    import src.services.agent.memory as memory

    store = object()
    records = [{"key": "memory-1", "score": 0.9, "query": "outside-pdf"}]
    get_store = AsyncMock(return_value=store)
    search = AsyncMock(return_value=records)
    monkeypatch.setattr(memory, "get_memory_store", get_store)
    monkeypatch.setattr(memory, "search_memories", search)
    state: dict[str, Any] = {
        "messages": [HumanMessage(content="Explain the transformer architecture")],
        "user_memories": records,
    }
    if document_only is not None:
        state["document_only"] = document_only

    result = await memory_retrieval_node(
        cast(AgentState, state),
        {"configurable": {"user_id": "user-1", "organization_id": "org-1"}},
    )

    if document_only:
        assert result == {"user_memories": []}
        get_store.assert_not_awaited()
        search.assert_not_awaited()
    else:
        assert result == {"user_memories": records}
        get_store.assert_awaited_once_with()
        search.assert_awaited_once_with(
            store,
            "user-1",
            "Explain the transformer architecture",
            limit=5,
            organization_id="org-1",
        )
