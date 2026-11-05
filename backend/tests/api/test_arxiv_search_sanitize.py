"""Unit tests for arXiv query sanitisation and relevance-sort default.

Covers:
- ``_sanitize_arxiv_query``: strips filler/recency words without clobbering
  meaningful tokens, and falls back to the original when all tokens are noise.
- ``_tool_search_arxiv``: passes a sanitised query and ``sort_by="relevance"``
  to the service by default, and ``sort_by="submittedDate"`` when
  ``chronological=True`` is requested.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services.agent.tools_impl import (
    _field_arxiv_query,
    _sanitize_arxiv_query,
    _tool_search_arxiv,
)

# ---------------------------------------------------------------------------
# _sanitize_arxiv_query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sanitize_strips_recency_filler_words() -> None:
    result = _sanitize_arxiv_query("recent papers on retrieval-augmented generation")
    assert result == "retrieval-augmented generation"


@pytest.mark.unit
def test_sanitize_strips_leading_latest() -> None:
    result = _sanitize_arxiv_query("latest RAG papers")
    assert result == "RAG"


@pytest.mark.unit
def test_sanitize_preserves_clean_query() -> None:
    result = _sanitize_arxiv_query("transformers")
    assert result == "transformers"


@pytest.mark.unit
def test_sanitize_all_stopwords_returns_original() -> None:
    """If every token is a stopword the original is returned unchanged."""
    query = "recent papers on the"
    result = _sanitize_arxiv_query(query)
    assert result == query


@pytest.mark.unit
def test_sanitize_strips_venue_tokens() -> None:
    """Conference names have no arXiv field; ANDing them empties the query."""
    result = _sanitize_arxiv_query("jailbreak NeurIPS ICML adversarial prompts")
    assert result == "jailbreak adversarial prompts"


@pytest.mark.unit
def test_sanitize_dedupes_repeated_terms() -> None:
    """Repeated tokens (quoted or not) collapse to one occurrence."""
    result = _sanitize_arxiv_query("jailbreak RLHF jailbreak 'RLHF'")
    assert result == "jailbreak RLHF"


@pytest.mark.unit
def test_sanitize_traced_dead_query_becomes_searchable() -> None:
    """Regression: the exact query from thread 9e151940 returned 0 papers
    because it ANDed venue names + duplicate quoted terms. After sanitising it
    must reduce to real topic keywords only."""
    query = "jailbreak RLHF NeurIPS ICML adversarial prompts 'RLHF' 'jailbreak' NeurIPS ICML"
    result = _sanitize_arxiv_query(query)
    assert result == "jailbreak RLHF adversarial prompts"


@pytest.mark.unit
def test_sanitize_keeps_quoted_title_verbatim() -> None:
    # Stopwords attached to a quote ('"A', 'on') used to be dropped with the
    # quote itself, leaving 'Survey Large Language Models"': unbalanced, and
    # passed through to arXiv unchanged because it contains a quote.
    title = '"A Survey on Large Language Models"'
    assert _sanitize_arxiv_query(title) == title


@pytest.mark.unit
def test_sanitize_strips_filler_outside_quotes_only() -> None:
    result = _sanitize_arxiv_query('recent papers on "the transformer" latest')
    assert result == '"the transformer"'
    assert result.count('"') == 2


# ---------------------------------------------------------------------------
# _tool_search_arxiv — sort and query sanitisation via mocked service
# ---------------------------------------------------------------------------


def _make_service_mock() -> AsyncMock:
    """Return an async-context-manager mock whose search_papers returns one paper."""
    paper = {
        "id": "2401.00001",
        "title": "RAG survey",
        "authors": ["A. Author"],
        "abstract": "A survey of retrieval-augmented generation.",
        "published": "2024-01-01",
        "categories": ["cs.AI"],
        "pdf_url": "https://arxiv.org/pdf/2401.00001",
    }
    service = AsyncMock()
    service.search_papers = AsyncMock(return_value=[paper])
    return service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_uses_relevance_sort_by_default() -> None:
    """Default call should pass sort_by='relevance' and a sanitised query."""
    service = _make_service_mock()

    # ArXivIngestionService is imported lazily inside the function body, so
    # patch it at its definition site (the module it lives in).
    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await _tool_search_arxiv({"query": "recent papers on RAG"})

    assert "papers" in result
    _call = service.search_papers.call_args
    # sort_by must be 'relevance' (not 'submittedDate')
    assert _call.kwargs.get("sort_by") == "relevance"
    # The composed query sent to arXiv must not contain the stripped words
    sent_query: str = _call.kwargs.get("query", "") or _call.args[0]
    assert "recent" not in sent_query
    assert "papers" not in sent_query
    assert "on" not in sent_query
    # The topic keyword must survive
    assert "RAG" in sent_query
    # The date-window filter must still be present
    assert "submittedDate:" in sent_query


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_chronological_uses_date_sort() -> None:
    """chronological=True must flip sort_by to 'submittedDate'."""
    service = _make_service_mock()

    # ArXivIngestionService is imported lazily inside the function body, so
    # patch it at its definition site (the module it lives in).
    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await _tool_search_arxiv({"query": "X", "chronological": True})

    _call = service.search_papers.call_args
    assert _call.kwargs.get("sort_by") == "submittedDate"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_empty_results_include_honest_warning() -> None:
    """Empty arXiv searches must surface a warning instead of silent success."""
    service = AsyncMock()
    service.search_papers = AsyncMock(return_value=[])

    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set") as cache_set,
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await _tool_search_arxiv({"query": "nonexistent topic"})

    assert result["papers"] == []
    assert result["total"] == 0
    assert "warning" in result
    assert "No arXiv papers matched" in result["warning"]
    assert "do not claim" in result["warning"]
    cached_payload = cache_set.call_args.args[1]
    assert cached_payload["warning"] == result["warning"]


# ---------------------------------------------------------------------------
# recency_days — the window must be reachable from the public tool schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_recency_days_zero_disables_date_filter() -> None:
    """recency_days=0 must drop the submittedDate window entirely."""
    service = _make_service_mock()

    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await _tool_search_arxiv({"query": "RAG", "recency_days": 0})

    sent_query = service.search_papers.call_args.kwargs["query"]
    assert "submittedDate:" not in sent_query


# Schema exposure + wrapper pass-through are covered by #1404 in
# tests/unit/services/test_agent_tools.py; only the impl-side behaviour it
# does not reach is asserted here.


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("recency_days", [-5, 1_000_000])
async def test_tool_search_arxiv_clamps_out_of_range_recency(recency_days: int) -> None:
    """Out-of-range windows must not raise — a negative disables the filter and
    an oversized one is capped instead of blowing up on timedelta overflow.

    Clamping lives in the impl, so the research subgraph's direct-search fast
    path (which builds this args dict itself) is covered too.
    """
    service = _make_service_mock()

    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await _tool_search_arxiv(
            {"query": "RAG", "recency_days": recency_days}
        )

    assert "papers" in result
    sent_query = service.search_papers.call_args.kwargs["query"]
    # Negative clamps to 0 (no filter); oversized clamps to the cap (filter present).
    assert ("submittedDate:" in sent_query) is (recency_days > 0)


# ---------------------------------------------------------------------------
# _field_arxiv_query — plain keywords get all:-scoped AND terms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_field_query_ands_plain_keywords() -> None:
    result = _field_arxiv_query("retrieval-augmented generation")
    assert result == "all:retrieval-augmented AND all:generation"


@pytest.mark.unit
def test_field_query_single_token() -> None:
    assert _field_arxiv_query("transformers") == "all:transformers"


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        'ti:"attention is all you need"',
        "cat:cs.CL",
        "retrieval AND generation",
        "diffusion OR flow",
        "(retrieval) ANDNOT vision",
    ],
)
def test_field_query_preserves_arxiv_syntax(query: str) -> None:
    """Queries already written in arXiv syntax must pass through untouched."""
    assert _field_arxiv_query(query) == query


@pytest.mark.unit
def test_field_query_empty_passthrough() -> None:
    assert _field_arxiv_query("") == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_chronological_sends_fielded_query() -> None:
    """Chronological search must send all:-scoped AND terms, not bare keywords.

    Bare multi-word queries are OR'd by the arXiv API; under submittedDate
    sort that returns the newest submissions regardless of topic.
    """
    service = _make_service_mock()

    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await _tool_search_arxiv(
            {
                "query": "retrieval-augmented generation",
                "chronological": True,
                "recency_days": 60,
            }
        )

    _call = service.search_papers.call_args
    assert _call.kwargs.get("sort_by") == "submittedDate"
    sent_query = _call.kwargs["query"]
    assert "all:retrieval-augmented AND all:generation" in sent_query
    assert "submittedDate:" in sent_query


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_search_arxiv_pure_filler_query_stays_unfielded() -> None:
    """Sanitizer fallback (all tokens are stopwords) must NOT be fielded —
    'all:recent AND all:the' would rewrite a deliberately-preserved query
    into an over-restrictive one (codex audit on #1406, finding 3)."""
    service = _make_service_mock()

    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService") as mock_cls,
        patch(
            "src.services.agent.tools_impl._arxiv_cache_get",
            return_value=None,
        ),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
    ):
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=service)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await _tool_search_arxiv({"query": "recent papers on the"})

    sent_query = service.search_papers.call_args.kwargs["query"]
    assert "all:" not in sent_query
    assert "recent papers on the" in sent_query
