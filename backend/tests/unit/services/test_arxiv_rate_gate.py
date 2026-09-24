"""Unit coverage for the fail-closed, exclusive arXiv request gate.

The gate owns a Redis lease for the whole HTTP request. This pins the three
properties the legacy timestamp reservation did not provide: one in-flight
request across pods, a three-second gap between every attempt, and no
unreserved request when Redis or the bounded queue is unavailable.
"""

from __future__ import annotations

import pytest

from src.services.arxiv import arxiv_service as svc


class _FakeRedis:
    def __init__(self, *, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    def _expire(self, key: str) -> None:
        expires_at = self.expiry.get(key)
        if expires_at is not None and expires_at <= self.now_ms:
            self.values.pop(key, None)
            self.expiry.pop(key, None)

    async def set(self, key, value, *, nx=False, px=None):
        self._expire(key)
        if nx and key in self.values:
            return False
        self.values[key] = str(value)
        if px is not None:
            self.expiry[key] = self.now_ms + int(px)
        return True

    async def get(self, key):
        self._expire(key)
        return self.values.get(key)

    async def pttl(self, key):
        self._expire(key)
        if key not in self.values:
            return -2
        if key not in self.expiry:
            return -1
        return max(0, self.expiry[key] - self.now_ms)

    async def time(self):
        return self.now_ms // 1000, (self.now_ms % 1000) * 1000

    async def eval(self, _script, _numkeys, key, token):
        if self.values.get(key) == token:
            self.values.pop(key, None)
            self.expiry.pop(key, None)
            return 1
        return 0


class _RaisingRedis:
    async def set(self, *_a, **_k):
        raise RuntimeError("redis down")


class _BrokenClockRedis(_FakeRedis):
    async def time(self):
        raise RuntimeError("redis clock unavailable")


def _reset(monkeypatch, redis_obj) -> None:
    monkeypatch.setattr(svc, "_arxiv_gate_redis", redis_obj, raising=False)
    monkeypatch.setattr(svc, "_arxiv_gate_disabled", False, raising=False)


async def test_no_redis_url_fails_closed(monkeypatch):
    _reset(monkeypatch, None)
    from src.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "", raising=False)
    with pytest.raises(svc.ArxivCoordinationUnavailableError):
        await svc._acquire_arxiv_rate_slot()


async def test_every_attempt_waits_three_seconds_from_last_start(monkeypatch):
    fake = _FakeRedis()
    _reset(monkeypatch, fake)

    first = await svc._acquire_arxiv_rate_slot()
    assert first.wait_seconds == 0.0
    await svc._mark_arxiv_request_started(first)
    await svc._release_arxiv_rate_slot(first)

    fake.now_ms += 100
    second = await svc._acquire_arxiv_rate_slot()
    assert second.wait_seconds == pytest.approx(2.9)
    await svc._release_arxiv_rate_slot(second)


async def test_second_request_cannot_enter_while_first_is_in_flight(monkeypatch):
    fake = _FakeRedis()
    _reset(monkeypatch, fake)
    first = await svc._acquire_arxiv_rate_slot()

    monkeypatch.setattr(svc, "_ARXIV_MAX_WAIT_MS", 0)
    with pytest.raises(svc.ArxivCoordinationUnavailableError):
        await svc._acquire_arxiv_rate_slot()

    await svc._release_arxiv_rate_slot(first)


async def test_active_406_cooldown_blocks_without_calling_upstream(monkeypatch):
    fake = _FakeRedis()
    _reset(monkeypatch, fake)
    await fake.set(
        svc._ARXIV_406_COOLDOWN_KEY,
        "upstream-unavailable",
        px=svc._ARXIV_406_COOLDOWN_MS,
    )

    with pytest.raises(svc.ArxivUpstreamUnavailableError):
        await svc._acquire_arxiv_rate_slot()

    assert svc._ARXIV_INFLIGHT_KEY not in fake.values


async def test_transient_redis_error_fails_closed_without_disabling(monkeypatch):
    _reset(monkeypatch, _RaisingRedis())
    with pytest.raises(svc.ArxivCoordinationUnavailableError):
        await svc._acquire_arxiv_rate_slot()
    assert svc._arxiv_gate_disabled is False


async def test_spacing_read_error_is_reported_as_coordination_unavailable(monkeypatch):
    fake = _BrokenClockRedis()
    _reset(monkeypatch, fake)

    with pytest.raises(svc.ArxivCoordinationUnavailableError):
        await svc._acquire_arxiv_rate_slot()

    assert svc._ARXIV_INFLIGHT_KEY not in fake.values
