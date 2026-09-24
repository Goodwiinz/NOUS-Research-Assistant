"""Phase 2 of the arXiv-429 fix: the shared, normalized L2 cache for
``_tool_search_arxiv``. Pure unit tests — no real Redis or arXiv; the Redis
layer and the arXiv service are monkeypatched.

Pins the invariants: (1) real vs synthetic phrasings collapse to one L2 key;
(2) a fresh cache hit short-circuits the live call; (3) a 429 falls back to a
stale L2 entry; (4) Redis-down still performs the live call.
"""

from __future__ import annotations

from src.services.agent import tools_impl as ti


def _clear_l1():
    ti._ARXIV_SEARCH_CACHE.clear()


# --- key normalization ------------------------------------------------------


def test_real_and_synthetic_phrasings_share_one_key():
    user = ti._arxiv_redis_key(
        "recent papers on retrieval-augmented generation", 5, None, 365, False
    )
    synth = ti._arxiv_redis_key(
        "Search arXiv for recent papers on retrieval-augmented generation.",
        5,
        None,
        365,
        False,
    )
    assert user == synth
    assert user.startswith("arxiv:search:")


def test_distinguishing_params_yield_distinct_keys():
    base = ti._arxiv_redis_key("rag", 5, None, 365, False)
    assert ti._arxiv_redis_key("rag", 10, None, 365, False) != base  # max_results
    assert ti._arxiv_redis_key("rag", 5, ["cs.CL"], 365, False) != base  # categories
    assert ti._arxiv_redis_key("rag", 5, None, 30, False) != base  # recency
    assert (
        ti._arxiv_redis_key("rag", 5, None, 365, True) != base
    )  # chronological (sort)


def test_normalize_drops_arxiv_and_fillers():
    assert ti._normalize_cache_query("Search arXiv for the latest RAG papers.") == "rag"


# --- fake arXiv service -----------------------------------------------------


class _FakeArxiv:
    def __init__(self, papers=None, raise_exc=None):
        self._papers = papers or []
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def search_papers(self, **_kw):
        if self._raise is not None:
            raise self._raise
        return self._papers


def _patch_arxiv(monkeypatch, **kw):
    monkeypatch.setattr(
        "src.services.arxiv.arxiv_service.ArXivIngestionService",
        lambda: _FakeArxiv(**kw),
    )


# --- cache behavior ---------------------------------------------------------


async def test_l2_fresh_hit_short_circuits_live_call(monkeypatch):
    _clear_l1()
    payload = {"papers": [{"id": "1", "title": "cached"}], "total": 1, "query": "rag"}

    async def fake_get(_key, allow_stale):
        return payload if not allow_stale else None

    monkeypatch.setattr(ti, "_arxiv_redis_get", fake_get)
    # If the live path runs, this raises → proves the cache short-circuited.
    _patch_arxiv(monkeypatch, raise_exc=RuntimeError("live call must not happen"))

    out = await ti._tool_search_arxiv({"query": "rag", "max_results": 5})
    assert out["cached"] is True and out["papers"][0]["title"] == "cached"


async def test_429_falls_back_to_stale_l2(monkeypatch):
    _clear_l1()
    stale = {"papers": [{"id": "9", "title": "old"}], "total": 1, "query": "rag"}

    async def fake_get(_key, allow_stale):
        return stale if allow_stale else None  # fresh miss, stale hit

    monkeypatch.setattr(ti, "_arxiv_redis_get", fake_get)
    monkeypatch.setattr(ti, "_arxiv_redis_set", _noop_set)
    _patch_arxiv(monkeypatch, raise_exc=Exception("ArXiv rate limited (HTTP 429)"))

    out = await ti._tool_search_arxiv({"query": "rag"})
    assert out["cached"] is True and out["stale"] is True
    assert "error" not in out and out["papers"][0]["title"] == "old"


async def test_406_falls_back_to_stale_l2(monkeypatch):
    _clear_l1()
    stale = {"papers": [{"id": "9", "title": "old"}], "total": 1, "query": "rag"}

    async def fake_get(_key, allow_stale):
        return stale if allow_stale else None

    monkeypatch.setattr(ti, "_arxiv_redis_get", fake_get)
    monkeypatch.setattr(ti, "_arxiv_redis_set", _noop_set)
    _patch_arxiv(
        monkeypatch,
        raise_exc=Exception("ArXiv search unavailable (HTTP 406); secret body"),
    )

    out = await ti._tool_search_arxiv({"query": "rag"})
    assert out["cached"] is True and out["stale"] is True
    assert "error" not in out and out["papers"][0]["title"] == "old"


async def test_cold_406_is_unavailable_not_an_empty_search(monkeypatch):
    _clear_l1()

    async def fake_get(_key, allow_stale):
        return None

    monkeypatch.setattr(ti, "_arxiv_redis_get", fake_get)
    monkeypatch.setattr(ti, "_arxiv_redis_set", _noop_set)
    _patch_arxiv(
        monkeypatch,
        raise_exc=Exception("ArXiv search unavailable (HTTP 406); secret body"),
    )

    out = await ti._tool_search_arxiv({"query": "rag"})

    assert out["error_type"] == "transient"
    assert "temporarily unavailable" in out["error"]
    assert "rate limit" not in out["error"].lower()
    assert "papers" not in out
    assert "secret body" not in str(out)


async def test_redis_down_still_does_live_call(monkeypatch):
    _clear_l1()

    async def no_redis():
        return None

    monkeypatch.setattr(ti, "_get_arxiv_redis", no_redis)
    _patch_arxiv(
        monkeypatch,
        papers=[{"id": "2", "title": "live", "authors": [], "abstract": "x"}],
    )

    out = await ti._tool_search_arxiv({"query": "rag"})
    assert "error" not in out and out["papers"][0]["title"] == "live"


async def _noop_set(*_a, **_k):
    return None
