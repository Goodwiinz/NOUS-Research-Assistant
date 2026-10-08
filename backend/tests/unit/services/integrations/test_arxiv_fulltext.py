"""Transient arXiv full-text pages: cached once, paginated, never persisted."""

import asyncio
import gc
import json
import multiprocessing
import os
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pytest

from src.services.integrations import arxiv_fulltext

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttls[key] = ex or 0

    def age(self, key: str, seconds: float) -> None:
        entry = json.loads(self.store[key])
        entry["fetched_at"] = time.time() - seconds
        self.store[key] = json.dumps(entry)

    def end_retry_window(self, key: str) -> None:
        entry = json.loads(self.store[key])
        entry["retry_after"] = 0
        self.store[key] = json.dumps(entry)


class DownRedis:
    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis down")

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise ConnectionError("redis down")


class CountingFetch:
    def __init__(self, text: str, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0

    async def __call__(self, arxiv_id: str) -> str:
        self.calls += 1
        if self.fail:
            raise LookupError("arxiv_unavailable")
        return self.text


async def test_first_call_downloads_and_caches_then_pages_without_refetch() -> None:
    redis = FakeRedis()
    fetch = CountingFetch("x" * 100_000)
    first = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=48_000, redis=redis, fetch=fetch
    )
    assert fetch.calls == 1
    assert len(first["text"]) == 48_000
    assert first["next_offset"] == 48_000
    assert first["total_chars"] == 100_000
    assert redis.ttls["arxiv:fulltext:2401.00001"] == arxiv_fulltext.STALE_TTL_S
    second = await arxiv_fulltext.get_page(
        "2401.00001", offset=48_000, limit=48_000, redis=redis, fetch=fetch
    )
    assert fetch.calls == 1
    assert second["next_offset"] == 96_000
    last = await arxiv_fulltext.get_page(
        "2401.00001", offset=96_000, limit=48_000, redis=redis, fetch=fetch
    )
    assert last["text"] == "x" * 4_000
    assert last["next_offset"] is None


async def test_unreachable_cache_is_a_miss_not_a_failure() -> None:
    fetch = CountingFetch("y" * 10)
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=4, redis=DownRedis(), fetch=fetch
    )
    assert page["text"] == "yyyy" and fetch.calls == 1


async def test_stale_entry_is_refreshed_or_served_when_refetch_fails() -> None:
    redis = FakeRedis()
    key = "arxiv:fulltext/2401.00001".replace("/", ":")
    await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=CountingFetch("old")
    )
    redis.age(key, arxiv_fulltext.TTL_S + 60)
    failing = CountingFetch("", fail=True)
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=failing
    )
    assert failing.calls == 1 and page["text"] == "old"
    redis.end_retry_window(key)  # once the retry window ends, a call refreshes
    fresh = CountingFetch("new")
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=fresh
    )
    assert fresh.calls == 1 and page["text"] == "new"
    assert json.loads(redis.store[key])["fetched_at"] > time.time() - 5
    again = CountingFetch("newer")
    await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=again
    )
    assert again.calls == 0


async def test_failed_refresh_serves_stale_text_without_refetching_for_a_while() -> (
    None
):
    redis = FakeRedis()
    key = "arxiv:fulltext:2401.00001"
    await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=CountingFetch("old")
    )
    redis.age(key, arxiv_fulltext.TTL_S + 60)
    failing = CountingFetch("", fail=True)
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=failing
    )
    assert failing.calls == 1 and page["text"] == "old"
    # Within RETRY_AFTER_S the stale text comes back at once: a hanging arXiv
    # must not cost every call the whole refetch budget (RT-2).
    again = CountingFetch("", fail=True)
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=again
    )
    assert again.calls == 0 and page["text"] == "old"
    # Still counted from the real fetch: re-storing never extends the 7 days.
    assert json.loads(redis.store[key])["fetched_at"] < (
        time.time() - arxiv_fulltext.TTL_S
    )
    assert redis.ttls[key] <= arxiv_fulltext.STALE_TTL_S - arxiv_fulltext.TTL_S


async def test_limit_is_clamped_to_one_page() -> None:
    page = await arxiv_fulltext.get_page(
        "hep-th/9901001v2",
        offset=0,
        limit=10**9,
        redis=FakeRedis(),
        fetch=CountingFetch("y" * 50_000),
    )
    assert len(page["text"]) == arxiv_fulltext.MAX_PAGE_CHARS


@pytest.mark.parametrize("bad", ["../etc", "2401.00001; rm", "", "abs/2401.00001"])
async def test_invalid_id_is_rejected_before_any_fetch(bad: str) -> None:
    fetch = CountingFetch("z")
    with pytest.raises(ValueError):
        await arxiv_fulltext.get_page(
            bad, offset=0, limit=10, redis=FakeRedis(), fetch=fetch
        )
    assert fetch.calls == 0


class _FakeService:
    """Mimics ArXivIngestionService: writes the PDF under its download dir."""

    events: list[str] = []
    dirs: list[Path] = []
    _last_request_time: float = 0.0

    def __init__(self, config: dict[str, Any]) -> None:
        self.download_dir = Path(config["arxiv_download_dir"])
        _FakeService.dirs.append(self.download_dir)

    async def __aenter__(self) -> "_FakeService":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def download_paper_pdf(self, paper_id: str) -> bytes | None:
        _FakeService.events.append("download")
        (self.download_dir / f"{paper_id}.pdf").write_bytes(b"%PDF")
        return b"%PDF"


async def test_fetch_text_uses_rate_slot_and_leaves_no_pdf_behind(
    monkeypatch: pytest.MonkeyPatch, thread_pool: None
) -> None:
    import src.services.arxiv.arxiv_service as mod

    _FakeService.events.clear()
    _FakeService.dirs.clear()

    async def slot() -> float | None:
        _FakeService.events.append("slot")
        return 0.0

    monkeypatch.setattr(mod, "ArXivIngestionService", _FakeService)
    monkeypatch.setattr(mod, "_acquire_arxiv_rate_slot", slot)
    monkeypatch.setattr(arxiv_fulltext, "_extract", _fake_extract)
    assert await arxiv_fulltext.fetch_text("2401.00001") == "body"
    assert _FakeService.events == ["slot", "download"]
    [tmp] = _FakeService.dirs
    assert not os.path.exists(tmp)
    assert not (Path.cwd() / "data" / "arxiv" / "2401.00001.pdf").exists()


async def test_fetch_text_raises_lookup_error_when_no_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Svc(_FakeService):
        async def download_paper_pdf(self, _id: str) -> bytes | None:
            return None

    import src.services.arxiv.arxiv_service as mod

    async def slot() -> float | None:
        return None

    monkeypatch.setattr(mod, "ArXivIngestionService", Svc)
    monkeypatch.setattr(mod, "_acquire_arxiv_rate_slot", slot)
    with pytest.raises(LookupError):
        await arxiv_fulltext.fetch_text("2401.00001")


async def test_legacy_plain_string_cache_value_is_a_miss() -> None:
    redis = FakeRedis()
    redis.store["arxiv:fulltext:2401.00001"] = "plain old text"
    fetch = CountingFetch("fresh")
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=fetch
    )
    assert fetch.calls == 1 and page["text"] == "fresh"
    assert json.loads(redis.store["arxiv:fulltext:2401.00001"])["text"] == "fresh"


async def test_empty_extraction_is_not_cached() -> None:
    redis = FakeRedis()
    await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=CountingFetch("")
    )
    assert redis.store == {}


def _fake_extract(_path: str) -> str:
    return "body"


def _slow_extract(_path: str) -> str:
    time.sleep(5)
    return "late"


@pytest.fixture
def thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # A thread pool stands in for the process pool so monkeypatched functions
    # are visible to the worker (spawned processes would not see them).
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        arxiv_fulltext, "_executor_factory", lambda: ThreadPoolExecutor(1)
    )


async def test_timed_out_extraction_does_not_poison_the_next_call(
    monkeypatch: pytest.MonkeyPatch, thread_pool: None
) -> None:
    import src.services.arxiv.arxiv_service as mod

    async def slot() -> float | None:
        return 0.0

    monkeypatch.setattr(mod, "ArXivIngestionService", _FakeService)
    monkeypatch.setattr(mod, "_acquire_arxiv_rate_slot", slot)
    monkeypatch.setattr(arxiv_fulltext, "_extract", _slow_extract)
    with pytest.raises(asyncio.TimeoutError):
        await arxiv_fulltext.get_page(
            "2401.00001", offset=0, limit=10, redis=FakeRedis(), budget=0.05
        )
    monkeypatch.setattr(arxiv_fulltext, "_extract", _fake_extract)
    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=FakeRedis(), budget=5
    )
    assert page["text"] == "body"


async def test_no_shared_gate_falls_back_to_per_process_spacing(
    monkeypatch: pytest.MonkeyPatch, thread_pool: None
) -> None:
    import src.services.arxiv.arxiv_service as mod

    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    async def slot() -> float | None:
        return None

    monkeypatch.setattr(mod, "ArXivIngestionService", _FakeService)
    monkeypatch.setattr(mod, "_acquire_arxiv_rate_slot", slot)
    monkeypatch.setattr(
        mod.ArXivIngestionService, "_last_request_time", time.monotonic()
    )
    monkeypatch.setattr(arxiv_fulltext, "_sleep", record)
    monkeypatch.setattr(arxiv_fulltext, "_extract", _fake_extract)
    assert await arxiv_fulltext.fetch_text("2401.00001") == "body"
    assert len(sleeps) == 1 and 0 < sleeps[0] <= arxiv_fulltext._PER_PROCESS_GAP_S


async def test_stale_entry_is_served_when_refresh_times_out() -> None:
    import asyncio

    redis = FakeRedis()
    key = "arxiv:fulltext:2401.00001"
    await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=CountingFetch("old")
    )
    redis.age(key, arxiv_fulltext.TTL_S + 60)

    async def hang(_id: str) -> str:
        await asyncio.sleep(5)
        return "never"

    page = await arxiv_fulltext.get_page(
        "2401.00001", offset=0, limit=10, redis=redis, fetch=hang, budget=0.05
    )
    assert page["text"] == "old"


# --- RT-3: real worker processes ---------------------------------------------
#
# Builtins pickle by reference under fork (Linux, production) and spawn
# (macOS), so these workers need no importable test module. The thread_pool
# fixture cannot stand in here: a thread can be neither killed nor broken.


async def _new_children_alive(before: set[int | None]) -> list[Any]:
    """Child processes started after ``before`` still alive after up to 5 s."""
    deadline = time.monotonic() + 5
    alive = [p for p in multiprocessing.active_children() if p.pid not in before]
    while alive and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        alive = [p for p in multiprocessing.active_children() if p.pid not in before]
    return alive


async def test_timed_out_parse_is_killed_with_its_call() -> None:
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            arxiv_fulltext._run_in_subprocess(time.sleep, 10), timeout=1.0
        )
    assert await _new_children_alive(before) == []


async def test_dead_worker_breaks_only_its_own_call() -> None:
    # A worker that dies mid-parse (an OOM kill) breaks its pool.
    with pytest.raises(BrokenProcessPool):
        await arxiv_fulltext._run_in_subprocess(os._exit, 137)
    assert await arxiv_fulltext._run_in_subprocess(pow, 2, 10) == 1024


async def test_one_callers_timeout_never_cancels_another_callers_parse() -> None:
    slow = asyncio.create_task(
        asyncio.wait_for(arxiv_fulltext._run_in_subprocess(time.sleep, 3), 0.5)
    )
    others = []
    for exponent in (1, 2, 3):
        await asyncio.sleep(0.1)
        others.append(
            asyncio.create_task(arxiv_fulltext._run_in_subprocess(pow, 2, exponent))
        )
    results = await asyncio.gather(slow, *others, return_exceptions=True)
    assert isinstance(results[0], asyncio.TimeoutError)
    # With one shared pool the queued callers got a bare CancelledError.
    assert results[1:] == [2, 4, 8]


async def test_a_third_parse_waits_for_one_of_two_running_parses(
    thread_pool: None,
) -> None:
    # The thread pool stands in so the two running parses can be held open
    # with events; the cap sits in front of the executor either way.
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    ran: list[str] = []

    def hold(i: int) -> str:
        started[i].set()
        release[i].wait(5)
        ran.append(f"held {i}")
        return f"held {i}"

    def quick(name: str) -> str:
        ran.append(name)
        return name

    running = [
        asyncio.create_task(arxiv_fulltext._run_in_subprocess(hold, i)) for i in (0, 1)
    ]
    try:
        for event in started:
            assert await asyncio.to_thread(event.wait, 5)
        # A third caller's own timeout ends only its wait for a slot ...
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                arxiv_fulltext._run_in_subprocess(quick, "timed out"), 0.2
            )
        assert ran == [] and not any(task.done() for task in running)
        # ... and the next caller starts as soon as one running parse ends.
        waiting = asyncio.create_task(
            arxiv_fulltext._run_in_subprocess(quick, "waited")
        )
        await asyncio.sleep(0.1)
        assert not waiting.done()
        release[0].set()
        assert await waiting == "waited"
        assert ran == ["held 0", "waited"] and not running[1].done()
    finally:
        for event in release:
            event.set()
    assert await asyncio.gather(*running) == ["held 0", "held 1"]


def test_kill_workers_prefers_the_public_kill_workers() -> None:
    # ProcessPoolExecutor.kill_workers is public from Python 3.14.
    calls: list[str] = []

    class Worker:
        def kill(self) -> None:
            calls.append("_processes")

    class Pool:
        _processes = {1: Worker()}

        def kill_workers(self) -> None:
            calls.append("kill_workers")

    arxiv_fulltext._kill_workers(Pool())
    assert calls == ["kill_workers"]


async def test_parse_worker_freezes_the_objects_it_starts_with() -> None:
    # Frozen objects are never walked by the worker's collector, so a forked
    # worker does not dirty the copy-on-write pages it inherited.
    assert await arxiv_fulltext._run_in_subprocess(gc.get_freeze_count) > 0
