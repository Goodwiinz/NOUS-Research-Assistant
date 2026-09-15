"""Tests for source connectors: ArXiv, Semantic Scholar, RAG Store."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.research_engine.connectors.arxiv_connector import ArxivConnector
from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.connectors.rag_store_connector import (
    RagStoreConnector,
)
from src.services.research_engine.connectors.semantic_scholar_connector import (
    SemanticScholarConnector,
)

# ---------------------------------------------------------------------------
# SourceDocument
# ---------------------------------------------------------------------------


class TestSourceDocument:
    def test_creation_defaults(self):
        doc = SourceDocument(connector_type="test")
        assert doc.connector_type == "test"
        assert doc.external_id is None
        assert doc.title == ""
        assert doc.authors == []
        assert doc.abstract is None
        assert doc.url is None
        assert doc.full_text is None
        assert doc.metadata == {}
        assert doc.content_hash is None

    def test_creation_with_values(self):
        doc = SourceDocument(
            connector_type="arxiv",
            external_id="2301.00001",
            title="Test Paper",
            authors=["Alice", "Bob"],
            abstract="An abstract.",
            url="https://arxiv.org/abs/2301.00001",
            full_text="Full text here.",
            metadata={"category": "cs.AI"},
        )
        assert doc.title == "Test Paper"
        assert doc.authors == ["Alice", "Bob"]
        assert doc.metadata == {"category": "cs.AI"}

    def test_compute_hash(self):
        doc = SourceDocument(
            connector_type="test",
            title="Title",
            abstract="Abstract",
            full_text="Body",
        )
        doc.compute_hash()
        expected = hashlib.sha256("TitleAbstractBody".encode()).hexdigest()
        assert doc.content_hash == expected

    def test_compute_hash_none_fields(self):
        doc = SourceDocument(connector_type="test", title="Only Title")
        doc.compute_hash()
        expected = hashlib.sha256("Only Title".encode()).hexdigest()
        assert doc.content_hash == expected


# ---------------------------------------------------------------------------
# ArxivConnector
# ---------------------------------------------------------------------------

ARXIV_XML_RESPONSE = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Test Paper One</title>
    <summary>Abstract of paper one.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2301.00002v1</id>
    <title>Test Paper Two</title>
    <summary>Abstract of paper two.</summary>
    <author><name>Charlie</name></author>
  </entry>
</feed>
"""


class TestArxivConnector:
    @pytest.mark.asyncio
    async def test_search_parses_xml(self):
        mock_response = MagicMock()
        mock_response.text = ARXIV_XML_RESPONSE
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "src.services.research_engine.connectors.arxiv_connector.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "src.services.arxiv.arxiv_service._acquire_arxiv_rate_slot",
                AsyncMock(return_value=0.0),
            ),
        ):
            connector = ArxivConnector()
            results = await connector.search("test query", max_results=10)

        assert len(results) == 2

        assert results[0].connector_type == "arxiv"
        assert results[0].title == "Test Paper One"
        assert results[0].abstract == "Abstract of paper one."
        assert results[0].authors == ["Alice", "Bob"]
        assert results[0].external_id == "http://arxiv.org/abs/2301.00001v1"

        assert results[1].title == "Test Paper Two"
        assert results[1].authors == ["Charlie"]

        # https (the plain-HTTP endpoint redirects and leaks the query on the
        # way) and follow_redirects, now that the connector goes through the
        # shared arXiv rate gate like every other caller.
        mock_client.get.assert_called_once_with(
            "https://export.arxiv.org/api/query",
            params={
                # Per-token fielding: unfielded terms are OR'd by arXiv, so
                # multiword queries are AND-joined with all: per token.
                "search_query": "all:test AND all:query",
                "start": 0,
                "max_results": 10,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            follow_redirects=True,
        )


# ---------------------------------------------------------------------------
# SemanticScholarConnector
# ---------------------------------------------------------------------------

SEMANTIC_SCHOLAR_JSON = {
    "data": [
        {
            "paperId": "abc123",
            "externalIds": {"DOI": "10.1234/abc", "PubMed": "123"},
            "title": "S2 Paper One",
            "abstract": "First abstract.",
            "authors": [{"name": "Dave"}, {"name": "Eve"}],
            "url": "https://semanticscholar.org/paper/abc123",
        },
        {
            "paperId": "def456",
            "title": "S2 Paper Two",
            "abstract": "Second abstract.",
            "authors": [{"name": "Frank"}],
            "url": "https://semanticscholar.org/paper/def456",
        },
    ]
}


class TestSemanticScholarConnector:
    @pytest.mark.asyncio
    async def test_search_parses_json(self):
        mock_response = MagicMock()
        mock_response.json.return_value = SEMANTIC_SCHOLAR_JSON
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.research_engine.connectors.semantic_scholar_connector.httpx.AsyncClient",
            return_value=mock_client,
        ):
            connector = SemanticScholarConnector()
            results = await connector.search("deep learning", max_results=10)

        assert len(results) == 2

        assert results[0].connector_type == "semantic_scholar"
        assert results[0].external_id == "abc123"
        assert results[0].title == "S2 Paper One"
        assert results[0].authors == ["Dave", "Eve"]
        assert results[0].url == "https://semanticscholar.org/paper/abc123"
        assert results[0].metadata["doi"] == "10.1234/abc"
        assert results[0].metadata["pmid"] == "123"

        assert results[1].title == "S2 Paper Two"

        mock_client.get.assert_called_once_with(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": "deep learning",
                "limit": 10,
                "fields": "paperId,title,abstract,authors,url,externalIds,publicationDate,publicationTypes,openAccessPdf",
            },
            headers={},
        )

    @pytest.mark.asyncio
    async def test_search_with_api_key(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.research_engine.connectors.semantic_scholar_connector.httpx.AsyncClient",
            return_value=mock_client,
        ):
            connector = SemanticScholarConnector(api_key="my-secret-key")
            results = await connector.search("query")

        assert results == []
        call_kwargs = mock_client.get.call_args
        assert call_kwargs.kwargs["headers"] == {"x-api-key": "my-secret-key"}


# ---------------------------------------------------------------------------
# RagStoreConnector
# ---------------------------------------------------------------------------


class TestRagStoreConnector:
    @pytest.mark.asyncio
    async def test_search_calls_search_fn(self):
        mock_search_fn = AsyncMock(
            return_value={
                "results": [
                    {
                        "id": "doc-1",
                        "title": "Local Doc One",
                        "content": "Some content.",
                        "metadata": {"source": "upload"},
                    },
                    {
                        "id": "doc-2",
                        "title": "Local Doc Two",
                        "content": "Other content.",
                        "metadata": {},
                    },
                ]
            }
        )

        connector = RagStoreConnector(search_fn=mock_search_fn)
        results = await connector.search("local query", max_results=5)

        mock_search_fn.assert_awaited_once_with("local query", 5)
        assert len(results) == 2

        assert results[0].connector_type == "rag_store"
        assert results[0].external_id == "doc-1"
        assert results[0].title == "Local Doc One"
        assert results[0].full_text == "Some content."
        assert results[0].metadata == {"source": "upload"}

        assert results[1].external_id == "doc-2"
        assert results[1].title == "Local Doc Two"

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        mock_search_fn = AsyncMock(return_value={"results": []})
        connector = RagStoreConnector(search_fn=mock_search_fn)
        results = await connector.search("nothing", max_results=10)
        assert results == []
