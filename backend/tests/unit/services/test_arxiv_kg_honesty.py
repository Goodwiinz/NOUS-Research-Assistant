"""R5-H1 + R5-H4: the arXiv KG path must not report work it never did.

Three failure modes are locked down here:

1. `process_paper_kg_integration` used to run the (paid) Azure entity/relation
   extraction *before* checking whether a knowledge graph existed to write to.
   With `kg_service=None` — the permanent state, see R5-H1 — that was one LLM
   call per paper, up to 500 per /bulk-ingest request, for zero writes.
2. `/bulk-ingest` incremented `kg_entries_created` once per paper attempted,
   regardless of whether anything was written (or even if the call returned
   None after swallowing its own exception).
3. `__aenter__` swallowed a Neo4j init failure and set `kg_service=None`, so
   every correctly-entered caller silently degraded to "success, zero writes".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.arxiv import arxiv_knowledge_graph as route_module
from src.api.arxiv.arxiv_knowledge_graph import (
    BulkIngestionRequest,
    bulk_ingest_with_kg,
)
from src.services.arxiv import arxiv_kg_integration as kg_module
from src.services.arxiv.arxiv_kg_integration import ArXivKnowledgeGraphIntegration

pytestmark = pytest.mark.unit


def _paper() -> dict:
    return {
        "id": "2401.00001v1",
        "title": "A Title",
        "abstract": "Abstract text.",
        "authors": ["Ada"],
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
    }


@pytest.mark.asyncio
async def test_no_llm_call_when_kg_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No knowledge graph to write to => no paid extraction."""
    azure = MagicMock()
    azure.is_chat_available = MagicMock(return_value=True)
    azure.chat_completion = AsyncMock(
        side_effect=AssertionError("LLM must not be called without a KG")
    )
    monkeypatch.setattr(kg_module, "azure_openai_service", azure)

    integration = ArXivKnowledgeGraphIntegration()
    assert integration.kg_service is None  # what the bare constructor gives you

    result = await integration.process_paper_kg_integration(_paper())

    azure.chat_completion.assert_not_awaited()
    assert result["kg_updated"] is False
    assert result["entities_created"] == 0
    assert result["relationships_created"] == 0


@pytest.mark.asyncio
async def test_entities_created_counts_only_actual_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three entities extracted, one write blows up => count is two, not three."""
    integration = ArXivKnowledgeGraphIntegration()

    kg_service = MagicMock()
    # 4th slot: process_paper_kg_integration now also upserts the paper-title
    # node before relationship creation (audit R5-M3).
    kg_service.create_entity = MagicMock(
        side_effect=[
            SimpleNamespace(id="e1"),
            RuntimeError("neo4j write failed"),
            SimpleNamespace(id="e3"),
            SimpleNamespace(id="paper-title"),
        ]
    )
    integration.kg_service = kg_service  # type: ignore[assignment]

    entities = [
        {"text": f"Concept {i}", "type": "concept", "confidence": 0.9, "source": "test"}
        for i in range(3)
    ]
    monkeypatch.setattr(
        integration, "_extract_entities_from_paper", AsyncMock(return_value=entities)
    )
    monkeypatch.setattr(
        integration, "_extract_relationships_from_paper", AsyncMock(return_value=[])
    )

    result = await integration.process_paper_kg_integration(_paper())

    assert len(result["entities"]) == 3
    assert result["entities_created"] == 2
    assert result["relationships_created"] == 0
    assert result["kg_updated"] is True


@pytest.mark.asyncio
async def test_aenter_raises_when_graph_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Neo4j failure is loud; it must not degrade into kg_service=None."""
    import src.services.knowledge_graph as kg_pkg

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("neo4j down")

    monkeypatch.setattr(kg_pkg, "KnowledgeGraphService", _Boom)

    integration = ArXivKnowledgeGraphIntegration()
    arxiv_service = MagicMock()
    arxiv_service.__aenter__ = AsyncMock(return_value=arxiv_service)
    arxiv_service.__aexit__ = AsyncMock(return_value=None)
    integration.arxiv_service = arxiv_service

    with pytest.raises(RuntimeError, match="Knowledge graph service unavailable"):
        await integration.__aenter__()

    # the arXiv HTTP session opened moments earlier must not leak
    arxiv_service.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_ingest_reports_kg_entries_actually_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arxiv_service = MagicMock()
    arxiv_service.search_papers = AsyncMock(return_value=[_paper(), _paper()])
    arxiv_service.ingest_papers = AsyncMock(return_value=[MagicMock()])

    persisted = MagicMock()
    persisted.document_ids = ["doc-1"]
    monkeypatch.setattr(
        route_module, "persist_arxiv_documents", AsyncMock(return_value=persisted)
    )

    kg_integration = MagicMock()
    kg_integration.process_paper_kg_integration = AsyncMock(
        side_effect=[
            {"entities_created": 4, "relationships_created": 1},
            # second paper: swallowed failure, returns None — must count zero
            None,
        ]
    )

    result = await bulk_ingest_with_kg.__wrapped__(
        request=BulkIngestionRequest(query="cat:cs.AI", create_kg_entries=True),
        current_user={"organization_id": "org-1", "id": "user-1"},
        arxiv_service=arxiv_service,
        kg_integration=kg_integration,
    )

    assert result["kg_status"] == "ok"
    assert result["kg_entries_created"] == 5


@pytest.mark.asyncio
async def test_bulk_ingest_still_ingests_when_kg_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents are the working half — they still land, and the KG side is
    reported as unavailable instead of being counted as success."""
    arxiv_service = MagicMock()
    arxiv_service.search_papers = AsyncMock(return_value=[_paper()])
    arxiv_service.ingest_papers = AsyncMock(return_value=[MagicMock()])

    persisted = MagicMock()
    persisted.document_ids = ["doc-1"]
    monkeypatch.setattr(
        route_module, "persist_arxiv_documents", AsyncMock(return_value=persisted)
    )

    result = await bulk_ingest_with_kg.__wrapped__(
        request=BulkIngestionRequest(query="cat:cs.AI", create_kg_entries=True),
        current_user={"organization_id": "org-1", "id": "user-1"},
        kg_integration=None,
        arxiv_service=arxiv_service,
    )

    assert result["papers_ingested"] == 1
    assert result["kg_status"] == "unavailable"
    assert result["kg_entries_created"] == 0


@pytest.mark.asyncio
async def test_get_kg_integration_yields_none_when_graph_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dependency must enter the context manager (bare construction leaves
    kg_service=None forever) and degrade to None when entering fails."""
    monkeypatch.setattr(
        route_module,
        "ArXivKnowledgeGraphIntegration",
        MagicMock(
            return_value=SimpleNamespace(
                __aenter__=AsyncMock(side_effect=RuntimeError("neo4j down")),
                __aexit__=AsyncMock(return_value=None),
            )
        ),
    )

    agen = route_module.get_kg_integration()
    assert await agen.__anext__() is None
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
