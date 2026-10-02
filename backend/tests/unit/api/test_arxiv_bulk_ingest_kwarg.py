"""R2-M22: bulk_ingest_with_kg called ArXivIngestionService.ingest_papers with
an extract_entities kwarg the method doesn't accept (no **kwargs either) ->
TypeError -> 500 on every request. Entity extraction already happens via the
kg_integration.process_paper_kg_integration call right after; the fix drops
the bogus kwarg.

Follow-up (Codex review on PR #1452): once the TypeError no longer masked it,
ingest_papers() only builds transient in-memory documents — nothing wrote
them to the DB, so a "successful" bulk-ingest response silently persisted
nothing. Fixed by wiring persist_arxiv_documents(), same as the other live
caller (src/api/arxiv/core.py). A user with no organization must be rejected
(403), never passed through as an unscoped write.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.arxiv import arxiv_knowledge_graph as route_module
from src.api.arxiv.arxiv_knowledge_graph import (
    BulkIngestionRequest,
    bulk_ingest_with_kg,
)
from src.services.arxiv.arxiv_service import ArXivIngestionService

pytestmark = pytest.mark.unit


def test_ingest_papers_signature_has_no_extract_entities_kwarg() -> None:
    """Guards the underlying contract: if this ever regains extract_entities,
    the call site is free to pass it again, but not silently drop it."""
    sig = inspect.signature(ArXivIngestionService.ingest_papers)
    assert "extract_entities" not in sig.parameters
    assert not any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


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
async def test_bulk_ingest_route_does_not_raise_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = _paper()

    arxiv_service = MagicMock()
    arxiv_service.search_papers = AsyncMock(return_value=[paper])
    arxiv_service.ingest_papers = AsyncMock(return_value=[MagicMock()])

    kg_integration = MagicMock()
    kg_integration.process_paper_kg_integration = AsyncMock(return_value={})

    persisted = MagicMock()
    persisted.document_ids = ["doc-1"]
    persist_mock = AsyncMock(return_value=persisted)
    monkeypatch.setattr(route_module, "persist_arxiv_documents", persist_mock)

    current_user = {"organization_id": "org-1", "id": "user-1"}

    result = await bulk_ingest_with_kg.__wrapped__(
        request=BulkIngestionRequest(query="cat:cs.AI", create_kg_entries=True),
        current_user=current_user,
        arxiv_service=arxiv_service,
        kg_integration=kg_integration,
    )

    assert result["status"] == "success"
    arxiv_service.ingest_papers.assert_awaited_once()
    _, kwargs = arxiv_service.ingest_papers.call_args
    assert "extract_entities" not in kwargs

    # Documents must actually be persisted (not just built in memory) — the
    # count reported to the caller reflects what was durably written.
    persist_mock.assert_awaited_once()
    _, persist_kwargs = persist_mock.call_args
    assert persist_kwargs["organization_id"] == "org-1"
    assert persist_kwargs["user_id"] == "user-1"
    assert result["papers_ingested"] == 1


@pytest.mark.asyncio
async def test_bulk_ingest_rejects_user_without_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user with no organization must be rejected (403), never silently
    persisted as an unscoped/cross-tenant write."""
    arxiv_service = MagicMock()
    arxiv_service.search_papers = AsyncMock(return_value=[_paper()])
    arxiv_service.ingest_papers = AsyncMock(return_value=[MagicMock()])

    kg_integration = MagicMock()
    persist_mock = AsyncMock()
    monkeypatch.setattr(route_module, "persist_arxiv_documents", persist_mock)

    current_user = {"organization_id": None, "id": "user-1"}

    with pytest.raises(HTTPException) as exc_info:
        await bulk_ingest_with_kg.__wrapped__(
            request=BulkIngestionRequest(query="cat:cs.AI"),
            current_user=current_user,
            arxiv_service=arxiv_service,
            kg_integration=kg_integration,
        )

    assert exc_info.value.status_code == 403
    persist_mock.assert_not_awaited()
