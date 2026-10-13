"""``_tool_search_arxiv``: one OR-ed request, and a rate-gate time budget
(GOO-435 S5c, ``discover_papers``).

Two keyword-only arguments that a model cannot set:

- ``or_terms``: plain keywords searched in the SAME request as the query,
  ``((<query ANDed>) OR (<or_terms ANDed>)) AND submittedDate:[..]``. arXiv
  spaces requests 3 s apart across all pods, so ``discover_papers`` sending
  its acronym recovery as a second request made every round wait 3 s.
- ``max_gate_wait_s``: the most the call waits for that gate. Over it, nothing
  is sent and the answer is the last known result (``stale``) or a payload with
  ``error_type="rate_gated"``. A fresh cached result is served before the gate
  is asked, so a busy gate never hides an answer that is already known.

Pure unit tests; the arXiv service, the gate and Redis are replaced.

Mutation checks (2026-10-13), each guard removed in turn, the named tests seen
failing, the source restored byte-identical and the tests green again:

- the cache before the gate (the cache lookup skipped for a budgeted call, so
  the gate is asked first) fails ``test_a_fresh_cached_result_is_served_
  without_asking_the_gate``;
- ``or_terms`` left out of ``_arxiv_cache_key`` / ``_arxiv_redis_key`` fails
  ``test_the_or_part_is_part_of_the_cache_identity`` (an OR search was answered
  from the plain search's cache);
- dropping the ``ArxivRateGated`` handler of ``_tool_search_arxiv`` fails
  ``test_a_busy_gate_returns_a_rate_gated_payload_and_caches_nothing``,
  ``test_a_busy_gate_serves_the_last_known_result``,
  ``test_a_fresh_cached_result_is_served_without_asking_the_gate`` and the
  discover-level ``test_a_busy_gate_through_the_real_tool_skips_arxiv_without_
  sending``.

Focused command: ``pytest -q backend/tests/unit/api/
test_arxiv_search_combined_query.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.services.agent import tools_impl as ti
from src.services.arxiv import arxiv_service as svc

pytestmark = pytest.mark.unit

WINDOW = {
    "published_after": "2020-01-01",
    "published_before": "2020-12-31",
    "recency_days": 0,
}
WINDOW_FILTER = "submittedDate:[202001010000 TO 202012312359]"


class _Gate:
    """The shared rate gate as the service sees it: counts asks, can refuse."""

    asked = 0
    refuse = False

    @classmethod
    def reset(cls) -> None:
        cls.asked = 0
        cls.refuse = False


class _FakeArxiv:
    """The arXiv service: asks the gate once per search, like the real one."""

    sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeArxiv":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def search_papers(self, **kwargs: Any) -> list[dict[str, Any]]:
        _Gate.asked += 1
        if kwargs.get("max_gate_wait_s") is not None and _Gate.refuse:
            raise svc.ArxivRateGated(kwargs["max_gate_wait_s"])
        _FakeArxiv.sent.append(kwargs)
        return [
            {
                "id": "2401.00001v1",
                "title": "Group Relative Policy Optimization",
                "authors": ["A"],
                "abstract": "GRPO.",
                "published": "2024-01-02T00:00:00Z",
            }
        ]


@pytest.fixture(autouse=True)
def fake_arxiv(monkeypatch: pytest.MonkeyPatch) -> None:
    ti._ARXIV_SEARCH_CACHE.clear()
    _FakeArxiv.sent = []
    _Gate.reset()
    monkeypatch.setattr(svc, "ArXivIngestionService", _FakeArxiv)

    async def no_redis() -> None:
        return None

    monkeypatch.setattr(ti, "_get_arxiv_redis", no_redis)


def _sent_query() -> str:
    return str(_FakeArxiv.sent[-1]["query"])


# --- the combined query ------------------------------------------------------------


async def test_the_or_part_is_composed_inside_the_window() -> None:
    result = await ti._tool_search_arxiv(
        {"query": "GRPO reward models", **WINDOW}, or_terms="GRPO"
    )

    assert "error" not in result and result["total"] == 1
    assert _sent_query() == (
        "((all:GRPO AND all:reward AND all:models) OR (all:GRPO)) AND " + WINDOW_FILTER
    )
    assert result["query"] == _sent_query()  # the payload reports what was sent


async def test_the_or_part_is_composed_inside_the_default_lookback_too() -> None:
    await ti._tool_search_arxiv({"query": "GRPO reward models"}, or_terms="GRPO")

    sent = _sent_query()
    assert sent.startswith(
        "((all:GRPO AND all:reward AND all:models) OR (all:GRPO)) AND submittedDate:["
    )
    assert sent.endswith(" TO 999912312359]")
    assert sent.count("submittedDate:") == 1


async def test_without_a_window_the_query_is_the_bare_or_expression() -> None:
    await ti._tool_search_arxiv(
        {"query": "GRPO reward models", "recency_days": 0}, or_terms="GRPO"
    )

    assert _sent_query() == "(all:GRPO AND all:reward AND all:models) OR (all:GRPO)"


async def test_several_acronyms_are_anded_like_the_old_second_call() -> None:
    await ti._tool_search_arxiv(
        {"query": "GRPO PPO agents", **WINDOW}, or_terms="GRPO PPO"
    )

    assert _sent_query() == (
        "((all:GRPO AND all:PPO AND all:agents) OR (all:GRPO AND all:PPO)) AND "
        + WINDOW_FILTER
    )


async def test_the_main_query_alone_is_unchanged() -> None:
    await ti._tool_search_arxiv({"query": "retrieval augmented", **WINDOW})

    assert _sent_query() == ("(all:retrieval AND all:augmented) AND " + WINDOW_FILTER)


@pytest.mark.parametrize(
    "or_terms",
    [
        "",  # nothing to add
        "   ",
        "recent papers on",  # only filler: nothing to search for
        "retrieval augmented",  # the same terms again
        'x") OR (all:secret',  # arXiv syntax could step out of the window
        "ti:GRPO",
    ],
)
async def test_an_unusable_or_part_is_dropped(or_terms: str) -> None:
    await ti._tool_search_arxiv(
        {"query": "retrieval augmented", **WINDOW}, or_terms=or_terms
    )

    assert _sent_query() == ("(all:retrieval AND all:augmented) AND " + WINDOW_FILTER)


async def test_the_or_part_is_cleaned_with_the_same_helpers_as_the_query() -> None:
    # "recent papers on" is filler for the main query and for the OR part.
    await ti._tool_search_arxiv(
        {"query": "recent papers on GRPO agents", **WINDOW},
        or_terms="recent papers GRPO",
    )

    assert _sent_query() == (
        "((all:GRPO AND all:agents) OR (all:GRPO)) AND " + WINDOW_FILTER
    )


# --- the cache identity -------------------------------------------------------------


async def test_the_or_part_is_part_of_the_cache_identity() -> None:
    args = {"query": "GRPO reward models", **WINDOW}
    await ti._tool_search_arxiv(args)
    await ti._tool_search_arxiv(args, or_terms="GRPO")
    assert len(_FakeArxiv.sent) == 2  # the OR search was not answered from the first

    again = await ti._tool_search_arxiv(args, or_terms="GRPO")
    assert again["cached"] is True and len(_FakeArxiv.sent) == 2

    await ti._tool_search_arxiv(args, or_terms="PPO")
    assert len(_FakeArxiv.sent) == 3  # a different OR part is a different search


def test_keys_without_an_or_part_keep_their_shape_and_digest() -> None:
    plain = ti._arxiv_cache_key("rag", 5, None, 365)
    assert plain == ("rag", 5, (), 365, False)
    assert ti._arxiv_cache_key("rag", 5, None, 365, or_terms="") == plain
    assert ti._arxiv_cache_key("rag", 5, None, 365, or_terms="GRPO") != plain
    assert ti._arxiv_redis_key("rag", 5, None, 365, False, or_terms=None) == (
        ti._arxiv_redis_key("rag", 5, None, 365, False)
    )
    assert ti._arxiv_redis_key("rag", 5, None, 365, False, or_terms="GRPO") != (
        ti._arxiv_redis_key("rag", 5, None, 365, False)
    )
    assert ti._arxiv_redis_key("rag", 5, None, 365, False, or_terms="GRPO") != (
        ti._arxiv_redis_key("rag", 5, None, 365, False, or_terms="PPO")
    )


# --- the gate budget ---------------------------------------------------------------


async def test_the_budget_reaches_the_service_only_when_given() -> None:
    await ti._tool_search_arxiv({"query": "alpha", **WINDOW})
    assert "max_gate_wait_s" not in _FakeArxiv.sent[-1]  # as before this change

    await ti._tool_search_arxiv({"query": "beta", **WINDOW}, max_gate_wait_s=1.0)
    assert _FakeArxiv.sent[-1]["max_gate_wait_s"] == 1.0


async def test_a_busy_gate_returns_a_rate_gated_payload_and_caches_nothing() -> None:
    _Gate.refuse = True

    result = await ti._tool_search_arxiv(
        {"query": "GRPO reward models", **WINDOW}, or_terms="GRPO", max_gate_wait_s=1.0
    )

    assert result["error_type"] == "rate_gated"
    assert "papers" not in result and not result.get("cached")
    assert ti._ARXIV_SEARCH_CACHE == {}  # a refusal is not an answer

    _Gate.refuse = False  # the gate frees up: the same search now goes through
    retried = await ti._tool_search_arxiv(
        {"query": "GRPO reward models", **WINDOW}, or_terms="GRPO", max_gate_wait_s=1.0
    )
    assert retried["total"] == 1 and "error" not in retried


async def test_a_busy_gate_serves_the_last_known_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_known = {"papers": [{"id": "9", "title": "old"}], "total": 1, "query": "q"}

    async def shared_cache(_key: str, allow_stale: bool) -> dict[str, Any] | None:
        return last_known if allow_stale else None  # past the fresh window

    monkeypatch.setattr(ti, "_arxiv_redis_get", shared_cache)
    _Gate.refuse = True

    result = await ti._tool_search_arxiv(
        {"query": "GRPO reward models", **WINDOW}, or_terms="GRPO", max_gate_wait_s=1.0
    )

    assert result["papers"] == last_known["papers"]
    assert result["cached"] is True and result["stale"] is True
    assert "error" not in result and "busy" in result["warning"]
    assert _FakeArxiv.sent == []  # nothing was sent to arXiv


async def test_a_fresh_cached_result_is_served_without_asking_the_gate() -> None:
    args = {"query": "GRPO reward models", **WINDOW}
    await ti._tool_search_arxiv(args, or_terms="GRPO")
    assert _Gate.asked == 1
    _Gate.refuse = True

    result = await ti._tool_search_arxiv(args, or_terms="GRPO", max_gate_wait_s=1.0)

    assert result["cached"] is True and result["total"] == 1
    assert "stale" not in result  # fresh, not the fallback
    assert _Gate.asked == 1  # the gate was never asked

    # Control: an uncached search does reach the (busy) gate.
    other = await ti._tool_search_arxiv(
        {"query": "something else", **WINDOW}, max_gate_wait_s=1.0
    )
    assert _Gate.asked == 2 and other["error_type"] == "rate_gated"
