"""Regression tests for terminal-write durability in the job store (audit H2/M10/L12).

H2: ``set_job`` awaited the strict ``agent_runs`` projection (``raise_on_error=True``)
BEFORE the L1/Redis writes for a thread-scoped terminal status. Every caller sits
directly in an except-handler body with no try/except of its own, so a Postgres
blip at completion escaped ``_run_agent_graph`` entirely, killing the background
task with L1/Redis stuck on "running" — the real error was lost until the sweeper
overwrote it with a generic "swept as stale" message.

M10: the monotonic L1 guard compared the new write against ``existing``, a
snapshot taken *before* the strict projection's Postgres round-trip. A newer
write landing in L1 during that await (e.g. ``get_job_fresh`` folding in a
fresher Redis read) was unconditionally stomped because the comparison used
the stale pre-await snapshot instead of a fresh read.

L12: a failed Redis ping left the connection pool ``from_url`` had already
opened un-``aclose()``d — one leaked pool per retry cycle during an outage.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from src.services.agent import job_store as js
from src.shared.enums import JobStatus


@pytest.fixture(autouse=True)
def _clean_l1() -> Iterator[None]:
    with js._l1_lock:
        js._l1.clear()
    yield
    with js._l1_lock:
        js._l1.clear()


@pytest.mark.unit
@pytest.mark.parametrize(
    "existing_status",
    [JobStatus.COMPLETED.value, JobStatus.STOPPING.value],
)
def test_sync_set_job_preserves_terminal_and_stopping_winners(
    existing_status: str,
) -> None:
    """A newer bootstrap mirror cannot regress an existing durable winner."""
    from src.services.agent.agent_execution_service import _set_job

    winner = {
        "status": existing_status,
        "user_id": "u-sync-guard",
        "result": {"answer": "durable winner"},
        "created_at": 1.0,
        "_seq": 1,
    }
    js._l1["job-sync-guard"] = dict(winner)

    _set_job(
        "job-sync-guard",
        {"status": JobStatus.RUNNING.value, "user_id": "u-sync-guard"},
        project=False,
    )

    assert js._l1["job-sync-guard"] == winner


@pytest.mark.unit
def test_sync_set_job_keeps_newer_nonterminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absorbing-state guard retains existing nonterminal freshness order."""
    import src.services.agent.agent_execution_service as execution

    winner = {
        "status": JobStatus.RUNNING.value,
        "user_id": "u-sync-freshness",
        "created_at": 2000.0,
        "_seq": 50,
    }
    js._l1["job-sync-freshness"] = dict(winner)
    monkeypatch.setattr(execution.time, "time", lambda: 1000.0)

    execution._set_job(
        "job-sync-freshness",
        {"status": JobStatus.QUEUED.value, "user_id": "u-sync-freshness"},
        project=False,
    )

    assert js._l1["job-sync-freshness"] == winner


@pytest.mark.unit
def test_sync_set_job_rejects_terminal_publication() -> None:
    """Sync writers still defer terminal states to awaited durable publication."""
    from src.services.agent.agent_execution_service import _set_job

    current = {
        "status": JobStatus.RUNNING.value,
        "user_id": "u-sync-terminal",
        "created_at": 1.0,
        "_seq": 1,
    }
    js._l1["job-sync-terminal"] = dict(current)

    with pytest.raises(js.JobStatusPublicationError):
        _set_job(
            "job-sync-terminal",
            {"status": JobStatus.COMPLETED.value, "user_id": "u-sync-terminal"},
            project=False,
        )

    assert js._l1["job-sync-terminal"] == current


# ---------------------------------------------------------------------------
# H2 — terminal status must survive a failed strict projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_job_does_not_publish_terminal_status_when_projection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requested terminal cannot become visible without a DB decision."""
    js._l1["job-1"] = {
        "status": "running",
        "user_id": "u-1",
        "request": {"thread_id": "t-1"},
        "created_at": time.time(),
    }
    monkeypatch.setattr(js, "_get_redis", AsyncMock(return_value=None))
    failing_projection = AsyncMock(side_effect=RuntimeError("pg blip"))

    with patch(
        "src.services.agent.agent_run_service.record_job_status",
        new=failing_projection,
    ):
        with pytest.raises(js.JobStatusPublicationError, match="pg blip"):
            await js.set_job(
                "job-1",
                {
                    "status": "completed",
                    "thread_id": "t-1",
                    "error": "real msg",
                },
                require_durable_decision=True,
            )

    job = await js.get_job("job-1")
    assert job is not None, "terminal status lost after projection failure"
    assert job["status"] == "running"
    assert "error" not in job
    failing_projection.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_job_terminal_status_control_projection_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: an unmodified success path is unaffected by the try/except."""
    js._l1["job-2"] = {
        "status": "running",
        "user_id": "u-1",
        "request": {"thread_id": "t-2"},
        "created_at": time.time(),
    }
    monkeypatch.setattr(js, "_get_redis", AsyncMock(return_value=None))
    ok_projection = AsyncMock(
        return_value=SimpleNamespace(
            job_id="job-2",
            requested_status=JobStatus.COMPLETED,
            effective_status=JobStatus.COMPLETED,
            user_id="u-1",
            organization_id=None,
            thread_id="t-2",
            error=None,
            cancel_requested_at=None,
            updated_at="2026-09-26T00:00:00+00:00",
        )
    )

    with patch(
        "src.services.agent.agent_run_service.record_job_status", new=ok_projection
    ):
        await js.set_job(
            "job-2",
            {"status": "completed", "thread_id": "t-2"},
            require_durable_decision=True,
        )

    job = await js.get_job("job-2")
    assert job is not None
    assert job["status"] == "completed"
    ok_projection.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_committed_terminal_decision_survives_redis_cache_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache outage after commit cannot undo or hide the durable winner."""
    js._l1["job-cache-outage"] = {
        "status": JobStatus.RUNNING.value,
        "user_id": "u-3",
        "thread_id": "t-3",
        "created_at": time.time(),
    }
    decision = SimpleNamespace(
        job_id="job-cache-outage",
        requested_status=JobStatus.COMPLETED,
        effective_status=JobStatus.COMPLETED,
        user_id="u-3",
        organization_id=None,
        thread_id="t-3",
        error=None,
        cancel_requested_at=None,
        updated_at="2026-09-26T00:00:00+00:00",
    )
    projection = AsyncMock(return_value=decision)
    redis_client = SimpleNamespace(
        eval=AsyncMock(side_effect=OSError("redis unavailable"))
    )
    monkeypatch.setattr(js, "_get_redis", AsyncMock(return_value=redis_client))

    with patch(
        "src.services.agent.agent_run_service.record_job_status", new=projection
    ):
        result = await js.set_job(
            "job-cache-outage",
            {
                "status": JobStatus.COMPLETED,
                "user_id": "u-3",
                "thread_id": "t-3",
                "result": {"answer": "committed"},
            },
            require_durable_decision=True,
        )

    assert result is decision
    assert js._l1["job-cache-outage"]["status"] is JobStatus.COMPLETED
    assert js._l1["job-cache-outage"]["result"] == {"answer": "committed"}
    projection.assert_awaited_once()
    redis_client.eval.assert_awaited_once()


# ---------------------------------------------------------------------------
# M10 — the L1 monotonic guard must not compare against a stale pre-await snapshot
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_job_does_not_stomp_newer_write_landing_during_projection_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer L1 write landing while the strict projection is in flight must
    survive: the guard has to compare against a fresh read taken under the
    second lock, not the snapshot captured before the await widened the race
    window to a full Postgres round-trip."""
    js._l1["job-1"] = {
        "status": "running",
        "user_id": "u-1",
        "request": {"thread_id": "t-1"},
        "created_at": 1000.0,
        "_seq": 1,
    }
    monkeypatch.setattr(js, "_get_redis", AsyncMock(return_value=None))
    # Freeze this call's own created_at strictly *between* the pre-await
    # snapshot and the "newer" record below, so surviving is only possible by
    # re-reading L1 under the second lock — not by losing on the merits of
    # wall-clock time either way.
    monkeypatch.setattr(js.time, "time", lambda: 1500.0)

    newer_record = {
        "status": "running",
        "user_id": "u-1",
        "thread_id": "t-1",
        "created_at": 2000.0,
        "_seq": 999,
    }

    async def _land_newer_write_during_await(*_args: Any, **_kwargs: Any) -> None:
        # Simulates another writer (e.g. get_job_fresh folding a fresher Redis
        # read into L1, or a concurrent set_job) advancing the job while this
        # call awaits the projection's Postgres round-trip.
        with js._l1_lock:
            js._l1["job-1"] = dict(newer_record)

    async def _land_newer_write_during_await_with_decision(
        *_args: Any, **_kwargs: Any
    ) -> Any:
        await _land_newer_write_during_await()
        return SimpleNamespace(
            job_id="job-1",
            requested_status=JobStatus.COMPLETED,
            effective_status=JobStatus.COMPLETED,
            user_id="u-1",
            organization_id=None,
            thread_id="t-1",
            error=None,
            cancel_requested_at=None,
            updated_at="2026-09-26T00:00:00+00:00",
        )

    monkeypatch.setattr(
        "src.services.agent.agent_run_service.record_job_status",
        AsyncMock(side_effect=_land_newer_write_during_await_with_decision),
    )

    await js.set_job("job-1", {"status": "completed", "thread_id": "t-1"})

    # Pre-fix: the stale snapshot (created_at=1000.0) makes this write look
    # newer than the just-landed record, so it stomps `newer_record`.
    assert js._l1["job-1"] == newer_record


# ---------------------------------------------------------------------------
# L12 — a failed Redis ping must not leak the connection pool
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_redis_closes_client_on_failed_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``from_url`` allocates a pool synchronously; if ``ping`` then fails,
    the pool must be closed rather than dropped on the floor."""
    monkeypatch.setattr(js, "_redis", None)

    fake_client = AsyncMock()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("redis down"))

    with patch("redis.asyncio.from_url", return_value=fake_client) as from_url:
        result = await js._get_redis()

    from_url.assert_called_once()
    assert result is None
    fake_client.aclose.assert_awaited_once()
