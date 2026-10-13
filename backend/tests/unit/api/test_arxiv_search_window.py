"""``_tool_search_arxiv``: an absolute ``published_after`` / ``published_before``
window (GOO-435 S5c).

The tool only knew a relative lookback (``recency_days``, "now minus N days"),
so "papers from 2020" could not be asked of arXiv at all. The window is the
same arXiv ``submittedDate`` filter, with the caller's bounds, composed into the
query the service sends: it narrows what arXiv returns instead of trimming a
page that was already fetched. Pure unit tests; the arXiv service and Redis are
replaced.

Mutation checks (2026-10-13), each guard removed in turn, the named tests seen
failing, the source restored byte-identical and the tests green again: leaving
the window out of ``_arxiv_cache_key`` fails ``test_cached_results_are_keyed_by_
window``, ``test_window_keys_differ_and_windowless_keys_keep_their_shape`` and
``test_one_open_bound_uses_the_arxiv_floor_or_ceiling`` (a stale unbounded
result answered the windowed call); ignoring the window in ``_tool_search_arxiv``
fails the four tests that read the sent query. Focused command: ``pytest -q
backend/tests/unit/api/test_arxiv_search_window.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.services.agent import tools_impl as ti

pytestmark = pytest.mark.unit


class _FakeArxiv:
    """Records the query the tool sends to arXiv."""

    sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeArxiv":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def search_papers(self, **kwargs: Any) -> list[dict[str, Any]]:
        _FakeArxiv.sent.append(kwargs)
        return [
            {
                "id": "2005.11401v4",
                "title": "Retrieval-Augmented Generation",
                "authors": ["P. Lewis"],
                "abstract": "RAG.",
                "published": "2020-05-22T17:41:13Z",
            }
        ]


@pytest.fixture(autouse=True)
def fake_arxiv(monkeypatch: pytest.MonkeyPatch) -> None:
    ti._ARXIV_SEARCH_CACHE.clear()
    _FakeArxiv.sent = []
    monkeypatch.setattr(
        "src.services.arxiv.arxiv_service.ArXivIngestionService", _FakeArxiv
    )

    async def no_redis() -> None:
        return None

    monkeypatch.setattr(ti, "_get_arxiv_redis", no_redis)


def _sent_query() -> str:
    return str(_FakeArxiv.sent[-1]["query"])


async def test_both_bounds_become_an_inclusive_submitted_date_filter() -> None:
    result = await ti._tool_search_arxiv(
        {
            "query": "retrieval augmented",
            "published_after": "2020-01-01",
            "published_before": "2020-12-31",
            "recency_days": 0,
        }
    )
    assert "error" not in result and result["total"] == 1
    assert (
        _sent_query() == "(all:retrieval AND all:augmented) AND "
        "submittedDate:[202001010000 TO 202012312359]"
    )


async def test_one_open_bound_uses_the_arxiv_floor_or_ceiling() -> None:
    await ti._tool_search_arxiv({"query": "rag", "published_after": "2021-06-15"})
    assert _sent_query().endswith("submittedDate:[202106150000 TO 999912312359]")
    await ti._tool_search_arxiv({"query": "rag", "published_before": "2019-03-01"})
    assert _sent_query().endswith("submittedDate:[199001010000 TO 201903012359]")


async def test_a_window_replaces_the_default_twelve_month_lookback() -> None:
    # Without a window the tool searches the last 365 days; with one, the
    # caller's bounds are the only date filter (a 2020 window must not also be
    # cut to "after one year ago", which would make it empty).
    await ti._tool_search_arxiv({"query": "rag"})
    default_filter = _sent_query().rsplit("submittedDate:", 1)[1]
    assert default_filter.endswith(" TO 999912312359]")
    await ti._tool_search_arxiv(
        {
            "query": "rag",
            "published_after": "2020-01-01",
            "published_before": "2020-12-31",
        }
    )
    assert _sent_query().count("submittedDate:") == 1
    assert "202001010000 TO 202012312359" in _sent_query()


async def test_a_malformed_bound_is_ignored_not_sent_to_arxiv() -> None:
    await ti._tool_search_arxiv(
        {
            "query": "rag",
            "published_after": "2020-13-45",
            "published_before": "not a date",
            "recency_days": 0,
        }
    )
    assert "submittedDate" not in _sent_query()


async def test_cached_results_are_keyed_by_window() -> None:
    # recency_days is 0 for every windowed call, so without the window in the
    # key a 2020 search would be answered from an unbounded search's cache.
    await ti._tool_search_arxiv({"query": "rag", "recency_days": 0})
    await ti._tool_search_arxiv(
        {
            "query": "rag",
            "published_after": "2020-01-01",
            "published_before": "2020-12-31",
        }
    )
    await ti._tool_search_arxiv(
        {
            "query": "rag",
            "published_after": "2020-01-01",
            "published_before": "2020-12-31",
        }
    )
    assert len(_FakeArxiv.sent) == 2  # the third call is the 2020 cache hit
    assert "submittedDate" not in str(_FakeArxiv.sent[0]["query"])
    assert "202001010000 TO 202012312359" in str(_FakeArxiv.sent[1]["query"])


def test_window_keys_differ_and_windowless_keys_keep_their_shape() -> None:
    plain = ti._arxiv_cache_key("rag", 5, None, 0)
    windowed = ti._arxiv_cache_key("rag", 5, None, 0, False, ("2020-01-01", None))
    assert plain == ("rag", 5, (), 0, False)
    assert windowed != plain
    assert windowed != ti._arxiv_cache_key(
        "rag", 5, None, 0, False, ("2021-01-01", None)
    )
    assert ti._arxiv_redis_key("rag", 5, None, 0, False) == ti._arxiv_redis_key(
        "rag", 5, None, 0, False, None
    )
    assert ti._arxiv_redis_key(
        "rag", 5, None, 0, False, ("2020-01-01", "2020-12-31")
    ) != ti._arxiv_redis_key("rag", 5, None, 0, False)
