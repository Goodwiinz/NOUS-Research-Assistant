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
    assert redis_url is not None
    redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
    await redis_client.ping()
    return redis_client


def _clear_l1(job_id: str) -> None:
    from src.services.agent import job_store

    with job_store._l1_lock:
        job_store._l1.pop(job_id, None)
        job_store._retire_pending_publication(job_id)


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


class _FailingPausedPipeline(_PausedPipeline):
    async def execute(self) -> Any:
        await self._release.wait()
        raise OSError("injected Redis EXEC outage")


class _PauseAndFailPublication(_PauseCompletedPublication):
    def pipeline(self, *args: Any, **kwargs: Any) -> _FailingPausedPipeline:
        return _FailingPausedPipeline(
            self._redis.pipeline(*args, **kwargs),
            key=self._key,
            reached=self.reached,
            release=self.release,
        )


class _PauseRedisRead:
    """Pause a polling read after it has captured its Redis snapshot."""

    def __init__(self, redis_client: Any, key: str) -> None:
        self._redis = redis_client
        self._key = key
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self._paused = False

    async def get(self, key: str) -> Any:
        value = await self._redis.get(key)
        if key == self._key and not self._paused:
            self._paused = True
            self.reached.set()
            await self.release.wait()
        return value


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
async def test_normal_completed_publication_and_poll_adopt_redis_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The awaited publisher and terminal poll agree with Redis's result."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    winning_result = {
        "message": "original Redis winner",
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
        result={"message": "later local loser", "tool_executions": []},
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=redis_client))
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(original)
        )

        accepted = await job_store.set_job(
            job_id, later, project=False, decision=decision
        )
        raw = await redis_client.get(redis_key)
        assert raw is not None
        stored = json.loads(raw)
        response = await execute.get_job_status(
            job_id,
            cast(
                User,
                SimpleNamespace(id=user_id, organization_id=organization_id),
            ),
        )
        with job_store._l1_lock:
            local = dict(job_store._l1[job_id])

        assert accepted is decision
        assert stored["result"] == winning_result
        assert response.result == winning_result
        assert response.tool_executions == []
        assert response.result["nested"] == [[], {}]
        assert local["result"] == winning_result
    finally:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.parametrize("local_change", ["replacement", "eviction"])
@pytest.mark.asyncio
async def test_completed_publication_does_not_restore_replaced_or_evicted_l1(
    local_change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older Redis await cannot replace or resurrect a later L1 generation."""
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    winning_result = {
        "message": "original Redis winner",
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
    first = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=101.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result={"message": "older publisher candidate", "tool_executions": []},
    )
    completed_decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    delayed = _PauseCompletedPublication(redis_client, redis_key)
    provider_calls = 0

    async def _provide_redis() -> Any:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return delayed
        return None

    monkeypatch.setattr(job_store, "_get_redis", _provide_redis)
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(original)
        )
        older_publication = asyncio.create_task(
            job_store.set_job(job_id, first, project=False, decision=completed_decision)
        )
        await asyncio.wait_for(delayed.reached.wait(), timeout=3)

        if local_change == "replacement":
            cancelled_decision = _durable_decision(
                job_id=job_id,
                status=JobStatus.CANCELLED,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=thread_id,
            )
            cancelled = _job_payload(
                job_id,
                status=JobStatus.CANCELLED,
                created_at=102.0,
                sequence=3,
                user_id=user_id,
                organization_id=organization_id,
                thread_id=thread_id,
                result={"message": "must be stripped", "tool_executions": []},
            )
            cancelled["confirmation"] = {"action": "must be stripped"}
            accepted = await job_store.set_job(
                job_id, cancelled, project=False, decision=cancelled_decision
            )
            assert accepted is cancelled_decision
            with job_store._l1_lock:
                local = dict(job_store._l1[job_id])
            assert local["status"] == JobStatus.CANCELLED
            assert "result" not in local
            assert "confirmation" not in local
        else:
            monkeypatch.setattr(job_store, "_L1_MAX_ENTRIES", 0)
            with job_store._l1_lock:
                job_store._l1_cleanup()
                assert job_id not in job_store._l1

        delayed.release.set()
        assert (
            await asyncio.wait_for(older_publication, timeout=3) is completed_decision
        )
        raw = await redis_client.get(redis_key)
        assert raw is not None
        assert json.loads(raw)["result"] == winning_result
        with job_store._l1_lock:
            if local_change == "replacement":
                local_after_older_completion = dict(job_store._l1[job_id])
            else:
                assert job_id not in job_store._l1
        if local_change == "replacement":
            assert local_after_older_completion["status"] == JobStatus.CANCELLED
            assert "result" not in local_after_older_completion
    finally:
        delayed.release.set()
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_poll_during_terminal_redis_await_never_sees_losing_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll concurrent with publication returns the current Redis winner."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import agent_execution_service, job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    winning_result = {
        "message": "current Redis winner",
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
        result={"message": "losing candidate during await", "tool_executions": []},
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    delayed = _PauseCompletedPublication(redis_client, redis_key)
    provider_calls = 0

    async def _provide_redis() -> Any:
        nonlocal provider_calls
        provider_calls += 1
        return delayed if provider_calls == 1 else redis_client

    monkeypatch.setattr(job_store, "_get_redis", _provide_redis)
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(agent_execution_service, "_write_to_redis_only", AsyncMock())
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(original)
        )
        publisher = asyncio.create_task(
            job_store.set_job(job_id, later, project=False, decision=decision)
        )
        await asyncio.wait_for(delayed.reached.wait(), timeout=3)
        user = cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        )
        cancelled_poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.sleep(0)
        assert not cancelled_poll.done()
        cancelled_poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_poll
        assert not delayed.release.is_set()

        poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.sleep(0)
        assert not poll.done()

        from src.services.agent.agent_execution_service import _set_job

        _set_job(
            job_id,
            {"status": JobStatus.RUNNING.value, "user_id": user_id},
            project=False,
        )
        with job_store._l1_lock:
            reserved = dict(job_store._l1[job_id])
        assert reserved["status"] == JobStatus.COMPLETED.value
        assert reserved["result"]["message"] == "losing candidate during await"

        delayed.release.set()
        assert await asyncio.wait_for(publisher, timeout=3) is decision
        response = await asyncio.wait_for(poll, timeout=3)

        assert response.result == winning_result
        subsequent = await execute.get_job_status(job_id, user)
        assert subsequent.result == winning_result
        assert provider_calls == 1
    finally:
        delayed.release.set()
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_poll_started_before_publication_rechecks_after_redis_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final poll boundary catches a gate registered during Redis refresh."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    winning_result = {
        "message": "winner resolved during poll refresh",
        "tool_executions": [],
        "contexts": [],
        "metadata": {},
        "nested": [[], {}],
    }
    original = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=101.0,
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=winning_result,
    )
    initial_l1 = _job_payload(
        job_id,
        status=JobStatus.RUNNING,
        created_at=100.0,
        sequence=1,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    later = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=102.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result={"message": "provisional poll loser", "tool_executions": []},
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    paused_read = _PauseRedisRead(redis_client, redis_key)
    delayed_write = _PauseCompletedPublication(redis_client, redis_key)
    provider_calls = 0

    async def _provide_redis() -> Any:
        nonlocal provider_calls
        provider_calls += 1
        return paused_read if provider_calls == 1 else delayed_write

    monkeypatch.setattr(job_store, "_get_redis", _provide_redis)
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        with job_store._l1_lock:
            job_store._l1[job_id] = initial_l1
        await redis_client.setex(
            redis_key, job_store._JOB_TTL_SECONDS, json.dumps(original)
        )
        user = cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        )
        poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.wait_for(paused_read.reached.wait(), timeout=3)

        publisher = asyncio.create_task(
            job_store.set_job(job_id, later, project=False, decision=decision)
        )
        await asyncio.wait_for(delayed_write.reached.wait(), timeout=3)
        paused_read.release.set()
        await asyncio.sleep(0)
        assert not poll.done()
        with job_store._l1_lock:
            pending = job_store._pending_terminal_publications[job_id]
            assert pending.reservation is job_store._l1[job_id]

        delayed_write.release.set()
        assert await asyncio.wait_for(publisher, timeout=3) is decision
        response = await asyncio.wait_for(poll, timeout=3)
        assert response.result == winning_result
        assert provider_calls == 2
    finally:
        paused_read.release.set()
        delayed_write.release.set()
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_poll_waits_for_current_superseding_terminal_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older completion cannot clear the gate for a newer local generation."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    older_decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    newer_decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    old_release = asyncio.Event()
    new_release = asyncio.Event()
    writer_count = 0
    winning_result = {"message": "new generation Redis winner", "tool_executions": []}

    async def _controlled_writer(
        _job_id: str, payload: dict[str, Any], **_kwargs: Any
    ) -> Any:
        nonlocal writer_count
        writer_count += 1
        current_writer = writer_count
        release = old_release if current_writer == 1 else new_release
        await release.wait()
        resolved = dict(payload)
        if current_writer == 2:
            resolved["result"] = winning_result
        return job_store._RedisWriteOutcome("committed", resolved)

    monkeypatch.setattr(job_store, "set_job_redis_only", _controlled_writer)
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    try:
        _clear_l1(job_id)
        older = asyncio.create_task(
            job_store.set_job(
                job_id,
                _job_payload(
                    job_id,
                    status=JobStatus.COMPLETED,
                    created_at=1.0,
                    sequence=1,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=thread_id,
                    result={"message": "older provisional candidate"},
                ),
                project=False,
                decision=older_decision,
            )
        )
        await asyncio.sleep(0)
        newer = asyncio.create_task(
            job_store.set_job(
                job_id,
                _job_payload(
                    job_id,
                    status=JobStatus.COMPLETED,
                    created_at=2.0,
                    sequence=2,
                    user_id=user_id,
                    organization_id=organization_id,
                    thread_id=thread_id,
                    result={"message": "newer provisional candidate"},
                ),
                project=False,
                decision=newer_decision,
            )
        )
        await asyncio.sleep(0)
        with job_store._l1_lock:
            assert (
                job_store._pending_terminal_publications[job_id].reservation
                is job_store._l1[job_id]
            )

        user = cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        )
        poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.sleep(0)
        assert not poll.done()

        old_release.set()
        assert await asyncio.wait_for(older, timeout=3) is older_decision
        await asyncio.sleep(0)
        assert not poll.done()
        with job_store._l1_lock:
            assert (
                job_store._pending_terminal_publications[job_id].reservation
                is job_store._l1[job_id]
            )

        new_release.set()
        assert await asyncio.wait_for(newer, timeout=3) is newer_decision
        response = await asyncio.wait_for(poll, timeout=3)
        assert response.result == winning_result
    finally:
        old_release.set()
        new_release.set()
        _clear_l1(job_id)


@pytest.mark.asyncio
async def test_poll_uses_durable_l1_fallback_when_redis_fails_during_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis EXEC failure releases polling to the committed local decision."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    candidate_result = {"message": "durable local fallback", "tool_executions": []}
    candidate = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=101.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=candidate_result,
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    delayed = _PauseAndFailPublication(redis_client, redis_key)
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=delayed))
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        publisher = asyncio.create_task(
            job_store.set_job(job_id, candidate, project=False, decision=decision)
        )
        await asyncio.wait_for(delayed.reached.wait(), timeout=3)
        user = cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        )
        poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.sleep(0)
        assert not poll.done()

        delayed.release.set()
        assert await asyncio.wait_for(publisher, timeout=3) is decision
        response = await asyncio.wait_for(poll, timeout=3)
        assert response.result == candidate_result
        assert await redis_client.get(redis_key) is None
    finally:
        delayed.release.set()
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_publisher_cancellation_releases_waiting_poll_to_l1_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation signals waiters without discarding the durable L1 value."""
    from src.api.agent import execute
    from src.models.user import User
    from src.services.agent import job_store

    redis_client = await _real_redis()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    redis_key = f"{job_store._JOB_KEY_PREFIX}{job_id}"
    candidate_result = {"message": "publisher cancelled after durable commit"}
    candidate = _job_payload(
        job_id,
        status=JobStatus.COMPLETED,
        created_at=101.0,
        sequence=2,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
        result=candidate_result,
    )
    decision = _durable_decision(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        user_id=user_id,
        organization_id=organization_id,
        thread_id=thread_id,
    )
    delayed = _PauseCompletedPublication(redis_client, redis_key)
    monkeypatch.setattr(job_store, "_get_redis", AsyncMock(return_value=delayed))
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    try:
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        publisher = asyncio.create_task(
            job_store.set_job(job_id, candidate, project=False, decision=decision)
        )
        await asyncio.wait_for(delayed.reached.wait(), timeout=3)
        user = cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        )
        poll = asyncio.create_task(execute.get_job_status(job_id, user))
        await asyncio.sleep(0)
        assert not poll.done()

        publisher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publisher
        response = await asyncio.wait_for(poll, timeout=3)
        assert response.result == candidate_result
    finally:
        delayed.release.set()
        await redis_client.delete(redis_key)
        _clear_l1(job_id)
        await redis_client.aclose()


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
