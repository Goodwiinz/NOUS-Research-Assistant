"""Transient arXiv full-text pages: cached once, paginated, never persisted."""

import json
import os
import time
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

    async def extract_pdf_content(self, _pdf: bytes) -> dict[str, Any]:
        return {"full_text": "body"}


async def test_fetch_text_uses_rate_slot_and_leaves_no_pdf_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.services.arxiv.arxiv_service as mod

    _FakeService.events.clear()
    _FakeService.dirs.clear()

    async def slot() -> float | None:
        _FakeService.events.append("slot")
        return 0.0

    monkeypatch.setattr(mod, "ArXivIngestionService", _FakeService)
    monkeypatch.setattr(mod, "_acquire_arxiv_rate_slot", slot)
    assert await arxiv_fulltext.fetch_text("2401.00001") == "body"
    assert _FakeService.events == ["slot", "download"]
    [tmp] = _FakeService.dirs
    assert not os.path.exists(tmp)
    assert not Path("data/arxiv/2401.00001.pdf").exists()


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
