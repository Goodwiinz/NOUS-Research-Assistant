"""Transient arXiv full-text pages: cached once, paginated, never persisted."""

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


class CountingFetch:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def __call__(self, arxiv_id: str) -> str:
        self.calls += 1
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
    assert redis.ttls["arxiv:fulltext:2401.00001"] == arxiv_fulltext.TTL_S
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


async def test_limit_is_clamped_to_one_page() -> None:
    redis = FakeRedis()
    page = await arxiv_fulltext.get_page(
        "hep-th/9901001v2",
        offset=0,
        limit=10**9,
        redis=redis,
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


async def test_fetch_text_raises_lookup_error_when_no_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Svc:
        async def __aenter__(self) -> "Svc":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def download_paper_pdf(self, _id: str) -> bytes | None:
            return None

    import src.services.arxiv.arxiv_service as mod

    monkeypatch.setattr(mod, "ArXivIngestionService", Svc)
    with pytest.raises(LookupError):
        await arxiv_fulltext.fetch_text("2401.00001")
