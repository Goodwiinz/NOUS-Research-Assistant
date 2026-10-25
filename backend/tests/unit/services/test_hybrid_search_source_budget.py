"""F1: hybrid source collection must honour one bounded request deadline and
never queue unbounded work on the shared executor.

Before: ``_execute_parallel_searches`` called ``future.result(timeout=10)``
per source *sequentially*, so a request could wait ``10s x sources``; a
timed-out future kept running on the 3-worker pool and later submissions
queued behind it without bound.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from src.models.search_schemas import SearchQuery, SearchType
from src.services.search.hybrid_search_service import (
    HybridSearchService,
    SearchSourceResult,
    SearchSourceType,
)

pytestmark = pytest.mark.unit


def _ok(source: SearchSourceType) -> SearchSourceResult:
    return SearchSourceResult(
        source_type=source,
        results=[],
        search_time_ms=1.0,
        total_available=0,
        success=True,
    )


def _request() -> SearchQuery:
    return SearchQuery(query="who is alice", search_type=SearchType.HYBRID)


def test_sources_share_one_request_deadline() -> None:
    """Two sources that each take longer than the request budget must both
    fail by the deadline — the second source must not get a fresh timeout."""
    svc = HybridSearchService()
    svc.request_timeout_seconds = 0.2
    release = threading.Event()

    def _slow(*_a: Any, **_k: Any) -> Any:
        release.wait(2.0)
        return _ok(SearchSourceType.FULLTEXT)

    svc._execute_fulltext_search = _slow  # type: ignore[method-assign]
    svc._execute_knowledge_graph_search = _slow  # type: ignore[method-assign]

    started = time.monotonic()
    try:
        results = svc._execute_parallel_searches(
            _request(),
            [SearchSourceType.FULLTEXT, SearchSourceType.KNOWLEDGE_GRAPH],
            "user-1",
            "org-1",
            None,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"request waited {elapsed:.2f}s for a 0.2s budget"
    assert set(results) == {
        SearchSourceType.FULLTEXT,
        SearchSourceType.KNOWLEDGE_GRAPH,
    }
    for source_result in results.values():
        assert source_result.success is False
        assert source_result.results == []
        assert source_result.error  # stable, non-empty failure reason


def test_fast_source_still_succeeds_when_sibling_stalls() -> None:
    """A stalled source must not take the healthy source down with it."""
    svc = HybridSearchService()
    svc.request_timeout_seconds = 0.2
    release = threading.Event()

    def _stall(*_a: Any, **_k: Any) -> Any:
        release.wait(2.0)
        return _ok(SearchSourceType.FULLTEXT)

    svc._execute_fulltext_search = _stall  # type: ignore[method-assign]
    svc._execute_knowledge_graph_search = (  # type: ignore[method-assign]
        lambda *_a, **_k: _ok(SearchSourceType.KNOWLEDGE_GRAPH)
    )

    try:
        results = svc._execute_parallel_searches(
            _request(),
            [SearchSourceType.FULLTEXT, SearchSourceType.KNOWLEDGE_GRAPH],
            "user-1",
            "org-1",
            None,
        )
    finally:
        release.set()

    assert results[SearchSourceType.FULLTEXT].success is False
    assert results[SearchSourceType.KNOWLEDGE_GRAPH].success is True


def test_saturated_pool_fails_fast_instead_of_queueing() -> None:
    """When every in-flight slot is taken, a new request must not enqueue
    more work behind stalled searches; it reports the source unavailable."""
    svc = HybridSearchService()
    calls: list[str] = []

    def _record(*_a: Any, **_k: Any) -> Any:
        calls.append("called")
        return _ok(SearchSourceType.FULLTEXT)

    svc._execute_fulltext_search = _record  # type: ignore[method-assign]

    # Exhaust every slot so the next submission would have to queue.
    taken = 0
    while svc._search_slots.acquire(blocking=False):
        taken += 1
    assert taken > 0
    try:
        results = svc._execute_parallel_searches(
            _request(), [SearchSourceType.FULLTEXT], "user-1", "org-1", None
        )
    finally:
        for _ in range(taken):
            svc._search_slots.release()

    assert calls == []
    assert results[SearchSourceType.FULLTEXT].success is False
    assert results[SearchSourceType.FULLTEXT].error == "search capacity exhausted"


def test_timed_out_searches_do_not_leak_capacity() -> None:
    """Slots are tied to the worker finishing (or a queued future being
    cancelled), not to the request's wait. Repeatedly timing out must not
    leave the pool permanently "exhausted"; once the stalled workers return,
    the service is back to normal."""
    svc = HybridSearchService()
    svc.request_timeout_seconds = 0.05
    release = threading.Event()

    def _stall(*_a: Any, **_k: Any) -> Any:
        release.wait(5.0)
        return _ok(SearchSourceType.FULLTEXT)

    svc._execute_fulltext_search = _stall  # type: ignore[method-assign]

    def _one() -> SearchSourceResult:
        return svc._execute_parallel_searches(
            _request(), [SearchSourceType.FULLTEXT], "user-1", "org-1", None
        )[SearchSourceType.FULLTEXT]

    try:
        # More timed-out requests than there are slots. Only the few actually
        # running on the pool's workers hold a slot; queued ones are cancelled
        # on timeout and give theirs back, so admission never collapses.
        for _ in range(svc.max_inflight_searches + 1):
            outcome = _one()
            assert outcome.success is False
            assert outcome.error != "search capacity exhausted"
    finally:
        release.set()

    # Let the stalled workers return, then a fresh search must succeed.
    svc._execute_fulltext_search = (  # type: ignore[method-assign]
        lambda *_a, **_k: _ok(SearchSourceType.FULLTEXT)
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _one().success:
            break
        time.sleep(0.05)
    else:
        pytest.fail("pool never recovered after stalled searches finished")


def test_knowledge_graph_leg_fails_closed_without_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F5: the KG leg must never run an unscoped entity search."""
    from src.services.knowledge_graph import knowledge_graph_service as kg_mod

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("search_entities must not run without an org")

    monkeypatch.setattr(kg_mod.knowledge_graph_service, "search_entities", _boom)

    svc = HybridSearchService()
    result = svc._execute_knowledge_graph_search(
        _request(), "user-1", None, None  # type: ignore[arg-type]
    )

    # GOO-399 semantics: an empty, successful leg (no cross-tenant rows).
    assert result.results == []
