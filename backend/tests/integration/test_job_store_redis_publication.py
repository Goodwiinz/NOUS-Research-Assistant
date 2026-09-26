"""Real-Redis tests for guarded job publication and polling payload shape."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from src.shared.enums import JobStatus

if TYPE_CHECKING:
    from src.services.agent.agent_run_service import RunStatusDecision

pytestmark = [pytest.mark.integration, pytest.mark.requires_redis]


async def _real_redis() -> Any:
    import redis.asyncio as redis_asyncio

    redis_url = os.getenv("ORCHESTRATION_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("ORCHESTRATION_TEST_REDIS_URL is not configured")
    redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
    await redis_client.ping()
    return redis_client


def _clear_l1(job_id: str) -> None:
    from src.services.agent import job_store

    with job_store._l1_lock:
        job_store._l1.pop(job_id, None)


def _job_payload(
    job_id: str,
    *,
    status: JobStatus,
    created_at: float,
    sequence: int,
    user_id: str,
    organization_id: str,
    thread_id: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status.value,
        "user_id": user_id,
        "organization_id": organization_id,
        "thread_id": thread_id,
        "created_at": created_at,
        "_seq": sequence,
        "tool_executions": [],
    }
    if result is not None:
        payload["result"] = result
    return payload


def _durable_decision(
    job_id: str,
    *,
    status: JobStatus,
    user_id: str,
    organization_id: str,
    thread_id: str,
) -> RunStatusDecision:
    from src.services.agent.agent_run_service import RunStatusDecision

    return RunStatusDecision(
        job_id=job_id,
        requested_status=status,
        effective_status=status,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        error=None,
        cancel_requested_at=None,
        updated_at="2026-09-26T00:00:00+00:00",
    )


class _PausedPipeline:
    """Pause after WATCH/GET so another process can publish a winner."""

    def __init__(
        self,
        pipeline: Any,
        *,
        key: str,
        reached: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._pipeline = pipeline
        self._key = key
        self._reached = reached
        self._release = release
        self._paused = False

    async def __aenter__(self) -> _PausedPipeline:
        await self._pipeline.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> Any:
        return await self._pipeline.__aexit__(*exc_info)

    async def watch(self, *keys: str) -> Any:
        return await self._pipeline.watch(*keys)

    async def get(self, key: str) -> Any:
        value = await self._pipeline.get(key)
        if key == self._key and not self._paused:
            self._paused = True
            self._reached.set()
            await self._release.wait()
        return value

    def multi(self) -> Any:
        return self._pipeline.multi()

    def setex(self, *args: Any, **kwargs: Any) -> Any:
        return self._pipeline.setex(*args, **kwargs)

    async def execute(self) -> Any:
        return await self._pipeline.execute()

    async def reset(self) -> Any:
        return await self._pipeline.reset()


class _PauseCompletedPublication:
    """Redis proxy pausing a candidate between read and atomic publication."""

    def __init__(self, redis_client: Any, key: str) -> None:
        self._redis = redis_client
        self._key = key
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def pipeline(self, *args: Any, **kwargs: Any) -> _PausedPipeline:
        return _PausedPipeline(
            self._redis.pipeline(*args, **kwargs),
            key=self._key,
            reached=self.reached,
            release=self.release,
        )


@pytest.mark.asyncio
async def test_cold_l1_job_poll_preserves_nested_json_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A polling API process receives JSON arrays and objects in their shape."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    result = {
        "message": {"content": "waiting", "segments": []},
        "tool_executions": [],
        "contexts": [],
        "metadata": {},
        "nested": [[], {}],
    }
    payload = _job_payload(
        job_id,
        status=JobStatus.RUNNING,
        created_at=time.time(),
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=result,
    )
    payload["request"] = {
        "messages": [],
        "page_context": {},
        "arguments": [{"items": []}, {}],
    }
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=redis_client))
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await job_store.set_job_redis_only(job_id, payload)
        # The route's nonterminal path must fetch from Redis with no local seed.
        _clear_l1(job_id)
        monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
        response = await execute.get_job_status(
            job_id,
            cast(
                User,
                SimpleNamespace(id=user_id, organization_id=organization_id),
            ),
        )
        assert response.status == JobStatus.RUNNING
        assert response.tool_executions == []
        assert response.result == result
        raw = await redis_client.get(redis_key)
        assert raw is not None
        stored = json.loads(raw)
        assert stored["tool_executions"] == []
        assert stored["request"] == payload["request"]
        assert stored["result"] == result
    finally:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_repeated_completed_publication_preserves_winner_payload_and_enriches_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Redis client cannot replace a completed answer or JSON shape."""
    from src.services.agent import job_store

    first_client = await _real_redis()
    second_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    winning_result = {
        "message": "first completed answer",
        "tool_executions": [],
        "contexts": [],
        "metadata": {},
        "nested": [[], {}],
    }
    original = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=100.0,
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=winning_result,
    )
    later = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=101.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result={"message": "different later answer", "tool_executions": []},
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=first_client))
    try:
        await first_client.delete(redis_key)
        _clear_l1(job_id)
        await first_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(original)
        )
        monkeypatch.setattr(
            job_store, "_get_redis", AsyncMock(return_value=second_client)
        )
        await job_store.set_job_redis_only(job_id, later, decision=decision)
        raw = await first_client.get(redis_key)
        assert raw is not None
        stored = json.loads(raw)
        assert stored["result"] == winning_result
        assert stored["tool_executions"] == []
        assert stored["user_id"] == user_id
        assert stored["organization_id"] == organization_id
        assert stored["thread_id"] == thread_id
    finally:
        await first_client.delete(redis_key)
        _clear_l1(job_id)
        await first_client.aclose()
        await second_client.aclose()


@pytest.mark.asyncio
async def test_stale_completed_publication_keeps_concurrent_redis_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WATCH retry re-reads and retains the completed winner's result."""
    from src.services.agent import job_store

    winner_client = await _real_redis()
    delayed_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    prior = _job_payload(
        job_id,
        status=JobStatus.RUNNING,
        created_at=1.0,
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    winner_result = {"message": "winner", "tool_executions": [], "contexts": []}
    winner = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=2.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=winner_result,
    )
    stale_result = {"message": "stale different result", "tool_executions": []}
    stale = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=3.0,
        sequence=3,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=stale_result,
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    delayed = _PauseCompletedPublication(delayed_client, redis_key)
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=delayed))
    try:
        await winner_client.delete(redis_key)
        _clear_l1(job_id)
        await winner_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(prior)
        )
        stale_write = asyncio.create_task(
            job_store.set_job_redis_only(job_id, stale, decision=decision)
        )
        await asyncio.wait_for(delayed.reached.wait(), timeout=3)
        await job_store._redis_write_if_newer(
            winner_client,
            job_id,
            winner,
            authorized_status=JobStatus.COMPLETED,
            allow_terminal_correction=True,
        )
        delayed.release.set()
        await asyncio.wait_for(stale_write, timeout=3)
        raw = await winner_client.get(redis_key)
        assert raw is not None
        stored = json.loads(raw)
        assert stored["status"] == JobStatus.COMPLETED.value
        assert stored["result"] == winner_result
    finally:
        delayed.release.set()
        await winner_client.delete(redis_key)
        _clear_l1(job_id)
        await winner_client.aclose()
        await delayed_client.aclose()


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.STOPPING])
@pytest.mark.asyncio
async def test_real_redis_unknown_status_cannot_replace_absorbing_winner(
    status: JobStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis preserves Lua's refusal when a candidate status is absent."""
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    existing = {
        "status": status.value,
        "created_at": 1.0,
        "_seq": 1,
        "result": {"message": "terminal winner"},
    }
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=redis_client))
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(existing)
        )
        await job_store.set_job_redis_only(job_id, {"created_at": 2.0, "_seq": 2})
        raw = await redis_client.get(redis_key)
        assert raw is not None
        assert json.loads(raw) == existing
    finally:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.parametrize("status", [JobStatus.CANCELLED, JobStatus.FAILED])
@pytest.mark.asyncio
async def test_non_success_terminal_publication_strips_result_and_confirmation(
    status: JobStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation and failure projections never publish success fields."""
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    candidate = _job_payload(
        job_id,
        status=status,
        created_at=10.0,
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result={"message": "must not leak", "tool_executions": []},
    )
    candidate["confirmation"] = {"action": "must not leak"}
    decision = _durable_decision(
        job_id=job_id,
        status=status,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=redis_client))
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await job_store.set_job_redis_only(job_id, candidate, decision=decision)
        raw = await redis_client.get(redis_key)
        assert raw is not None
        stored = json.loads(raw)
        assert stored["status"] == status.value
        assert "result" not in stored
        assert "confirmation" not in stored
    finally:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()
