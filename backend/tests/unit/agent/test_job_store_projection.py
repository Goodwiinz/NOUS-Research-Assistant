"""job_store → agent_runs write-through wiring (audit P1.2).

Pins that every status write path schedules the durable projection with a
*snapshotted* payload (status/owner/org/error/thread), and that the
scheduling helper is safe with no running loop and with statusless payloads.
The projection body itself is covered in test_agent_run_service.py.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services.agent import job_store as js
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

# Captured at import time — before the autouse fixture below monkeypatches it —
# so the environment-gate test can exercise the real implementation.
_real_projection_enabled = js._projection_enabled


@pytest.fixture(autouse=True)
def _clean_store():
    js._l1.clear()
    yield
    js._l1.clear()


@pytest.fixture(autouse=True)
def _enable_projection(monkeypatch):
    """Re-enable projection scheduling for these tests.

    ``schedule_run_projection`` is a no-op under ENVIRONMENT=testing (a task
    scheduled fire-and-forget outlives pytest's per-test event loop and, on
    the CI sqlite DB, wedges a non-daemon aiosqlite thread — hanging the
    suite). These tests pin the *scheduling* contract itself, with
    ``record_job_status`` mocked and every task drained before the test
    returns, so they opt back in explicitly.
    """
    monkeypatch.setattr(js, "_projection_enabled", lambda: True)


async def _drain_projection_tasks():
    if js._projection_tasks:
        await asyncio.gather(*list(js._projection_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_set_job_schedules_projection_with_snapshot():
    recorded = AsyncMock()
    with (
        patch.object(js, "_get_redis", AsyncMock(return_value=None)),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        await js.set_job(
            "job-1",
            {
                "status": JobStatus.RUNNING,
                "user_id": "u-1",
                "organization_id": "org-1",
                "request": {"thread_id": "t-1"},
            },
        )
        await _drain_projection_tasks()

    recorded.assert_awaited_once()
    job_id, payload = recorded.await_args.args
    assert job_id == "job-1"
    assert payload["status"] == JobStatus.RUNNING
    assert payload["user_id"] == "u-1"
    assert payload["organization_id"] == "org-1"
    assert payload["thread_id"] == "t-1"  # snapshotted out of nested request


@pytest.mark.asyncio
async def test_live_only_nonterminal_write_skips_durable_projection():
    """A mirror of an already-committed durable claim must stay live-only."""
    recorded = AsyncMock()
    redis_write = AsyncMock()

    with (
        patch.object(js, "set_job_redis_only", redis_write),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        await js.set_job(
            "job-live-only",
            {
                "status": JobStatus.RUNNING,
                "user_id": "u-1",
                "organization_id": "org-1",
            },
            project=False,
        )
        await _drain_projection_tasks()

    assert js._l1["job-live-only"]["status"] is JobStatus.RUNNING
    redis_write.assert_awaited_once()
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_terminal_projection_lands_before_job_publication():
    order = []

    async def record(*_args, **_kwargs):
        order.append("projection")
        return SimpleNamespace(
            job_id="job-terminal",
            requested_status=JobStatus.COMPLETED,
            effective_status=JobStatus.COMPLETED,
            user_id="u-1",
            organization_id=None,
            thread_id="t-1",
            error=None,
            cancel_requested_at=None,
            updated_at="2026-09-26T00:00:00+00:00",
        )

    async def write_redis(*_args, **_kwargs):
        order.append("redis")

    recorded = AsyncMock(side_effect=record)
    redis_write = AsyncMock(side_effect=write_redis)
    js._l1["job-terminal"] = {
        "status": JobStatus.RUNNING,
        "user_id": "u-1",
        "request": {"thread_id": "t-1"},
        "created_at": time.time(),
    }

    with (
        patch.object(js, "set_job_redis_only", redis_write),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        await js.set_job("job-terminal", {"status": JobStatus.COMPLETED})

    recorded.assert_awaited_once_with(
        "job-terminal",
        {
            "status": JobStatus.COMPLETED,
            "user_id": "u-1",
            "organization_id": None,
            "error": None,
            "thread_id": "t-1",
        },
        raise_on_error=True,
    )
    redis_write.assert_awaited_once()
    assert order == ["projection", "redis"]


@pytest.mark.asyncio
async def test_thread_terminal_projection_failure_does_not_publish_requested_state():
    """A failed durable decision cannot expose the requested terminal result."""
    recorded = AsyncMock(side_effect=RuntimeError("db unavailable"))
    redis_write = AsyncMock()
    js._l1["job-terminal"] = {
        "status": JobStatus.RUNNING,
        "user_id": "u-1",
        "request": {"thread_id": "t-1"},
        "created_at": time.time(),
    }

    with (
        patch.object(js, "set_job_redis_only", redis_write),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        with pytest.raises(RuntimeError, match="db unavailable"):
            await js.set_job(
                "job-terminal",
                {"status": JobStatus.COMPLETED, "result": {"message": "new"}},
                require_durable_decision=True,
            )
        await _drain_projection_tasks()

    assert js._l1["job-terminal"]["status"] is JobStatus.RUNNING
    assert "result" not in js._l1["job-terminal"]
    redis_write.assert_not_awaited()
    recorded.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_job_publishes_the_durable_winner_not_losing_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted Stop stays STOPPING when the producer proposes COMPLETED."""
    js._l1["job-stop-wins"] = {
        "status": JobStatus.RUNNING,
        "user_id": "u-1",
        "organization_id": "org-1",
        "thread_id": "t-1",
        "created_at": time.time(),
    }
    monkeypatch.setattr(js, "_projection_enabled", lambda: False)
    redis_write = AsyncMock()
    monkeypatch.setattr(js, "set_job_redis_only", redis_write)
    decision = SimpleNamespace(
        job_id="job-stop-wins",
        requested_status=JobStatus.COMPLETED,
        effective_status=JobStatus.STOPPING,
        requested_status_won=False,
        user_id="u-1",
        organization_id="org-1",
        thread_id="t-1",
        error=None,
        cancel_requested_at="2026-09-26T00:00:00+00:00",
    )
    recorded = AsyncMock(return_value=decision)

    with patch("src.services.agent.agent_run_service.record_job_status", new=recorded):
        result = await js.set_job(
            "job-stop-wins",
            {
                "status": JobStatus.COMPLETED,
                "result": {"message": "must not leak"},
                "user_id": "u-1",
                "organization_id": "org-1",
                "thread_id": "t-1",
            },
            require_durable_decision=True,
        )

    assert result is decision
    assert js._l1["job-stop-wins"]["status"] is JobStatus.STOPPING
    assert "result" not in js._l1["job-stop-wins"]
    redis_write.assert_awaited_once()
    assert redis_write.await_args.args[1]["status"] is JobStatus.STOPPING
    assert "result" not in redis_write.await_args.args[1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_awaiting_confirmation_publication_requires_durable_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer cannot return from an interrupt before durable projection."""
    monkeypatch.setattr(js, "_projection_enabled", lambda: False)
    redis_write = AsyncMock()
    monkeypatch.setattr(js, "set_job_redis_only", redis_write)
    decision = SimpleNamespace(
        job_id="job-park",
        requested_status=JobStatus.AWAITING_CONFIRMATION,
        effective_status=JobStatus.AWAITING_CONFIRMATION,
        requested_status_won=True,
        user_id="u-2",
        organization_id="org-2",
        thread_id=None,
        error=None,
        cancel_requested_at=None,
    )
    recorded = AsyncMock(return_value=decision)

    with patch("src.services.agent.agent_run_service.record_job_status", new=recorded):
        result = await js.set_job(
            "job-park",
            {
                "status": JobStatus.AWAITING_CONFIRMATION,
                "user_id": "u-2",
                "organization_id": "org-2",
            },
            require_durable_decision=True,
        )

    assert result is decision
    recorded.assert_awaited_once()
    assert recorded.await_args.kwargs["raise_on_error"] is True
    assert js._l1["job-park"]["status"] is JobStatus.AWAITING_CONFIRMATION
    redis_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_cas_in_memory_projects_the_claimed_transition():
    """confirm's awaiting→running claim reaches the projection (via set_job)."""
    recorded = AsyncMock()
    js._l1["job-2"] = {
        "status": "awaiting_confirmation",
        "user_id": "u-2",
        "created_at": time.time(),
    }
    with (
        patch.object(js, "_get_redis", AsyncMock(return_value=None)),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        outcome = await js.compare_and_set_status(
            "job-2", JobStatus.AWAITING_CONFIRMATION, JobStatus.RUNNING
        )
        await _drain_projection_tasks()

    assert outcome == "claimed"
    statuses = [call.args[1]["status"] for call in recorded.await_args_list]
    assert JobStatus.RUNNING in statuses


@pytest.mark.asyncio
async def test_cas_live_only_skips_projection_after_durable_claim():
    """A CAS mirroring an awaited Postgres claim must not race a re-park."""
    recorded = AsyncMock()
    js._l1["job-cas-live-only"] = {
        "status": JobStatus.AWAITING_CONFIRMATION,
        "user_id": "u-2",
        "created_at": time.time(),
    }
    with (
        patch.object(js, "_get_redis", AsyncMock(return_value=None)),
        patch("src.services.agent.agent_run_service.record_job_status", new=recorded),
    ):
        outcome = await js.compare_and_set_status(
            "job-cas-live-only",
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        )
        await _drain_projection_tasks()

    assert outcome == "claimed"
    assert js._l1["job-cas-live-only"]["status"] is JobStatus.RUNNING
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_is_noop_under_testing_environment(monkeypatch):
    """Under ENVIRONMENT=testing the real gate must skip scheduling entirely.

    Regression: a fire-and-forget projection task outliving pytest's per-test
    event loop left a non-daemon aiosqlite worker thread blocked on a closed
    loop, hanging the CI unit-test job.
    """
    monkeypatch.setattr(js, "_projection_enabled", _real_projection_enabled)
    with patch(
        "src.services.agent.agent_run_service.record_job_status", new=AsyncMock()
    ) as recorded:
        js.schedule_run_projection("job-env", {"status": "running", "user_id": "u"})
        await _drain_projection_tasks()
    recorded.assert_not_awaited()
    assert not js._projection_tasks


def test_schedule_is_noop_without_running_loop():
    """Sync caller outside async context — must not raise, must not schedule."""
    with patch(
        "src.services.agent.agent_run_service.record_job_status", new=AsyncMock()
    ) as recorded:
        js.schedule_run_projection("job-3", {"status": "running", "user_id": "u"})
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_skips_statusless_payload():
    with patch(
        "src.services.agent.agent_run_service.record_job_status", new=AsyncMock()
    ) as recorded:
        js.schedule_run_projection("job-4", {"user_id": "u"})
        await _drain_projection_tasks()
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduling_failure_never_breaks_the_write_path():
    """A broken loop/create_task must degrade to a logged skip, not an error."""
    with (
        patch.object(js, "_get_redis", AsyncMock(return_value=None)),
        patch.object(asyncio, "get_running_loop", side_effect=RuntimeError("no loop")),
    ):
        await js.set_job("job-5", {"status": JobStatus.RUNNING, "user_id": "u"})
    assert js._l1["job-5"]["status"] == JobStatus.RUNNING
