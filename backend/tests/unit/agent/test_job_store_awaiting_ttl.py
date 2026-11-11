"""R2-H3: a HITL run parked in awaiting_confirmation must outlive the 1h TTL.

Every job record used to expire after 1h, so a user who answered a
confirmation prompt later than that hit a 409 on /confirm. Awaiting records
now live for 24h in Redis and in the in-memory L1; every other status keeps
the 1h TTL, and the stale-awaiting sweeper stays longer than the 24h window.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest

from src.core.config import get_settings
from src.services.agent import job_store as js
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

_HOUR = 3600


@pytest.fixture(autouse=True)
def _clean_store() -> Iterator[None]:
    js._l1.clear()
    js._pending_terminal_publications.clear()
    yield
    js._l1.clear()
    js._pending_terminal_publications.clear()


class _FakePipeline:
    """Minimal WATCH/MULTI pipeline that records the SETEX it is asked for."""

    def __init__(self, store: dict[str, str], ttls: dict[str, int]) -> None:
        self._store = store
        self._ttls = ttls
        self.setex_calls: list[tuple[str, int, str]] = []

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def watch(self, _key: str) -> None:
        return None

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2
        return self._ttls.get(key, -1)

    async def reset(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))

    async def execute(self) -> None:
        for key, ttl, value in self.setex_calls:
            self._store[key] = value
            self._ttls[key] = ttl


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        pipe = _FakePipeline(self.store, self.ttls)
        self.pipelines.append(pipe)
        return pipe

    def last_ttl(self) -> int:
        return self.pipelines[-1].setex_calls[-1][1]


# ---------------------------------------------------------------------------
# TTL policy
# ---------------------------------------------------------------------------


def test_awaiting_ttl_is_a_day_and_other_statuses_keep_an_hour() -> None:
    assert js._AWAITING_JOB_TTL_SECONDS == 24 * _HOUR
    assert js._JOB_TTL_SECONDS == _HOUR
    assert js._job_ttl_seconds(JobStatus.AWAITING_CONFIRMATION) == 24 * _HOUR
    assert js._job_ttl_seconds("awaiting_confirmation") == 24 * _HOUR
    for status in (
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ):
        assert js._job_ttl_seconds(status) == _HOUR
    assert js._job_ttl_seconds(None) == _HOUR
    assert js._job_ttl_seconds("not-a-status") == _HOUR


def test_stale_awaiting_sweeper_outlives_awaiting_job_ttl() -> None:
    """The sweeper must not fail a run whose confirm can still succeed."""
    settings = get_settings()
    assert (
        settings.AGENT_RUN_STALE_AWAITING_AFTER_SECONDS > js._AWAITING_JOB_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# Redis write paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_write_uses_awaiting_ttl_for_parked_job() -> None:
    redis = _FakeRedis()
    outcome = await js._redis_write_if_newer(
        redis,
        "job-await",
        {"status": JobStatus.AWAITING_CONFIRMATION, "created_at": time.time()},
    )
    assert outcome.state == "committed"
    assert redis.last_ttl() == 24 * _HOUR


@pytest.mark.asyncio
async def test_redis_write_keeps_one_hour_ttl_for_running_job() -> None:
    redis = _FakeRedis()
    outcome = await js._redis_write_if_newer(
        redis,
        "job-run",
        {"status": JobStatus.RUNNING, "created_at": time.time()},
    )
    assert outcome.state == "committed"
    assert redis.last_ttl() == _HOUR


@pytest.mark.asyncio
async def test_cas_into_awaiting_restarts_full_hitl_window() -> None:
    redis = _FakeRedis()
    key = f"{js._JOB_KEY_PREFIX}job-cas-await"
    redis.store[key] = json.dumps({"status": "running"})
    redis.ttls[key] = 600
    with patch.object(js, "_get_redis", AsyncMock(return_value=redis)):
        result = await js.compare_and_set_status(
            "job-cas-await",
            JobStatus.RUNNING,
            JobStatus.AWAITING_CONFIRMATION,
            project=False,
        )
    assert result == "claimed"
    assert redis.last_ttl() == 24 * _HOUR


@pytest.mark.asyncio
async def test_cas_out_of_awaiting_caps_ttl_at_one_hour() -> None:
    redis = _FakeRedis()
    key = f"{js._JOB_KEY_PREFIX}job-cas-run"
    redis.store[key] = json.dumps({"status": "awaiting_confirmation"})
    redis.ttls[key] = 20 * _HOUR
    with patch.object(js, "_get_redis", AsyncMock(return_value=redis)):
        result = await js.compare_and_set_status(
            "job-cas-run",
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        )
    assert result == "claimed"
    assert redis.last_ttl() == _HOUR


@pytest.mark.asyncio
async def test_cas_preserves_shorter_remaining_ttl() -> None:
    redis = _FakeRedis()
    key = f"{js._JOB_KEY_PREFIX}job-cas-short"
    redis.store[key] = json.dumps({"status": "awaiting_confirmation"})
    redis.ttls[key] = 120
    with patch.object(js, "_get_redis", AsyncMock(return_value=redis)):
        await js.compare_and_set_status(
            "job-cas-short",
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        )
    assert redis.last_ttl() == 120


@pytest.mark.asyncio
async def test_cas_without_ttl_falls_back_to_status_ttl() -> None:
    redis = _FakeRedis()
    key = f"{js._JOB_KEY_PREFIX}job-cas-nottl"
    redis.store[key] = json.dumps({"status": "awaiting_confirmation"})
    with patch.object(js, "_get_redis", AsyncMock(return_value=redis)):
        await js.compare_and_set_status(
            "job-cas-nottl",
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        )
    assert redis.last_ttl() == _HOUR


# ---------------------------------------------------------------------------
# In-memory L1 expiry (Redis unavailable)
# ---------------------------------------------------------------------------


def _seed(job_id: str, status: JobStatus, age_seconds: float) -> None:
    js._l1[job_id] = {"status": status, "created_at": time.time() - age_seconds}


@pytest.mark.asyncio
@pytest.mark.parametrize("age_hours", [1.5, 23.5])
async def test_awaiting_job_readable_after_one_hour_and_before_a_day(
    age_hours: float,
) -> None:
    _seed("job-await", JobStatus.AWAITING_CONFIRMATION, age_hours * _HOUR)
    with patch.object(js, "_get_redis", AsyncMock(return_value=None)):
        got = await js.get_job("job-await")
        assert got is not None
        assert got["status"] == JobStatus.AWAITING_CONFIRMATION
        assert await js.get_job_fresh("job-await") is not None
        assert await js.get_job_for_poll("job-await") is not None


@pytest.mark.asyncio
async def test_awaiting_job_expires_after_a_day() -> None:
    with patch.object(js, "_get_redis", AsyncMock(return_value=None)):
        _seed("a1", JobStatus.AWAITING_CONFIRMATION, 24.5 * _HOUR)
        assert await js.get_job("a1") is None
        _seed("a2", JobStatus.AWAITING_CONFIRMATION, 24.5 * _HOUR)
        assert await js.get_job_fresh("a2") is None
        _seed("a3", JobStatus.AWAITING_CONFIRMATION, 24.5 * _HOUR)
        assert await js.get_job_for_poll("a3") is None


@pytest.mark.asyncio
async def test_running_job_still_expires_at_one_hour() -> None:
    with patch.object(js, "_get_redis", AsyncMock(return_value=None)):
        _seed("r1", JobStatus.RUNNING, 1.5 * _HOUR)
        assert await js.get_job("r1") is None
        _seed("r2", JobStatus.RUNNING, 1.5 * _HOUR)
        assert await js.get_job_fresh("r2") is None
        _seed("r3", JobStatus.RUNNING, 1.5 * _HOUR)
        assert await js.get_job_for_poll("r3") is None
        _seed("r4", JobStatus.RUNNING, 0.5 * _HOUR)
        assert await js.get_job("r4") is not None


def test_l1_cleanup_keeps_parked_job_and_drops_old_running_job() -> None:
    _seed("parked", JobStatus.AWAITING_CONFIRMATION, 2 * _HOUR)
    _seed("stale-running", JobStatus.RUNNING, 2 * _HOUR)
    _seed("stale-parked", JobStatus.AWAITING_CONFIRMATION, 25 * _HOUR)
    with js._l1_lock:
        js._l1_cleanup()
    assert "parked" in js._l1
    assert "stale-running" not in js._l1
    assert "stale-parked" not in js._l1
