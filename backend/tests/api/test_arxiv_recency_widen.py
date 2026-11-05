"""search_arxiv widens an *implicit* recency window once when it is empty.

The default 12-month window made exact-title lookups of older papers
("Attention Is All You Need", 2017) come back as "No arXiv papers matched",
which is the missing title-to-arXiv chain seen in LangSmith traces
(audit 2026-11, retrieval #4). An explicit recency_days or date window is the
caller's intent and is never widened; discover_papers (or_terms) is untouched.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.agent.tools_impl import _tool_search_arxiv

_PAPER: Dict[str, Any] = {
    "id": "1706.03762",
    "title": "Attention Is All You Need",
    "authors": ["A. Vaswani"],
    "abstract": "The dominant sequence transduction models...",
    "published": "2017-06-12",
    "categories": ["cs.CL"],
    "pdf_url": "https://arxiv.org/pdf/1706.03762",
}


def _service(responder: Any) -> MagicMock:
    service = AsyncMock()
    service.search_papers = AsyncMock(side_effect=responder)
    cls = MagicMock()
    cls.return_value.__aenter__ = AsyncMock(return_value=service)
    cls.return_value.__aexit__ = AsyncMock(return_value=None)
    return cls


def _empty_in_window(**kwargs: Any) -> List[Dict[str, Any]]:
    # Only the date-bounded query is empty; all-time finds the 2017 paper.
    return [] if "submittedDate:" in kwargs["query"] else [_PAPER]


async def _run(args: Dict[str, Any], **kw: Any) -> tuple[Dict[str, Any], MagicMock]:
    cls = _service(_empty_in_window)
    with (
        patch("src.services.arxiv.arxiv_service.ArXivIngestionService", cls),
        patch("src.services.agent.tools_impl._arxiv_cache_get", return_value=None),
        patch("src.services.agent.tools_impl._arxiv_cache_set"),
        patch(
            "src.services.agent.tools_impl._arxiv_redis_get",
            AsyncMock(return_value=None),
        ),
        patch("src.services.agent.tools_impl._arxiv_redis_set", AsyncMock()),
    ):
        result = await _tool_search_arxiv(args, **kw)
    return result, cls.return_value.__aenter__.return_value.search_papers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_implicit_window_widens_once_when_empty() -> None:
    result, search = await _run({"query": "Attention Is All You Need"})

    assert search.await_count == 2
    first, second = (c.kwargs["query"] for c in search.await_args_list)
    assert "submittedDate:" in first
    assert "submittedDate:" not in second
    assert [p["id"] for p in result["papers"]] == ["1706.03762"]
    assert result["recency_widened"] is True
    assert "warning" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_recency_is_never_widened() -> None:
    result, search = await _run({"query": "transformers", "recency_days": 30})

    assert search.await_count == 1
    assert result["papers"] == []
    assert "warning" in result
    assert "recency_widened" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_date_window_is_never_widened() -> None:
    result, search = await _run(
        {"query": "transformers", "published_after": "2025-01-01"}
    )

    assert search.await_count == 1
    assert result["papers"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_discover_papers_or_terms_is_not_widened() -> None:
    result, search = await _run({"query": "transformers"}, or_terms="attention")

    assert search.await_count == 1
    assert result["papers"] == []
