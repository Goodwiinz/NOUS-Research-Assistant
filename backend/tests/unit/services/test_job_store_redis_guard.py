"""Regression tests for atomic L2 terminal/freshness guarding.

``_redis_write_if_newer`` uses one Lua eval to compare status/freshness and
publish atomically, so a delayed writer cannot stomp a durable terminal winner.
"""

from __future__ import annotations

import json

import pytest

from src.shared.enums import JobStatus


class _FakeRedis:
    """Minimal atomic-eval Redis stub backing a single job key."""

    def __init__(self, existing=None):
        self.value = json.dumps(existing) if existing is not None else None
        self.eval_calls = []

    async def eval(self, script, numkeys, key, payload, ttl, authorized, correction):
        self.eval_calls.append(
            (script, numkeys, key, payload, ttl, authorized, correction)
        )
        candidate = json.loads(payload)
        existing = json.loads(self.value) if self.value is not None else None
        terminal = {"completed", "failed", "cancelled"}
        if candidate.get("status") in terminal and candidate["status"] != authorized:
            return 0
        if candidate.get("status") == "stopping" and authorized != "stopping":
            return 0
        if existing is not None:
            old_status = existing.get("status")
            if old_status in terminal:
                if candidate.get("status") != old_status and not (
                    correction == "1" and candidate.get("status") in terminal
                ):
                    return 0
            elif old_status == "stopping":
                if candidate.get("status") not in terminal | {"stopping"}:
                    return 0
            elif (candidate.get("created_at", 0), candidate.get("_seq", 0)) < (
                existing.get("created_at", 0),
                existing.get("_seq", 0),
            ):
                return 0
        self.value = json.dumps(candidate)
        return 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_skipped_when_existing_is_newer():
    """A stale (older) write must not overwrite a newer stored state."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing={"status": "completed", "created_at": 100.0})
    await _redis_write_if_newer(
        redis, "job-1", {"status": "running", "created_at": 50.0}
    )
    assert len(redis.eval_calls) == 1
    assert redis.value == json.dumps({"status": "completed", "created_at": 100.0})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_applied_when_newer():
    """A newer write proceeds and replaces the older stored state."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing={"status": "running", "created_at": 50.0})
    await _redis_write_if_newer(
        redis,
        "job-1",
        {"status": "completed", "created_at": 100.0},
        authorized_status=JobStatus.COMPLETED,
    )
    assert len(redis.eval_calls) == 1
    assert json.loads(redis.value)["status"] == "completed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_write_applied_when_absent():
    """First write for a job (no existing value) proceeds."""
    from src.services.agent.job_store import _redis_write_if_newer

    redis = _FakeRedis(existing=None)
    await _redis_write_if_newer(
        redis, "job-1", {"status": "running", "created_at": 1.0}
    )
    assert len(redis.eval_calls) == 1
    assert json.loads(redis.value)["status"] == "running"
