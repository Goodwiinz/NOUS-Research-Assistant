"""Regression tests for atomic L2 terminal/freshness guarding.

``_redis_write_if_newer`` uses WATCH/MULTI to compare status/freshness and
publish atomically, so a delayed writer cannot stomp a durable terminal winner.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from redis.exceptions import WatchError

from src.shared.enums import JobStatus


class _FakeRedis:
    """Minimal WATCH/MULTI Redis stub backing a single job key."""

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.value = json.dumps(existing) if existing is not None else None
        self.version = 0
        self.pipeline_calls: list[_FakePipeline] = []

    def pipeline(self, *, transaction: bool) -> _FakePipeline:
        assert transaction
        pipeline = _FakePipeline(self)
        self.pipeline_calls.append(pipeline)
        return pipeline


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.watched_version: int | None = None
        self.queued_value: str | None = None

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.reset()

    async def watch(self, _key: str) -> None:
        self.watched_version = self.redis.version

    async def get(self, _key: str) -> str | None:
        return self.redis.value

    def multi(self) -> None:
        return None

    def setex(self, _key: str, _ttl: int, value: str) -> _FakePipeline:
        self.queued_value = value
        return self

    async def execute(self) -> None:
        if self.watched_version != self.redis.version:
            raise WatchError
        if self.queued_value is not None:
            self.redis.value = self.queued_value
            self.redis.version += 1
        self.queued_value = None

    async def reset(self) -> None:
        self.watched_version = None
        self.queued_value = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_skipped_when_existing_is_newer() -> None:
    """A stale (older) write must not overwrite a newer stored state."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing={"status": "completed", "created_at": 100.0})
    await _redis_write_if_newer(
        redis, "job-1", {"status": "running", "created_at": 50.0}
    )
    assert len(redis.pipeline_calls) == 1
    assert redis.value == json.dumps({"status": "completed", "created_at": 100.0})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_applied_when_newer() -> None:
    """A newer write proceeds and replaces the older stored state."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing={"status": "running", "created_at": 50.0})
    await _redis_write_if_newer(
        redis,
        "job-1",
        {"status": "completed", "created_at": 100.0},
        authorized_status=JobStatus.COMPLETED,
    )
    assert len(redis.pipeline_calls) == 1
    assert json.loads(redis.value)["status"] == "completed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_applied_when_absent() -> None:
    """First write for a job (no existing value) proceeds."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing=None)
    await _redis_write_if_newer(
        redis, "job-1", {"status": "running", "created_at": 1.0}
    )
    assert len(redis.pipeline_calls) == 1
    assert json.loads(redis.value)["status"] == "running"


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED.value, JobStatus.STOPPING.value]
)
@pytest.mark.asyncio
async def test_redis_write_with_unknown_status_cannot_replace_absorbing_winner(
    status: str,
) -> None:
    """An unknown status keeps the old Lua refusal for terminal/STOPPING keys."""
    from src.services.agent.job_store import _redis_write_if_newer

    existing = {"status": status, "created_at": 1.0, "_seq": 1}
    redis = _FakeRedis(existing=existing)
    await _redis_write_if_newer(
        redis,
        "job-unknown-status",
        {"created_at": 2.0, "_seq": 2},
    )
    assert json.loads(redis.value or "null") == existing
