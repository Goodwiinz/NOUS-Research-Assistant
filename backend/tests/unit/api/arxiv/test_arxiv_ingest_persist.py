"""The mounted REST ingest must use the shared durable persistence path."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.arxiv import core
from src.services.arxiv.persistence import ArxivPersistenceResult

pytestmark = pytest.mark.unit


async def test_rest_ingest_delegates_with_authenticated_tenant(monkeypatch):
    source_document = MagicMock()
    service = MagicMock()
    service.search_papers = AsyncMock(return_value=[{"id": "2401.00001v1"}])
    service.ingest_papers = AsyncMock(return_value=[source_document])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=service)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(core, "ArXivIngestionService", MagicMock(return_value=context))

    persist = AsyncMock(
        return_value=ArxivPersistenceResult(
            document_ids=["document-1"], reused_document_ids=set()
        )
    )
    monkeypatch.setattr(core, "persist_arxiv_documents", persist)

    await core._process_arxiv_ingestion(
        paper_ids=["2401.00001v1"],
        user_id="user-1",
        organization_id="org-A",
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
    )

    persist.assert_awaited_once_with(
        [source_document], user_id="user-1", organization_id="org-A"
    )


async def test_rest_ingest_does_not_persist_when_arxiv_returns_no_papers(monkeypatch):
    service = MagicMock()
    service.search_papers = AsyncMock(return_value=[])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=service)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(core, "ArXivIngestionService", MagicMock(return_value=context))
    persist = AsyncMock()
    monkeypatch.setattr(core, "persist_arxiv_documents", persist)

    await core._process_arxiv_ingestion(
        paper_ids=["2401.99999"],
        user_id="user-1",
        organization_id="org-A",
        download_pdfs=True,
        extract_content=True,
        batch_size=1,
    )

    service.search_papers.assert_awaited_once_with(query="id:2401.99999", max_results=1)
    persist.assert_not_awaited()
