"""The arXiv rate gate with a wait budget (GOO-435 S5c, ``discover_papers``).

arXiv answers in about 0.3 s; what makes ``discover_papers`` slow is the shared
gate that spaces every request 3 s apart. A caller that passes
``max_gate_wait_s`` asks for a slot only if the wait is within that budget:
over it, nothing is reserved, slept or sent, and ``ArxivRateGated`` is raised.
Every caller that passes no budget behaves exactly as before, including the
deliberate "a queue deeper than the cap proceeds now"
(``test_arxiv_rate_gate.py::test_deep_queue_proceeds_now``).

Pure unit tests. A fake Redis runs the same slot-reservation logic as the Lua
script (the real script is checked against a real ``redis-server`` by hand, see
the mutation notes); ``asyncio.sleep`` and httpx are replaced so no test waits
or sends anything.

Mutation checks (2026-10-13), each guard removed in turn, the named tests seen
failing, the source restored byte-identical and the tests green again:

- the budget check, shared gate (``_acquire_arxiv_rate_slot`` not raising on -1
  for a budgeted caller, so it proceeds now like an unbudgeted one) fails
  ``test_over_budget_raises_and_reserves_nothing`` and
  ``test_a_budgeted_call_over_budget_is_not_sent_or_slept``;
- the budget not reaching the script (``maxwait`` left at the 30 s cap) fails
  ``test_the_budget_is_the_maxwait_handed_to_the_script``,
  ``test_over_budget_raises_and_reserves_nothing`` and
  ``test_a_budgeted_call_over_budget_is_not_sent_or_slept``;
- the budget check, per-process gate (``_make_async_request`` sleeping the
  missing seconds instead of raising) fails
  ``test_per_process_over_budget_is_gated_without_sleeping``;
- ``search_papers`` wrapping the gate error in ``IngestionError`` fails
  ``test_search_papers_lets_the_gate_error_through_unwrapped`` and
  ``test_a_later_page_over_budget_returns_the_pages_already_read``.

The fake Redis re-implements the Lua script, so the script itself was also run
by hand against a real ``redis-server`` (not part of this suite): a budgeted
call on an idle gate got wait 0 and reserved; the next one, 1 s budget, was
refused with the key unchanged; an unbudgeted call advanced the key by one
interval; a 10 s budget waited the queued 6 s and reserved; a 60 s-deep queue
with no budget returned 0 and left the key untouched.

Focused command: ``pytest -q backend/tests/unit/services/arxiv/
test_arxiv_gate_budget.py``.
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest

from src.services.arxiv import arxiv_service as svc
from src.services.arxiv.arxiv_service import ArXivIngestionService, ArxivRateGated

pytestmark = pytest.mark.unit


class _FakeRedis:
    """The Lua slot reservation in Python; records the ``maxwait`` it was given.

    ``eval`` is the Redis ``EVAL`` command of the async client (server-side
    Lua), not Python's ``eval``; nothing here executes a string.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.maxwaits: list[int] = []
        self.sets = 0

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        key: str,
        now_ms: int,
        interval: int,
        maxwait: int,
        _ttl: int,
    ) -> int:
        self.maxwaits.append(int(maxwait))
        last = int(self.store.get(key, 0))
        slot = max(int(now_ms), last + int(interval))
        wait = slot - int(now_ms)
        if wait > int(maxwait):
            return -1  # no SET: nothing is reserved
        self.store[key] = slot
        self.sets += 1
        return wait


@pytest.fixture
def redis_gate(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(svc, "_arxiv_gate_redis", fake, raising=False)
    monkeypatch.setattr(svc, "_arxiv_gate_disabled", False, raising=False)
    return fake


def _busy_for(fake: _FakeRedis, seconds: float) -> None:
    """Make the next reservation wait about ``seconds``."""
    last_ms = int(time.time() * 1000) + int(seconds * 1000) - svc._ARXIV_MIN_INTERVAL_MS
    fake.store[svc._ARXIV_RATE_KEY] = last_ms


# --- the shared (Redis) gate ---------------------------------------------------


async def test_over_budget_raises_and_reserves_nothing(redis_gate: _FakeRedis) -> None:
    _busy_for(redis_gate, 2.0)
    before = dict(redis_gate.store)

    with pytest.raises(ArxivRateGated):
        await svc._acquire_arxiv_rate_slot(max_wait_s=1.0)

    assert redis_gate.store == before  # the slot was not taken
    assert redis_gate.sets == 0


async def test_under_budget_reserves_and_returns_the_wait(
    redis_gate: _FakeRedis,
) -> None:
    _busy_for(redis_gate, 0.6)

    wait = await svc._acquire_arxiv_rate_slot(max_wait_s=1.0)

    assert wait is not None and 0.4 <= wait <= 0.6
    assert redis_gate.sets == 1  # this one is used, so it is reserved


async def test_an_idle_gate_is_free_within_any_budget(redis_gate: _FakeRedis) -> None:
    assert await svc._acquire_arxiv_rate_slot(max_wait_s=0.0) == 0.0


async def test_the_budget_is_the_maxwait_handed_to_the_script(
    redis_gate: _FakeRedis,
) -> None:
    await svc._acquire_arxiv_rate_slot(max_wait_s=1.0)
    redis_gate.store.clear()
    await svc._acquire_arxiv_rate_slot()  # no budget: the gate's own cap
    redis_gate.store.clear()
    await svc._acquire_arxiv_rate_slot(max_wait_s=600.0)  # never above the cap
    redis_gate.store.clear()
    await svc._acquire_arxiv_rate_slot(max_wait_s=-5.0)  # nonsense is "no wait"

    assert redis_gate.maxwaits == [1000, svc._ARXIV_MAX_WAIT_MS] + [
        svc._ARXIV_MAX_WAIT_MS,
        0,
    ]


async def test_the_lua_script_refuses_before_it_writes() -> None:
    # The reservation is atomic only if the "too deep" return comes before the
    # SET; a budgeted caller that is refused must leave the key as it was.
    script = svc._ARXIV_GATE_LUA
    assert script.index("return -1") < script.index("'SET'")


async def test_no_budget_keeps_the_old_deep_queue_behaviour(
    redis_gate: _FakeRedis,
) -> None:
    redis_gate.store[svc._ARXIV_RATE_KEY] = int(time.time() * 1000) + 60_000

    assert await svc._acquire_arxiv_rate_slot() == 0.0  # proceed now, no raise


async def test_a_redis_error_falls_back_even_with_a_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Down:
        async def eval(self, *_a: Any, **_k: Any) -> int:
            raise RuntimeError("redis down")

    monkeypatch.setattr(svc, "_arxiv_gate_redis", Down(), raising=False)
    monkeypatch.setattr(svc, "_arxiv_gate_disabled", False, raising=False)

    assert await svc._acquire_arxiv_rate_slot(max_wait_s=1.0) is None
    assert svc._arxiv_gate_disabled is False


# --- _make_async_request: shared and per-process gates, no network -----------


class _Response:
    status_code = 200
    text = "<feed/>"
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


class _Http:
    """Stands in for ``httpx.AsyncClient``; counts the requests it is given."""

    sent = 0

    async def __aenter__(self) -> "_Http":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, *_a: Any, **_k: Any) -> _Response:
        _Http.sent += 1
        return _Response()


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No network, no real sleeping; returns the sleeps that were asked for."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(svc.asyncio, "sleep", sleep)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda **_k: _Http())
    _Http.sent = 0
    return slept


@pytest.fixture
def per_process_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No shared gate: ``_make_async_request`` uses its own clock."""
    monkeypatch.setattr(svc, "_arxiv_gate_redis", None, raising=False)
    monkeypatch.setattr(svc, "_arxiv_gate_disabled", True, raising=False)


def _last_request(monkeypatch: pytest.MonkeyPatch, seconds_ago: float) -> float:
    at = time.monotonic() - seconds_ago
    monkeypatch.setattr(ArXivIngestionService, "_last_request_time", at)
    return at


async def test_a_budgeted_call_over_budget_is_not_sent_or_slept(
    redis_gate: _FakeRedis, offline: list[float]
) -> None:
    _busy_for(redis_gate, 2.5)

    with pytest.raises(ArxivRateGated):
        await ArXivIngestionService()._make_async_request(
            "http://x", {}, max_gate_wait_s=1.0
        )

    assert offline == []  # no asyncio.sleep of any length
    assert _Http.sent == 0


async def test_a_budgeted_call_under_budget_waits_then_sends(
    redis_gate: _FakeRedis, offline: list[float]
) -> None:
    _busy_for(redis_gate, 0.7)

    text = await ArXivIngestionService()._make_async_request(
        "http://x", {}, max_gate_wait_s=1.0
    )

    assert text == "<feed/>" and _Http.sent == 1
    assert len(offline) == 1 and 0.5 <= offline[0] <= 0.7


async def test_per_process_over_budget_is_gated_without_sleeping(
    monkeypatch: pytest.MonkeyPatch, per_process_gate: None, offline: list[float]
) -> None:
    at = _last_request(monkeypatch, seconds_ago=0.5)  # 2.5 s still to wait

    with pytest.raises(ArxivRateGated):
        await ArXivIngestionService()._make_async_request(
            "http://x", {}, max_gate_wait_s=1.0
        )

    assert offline == []  # in particular, no sleep of about 3 s
    assert _Http.sent == 0
    assert ArXivIngestionService._last_request_time == at  # the clock is untouched


async def test_per_process_under_budget_waits_then_sends(
    monkeypatch: pytest.MonkeyPatch, per_process_gate: None, offline: list[float]
) -> None:
    before = _last_request(monkeypatch, seconds_ago=2.5)  # 0.5 s to wait

    await ArXivIngestionService()._make_async_request(
        "http://x", {}, max_gate_wait_s=1.0
    )

    assert len(offline) == 1 and 0.3 <= offline[0] <= 0.5
    assert _Http.sent == 1
    assert ArXivIngestionService._last_request_time > before


async def test_per_process_without_a_budget_still_waits_the_full_gap(
    monkeypatch: pytest.MonkeyPatch, per_process_gate: None, offline: list[float]
) -> None:
    _last_request(monkeypatch, seconds_ago=0.5)

    await ArXivIngestionService()._make_async_request("http://x", {})

    assert len(offline) == 1 and 2.3 <= offline[0] <= 2.5  # not gated, not skipped
    assert _Http.sent == 1


async def test_a_call_without_a_budget_proceeds_through_a_deep_queue(
    redis_gate: _FakeRedis, offline: list[float]
) -> None:
    redis_gate.store[svc._ARXIV_RATE_KEY] = int(time.time() * 1000) + 60_000

    await ArXivIngestionService()._make_async_request("http://x", {})

    assert _Http.sent == 1 and offline == []


# --- search_papers carries the budget ------------------------------------------


def _feed(entries: str, total: int) -> str:
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{total}</opensearch:totalResults>{entries}</feed>"
    )


def _entry(number: int) -> str:
    return (
        f"<entry><id>http://arxiv.org/abs/2401.{number:05d}v1</id>"
        f"<title>Distinct arXiv paper number {number}</title>"
        "<summary>S.</summary><published>2024-01-02T00:00:00Z</published>"
        "<updated>2024-01-02T00:00:00Z</updated></entry>"
    )


def _service() -> ArXivIngestionService:
    service = ArXivIngestionService()
    service.session = cast(Any, object())  # search_papers only checks presence
    return service


async def test_search_papers_passes_the_budget_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    async def request(url: str, params: dict, **options: Any) -> str:
        seen.append(options)
        return _feed(_entry(1), total=1)

    service = _service()
    monkeypatch.setattr(service, "_make_async_request", request)

    await service.search_papers(query="all:x", max_results=1)
    await service.search_papers(query="all:x", max_results=1, max_gate_wait_s=1.0)

    assert seen == [{}, {"max_gate_wait_s": 1.0}]


async def test_search_papers_lets_the_gate_error_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(url: str, params: dict, **options: Any) -> str:
        raise ArxivRateGated(1.0)

    service = _service()
    monkeypatch.setattr(service, "_make_async_request", request)

    with pytest.raises(ArxivRateGated):
        await service.search_papers(query="all:x", max_results=5, max_gate_wait_s=1.0)


async def test_a_later_page_over_budget_returns_the_pages_already_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_feed(_entry(1) + _entry(2), total=5)]

    async def request(url: str, params: dict, **options: Any) -> str:
        if pages:
            return pages.pop(0)
        raise ArxivRateGated(1.0)

    service = _service()
    service.BATCH_SIZE = 2
    monkeypatch.setattr(service, "_make_async_request", request)

    papers = await service.search_papers(
        query="all:x", max_results=5, max_gate_wait_s=1.0
    )

    assert [p["id"] for p in papers] == ["2401.00001v1", "2401.00002v1"]
