"""GET /agent/jobs/{id} — Postgres fallback on Redis miss (audit X1/D7).

The job record used to live only in Redis (TTL 1h): after a failover every
poller got a hard 404 and the run became untrackable. The endpoint now falls
back to the durable ``agent_runs`` projection — status + error survive, the
poller can stop cleanly — while a genuine unknown job (or another tenant's
job) still 404s.

Calls the endpoint function directly with the store layers patched out, the
same style as the other agent job unit tests.
"""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.agent.execute import (
    AgentExecuteRequest,
    AgentMessage,
    ConfirmationRequest,
    JobStatusResponse,
    confirm_agent_action,
    execute_agent,
    get_job_status,
)
from src.models.agent_run import AgentRun
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit


def _user(org=None):
    return SimpleNamespace(id=uuid.uuid4(), organization_id=org or uuid.uuid4())


def _patch_stores(l1_job=None, redis_job=None):
    """Patch the L1 and Redis read layers of the poll endpoint.

    The endpoint reads L1 first and, for non-terminal (or missing) records,
    revalidates through ``get_job_fresh`` (Redis-first, L1 fallback — P1.3
    cross-process freshness). The fake mirrors that contract: the Redis copy
    when present, else the L1 record.
    """
    return (
        patch("src.api.agent.execute._get_job", return_value=l1_job),
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=redis_job if redis_job is not None else l1_job),
        ),
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_threadless_execute_survives_job_store_outage(
    session_factory: Any,
) -> None:
    user = _user()
    background_tasks = MagicMock()
    request = AgentExecuteRequest(messages=[AgentMessage(role="user", content="hi")])

    async with session_factory() as db:
        with (
            patch(
                "src.api.agent.execute._resolve_dispatch_backend",
                return_value="background",
            ),
            patch("src.api.agent.execute._set_job"),
            patch(
                "src.api.agent.execute._agent_rate_limiter.check_rate_limit",
                new=AsyncMock(return_value=(True, 0)),
            ),
            patch(
                "src.api.agent.execute._agent_rate_limiter.record_attempt",
                new=AsyncMock(),
            ),
            patch(
                "src.api.agent.execute._resolve_thread",
                new=AsyncMock(return_value=(None, "")),
            ),
        ):
            started = await execute_agent(
                request,
                background_tasks,
                current_user=user,
                db=db,
            )

    with (
        patch("src.api.agent.execute._get_job", return_value=None),
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=None),
        ),
        patch("src.core.database.AsyncSessionLocal", session_factory),
    ):
        polled = await get_job_status(job_id=started.job_id, current_user=user)

    assert polled.status is JobStatus.QUEUED


@pytest.mark.asyncio
async def test_confirm_without_a_job_payload_fails_closed() -> None:
    """R7-L13: the projection can restore status and ownership, never the
    request payload the resume needs. Admitting the confirm used to claim the
    run (awaiting -> running) and then fail it from the background task; the
    endpoint now refuses before the claim so the run stays parked."""
    user = _user()
    job_id = str(uuid.uuid4())
    run = SimpleNamespace(
        status=JobStatus.AWAITING_CONFIRMATION,
        error=None,
        user_id=user.id,
    )
    db = AsyncMock()
    release = AsyncMock(return_value=True)

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run",
            new=AsyncMock(return_value=run),
        ) as projection_read,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.agent.execute.claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.execute.release_confirmation_claim",
            new=release,
        ),
        patch(
            "src.services.agent.job_store.compare_and_set_status",
            new=AsyncMock(return_value="missing"),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await confirm_agent_action(
            job_id,
            ConfirmationRequest(confirmed=True),
            MagicMock(),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 409
    assert "expired" in str(exc.value.detail).lower()
    projection_read.assert_awaited_once_with(
        db,
        job_id,
        organization_id=user.organization_id,
        user_id=user.id,
    )
    # Nothing was claimed, so nothing needs releasing and the run is untouched.
    release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_status", [JobStatus.RUNNING, JobStatus.QUEUED], ids=["running", "queued"]
)
async def test_confirm_stale_live_state_is_reconciled(stale_status: JobStatus) -> None:
    """A stale live non-terminal mirror must not permanently 409 confirm."""
    user = _user()
    job_id = str(uuid.uuid4())
    run = SimpleNamespace(
        status=JobStatus.AWAITING_CONFIRMATION,
        error=None,
        user_id=user.id,
    )
    db = AsyncMock()
    background_tasks = MagicMock()
    release = AsyncMock(return_value=True)

    async def claim_live_mirror(
        _job_id: str,
        expected: JobStatus,
        _new_status: JobStatus,
        *,
        project: bool = True,
    ) -> str:
        # Reproduce the real Redis CAS: the stale record conflicts with the
        # normal awaiting -> running claim, but an exact-state reconciliation
        # can atomically establish this caller's mirror claim.
        assert project is False
        return "claimed" if expected is stale_status else "conflict"

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(
                return_value={
                    "status": stale_status.value,
                    "user_id": str(user.id),
                }
            ),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run",
            new=AsyncMock(return_value=run),
        ),
        patch(
            "src.api.agent.execute.claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.execute.release_confirmation_claim",
            new=release,
        ),
        patch(
            "src.services.agent.job_store.compare_and_set_status",
            new=AsyncMock(side_effect=claim_live_mirror),
        ) as mirror_claim,
    ):
        response = await confirm_agent_action(
            job_id,
            ConfirmationRequest(confirmed=True),
            background_tasks,
            current_user=user,
            db=db,
        )

    assert response == {"status": JobStatus.RUNNING, "job_id": job_id}
    assert mirror_claim.await_args_list == [
        call(
            job_id,
            JobStatus.AWAITING_CONFIRMATION,
            JobStatus.RUNNING,
            project=False,
        ),
        call(job_id, stale_status, JobStatus.RUNNING, project=False),
    ]
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_stale_projection_keeps_request_for_edit_access_gate() -> None:
    """Projection status repair must not discard the thread authorization input."""
    from src.services.agent.agent_execution_service import AgentThreadResolutionError

    user = _user()
    job_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    db = AsyncMock()
    claim = AsyncMock(return_value=True)
    compare = AsyncMock(return_value="claimed")
    background_tasks = MagicMock()
    run = SimpleNamespace(
        status=JobStatus.AWAITING_CONFIRMATION,
        error=None,
        user_id=user.id,
    )

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(
                return_value={
                    "status": JobStatus.RUNNING.value,
                    "user_id": str(user.id),
                    "request": {
                        "messages": [{"role": "user", "content": "hello"}],
                        "thread_id": thread_id,
                    },
                }
            ),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run",
            new=AsyncMock(return_value=run),
        ),
        patch(
            "src.api.agent.execute._resolve_thread",
            new=AsyncMock(side_effect=AgentThreadResolutionError("Thread not found")),
        ) as resolve,
        patch(
            "src.api.agent.execute.claim_awaiting_run_for_confirmation",
            new=claim,
        ),
        patch(
            "src.services.agent.job_store.compare_and_set_status",
            new=compare,
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await confirm_agent_action(
            job_id,
            ConfirmationRequest(confirmed=True),
            background_tasks,
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Thread not found"
    resolve.assert_awaited_once()
    claim.assert_not_awaited()
    compare.assert_not_awaited()
    background_tasks.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_projection_read_failure_is_retryable_503() -> None:
    """An unavailable durable store is not evidence that a run is absent."""
    user = _user()
    job_id = str(uuid.uuid4())
    db = AsyncMock()
    db.rollback.side_effect = RuntimeError("rollback also unavailable")

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run",
            new=AsyncMock(side_effect=RuntimeError("postgres unavailable")),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await confirm_agent_action(
            job_id,
            ConfirmationRequest(confirmed=True),
            MagicMock(),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Confirmation is temporarily unavailable; please retry"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_empty_live_record_fails_closed_before_projection() -> None:
    """Any present live record must prove ownership before durable fallback."""
    user = _user()
    projection_read = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.services.agent.agent_run_service.get_run",
            new=projection_read,
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await confirm_agent_action(
            str(uuid.uuid4()),
            ConfirmationRequest(confirmed=True),
            MagicMock(),
            current_user=user,
            db=AsyncMock(),
        )

    assert exc.value.status_code == 404
    projection_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_claim_failure_is_503_even_if_rollback_fails() -> None:
    """The retryable API contract survives a broken transaction cleanup."""
    user = _user()
    db = AsyncMock()
    db.rollback.side_effect = RuntimeError("rollback also unavailable")

    with (
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(
                return_value={
                    "status": JobStatus.AWAITING_CONFIRMATION,
                    "user_id": str(user.id),
                }
            ),
        ),
        patch(
            "src.api.agent.execute.claim_awaiting_run_for_confirmation",
            new=AsyncMock(side_effect=RuntimeError("postgres unavailable")),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await confirm_agent_action(
            str(uuid.uuid4()),
            ConfirmationRequest(confirmed=True),
            MagicMock(),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Confirmation is temporarily unavailable; please retry"
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_miss_falls_back_to_agent_runs_projection():
    user = _user()
    run = SimpleNamespace(status="completed", error=None)
    p1, p2 = _patch_stores()
    with (
        p1,
        p2,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=run),
        ) as fallback,
    ):
        resp = await get_job_status(job_id=str(uuid.uuid4()), current_user=user)

    assert isinstance(resp, JobStatusResponse)
    assert resp.status is JobStatus.COMPLETED
    # Tenancy must flow into the projection read (org + user).
    kwargs = fallback.call_args.kwargs
    assert kwargs["organization_id"] == user.organization_id
    assert kwargs["user_id"] == user.id


@pytest.mark.asyncio
async def test_redis_miss_and_projection_miss_is_404():
    p1, p2 = _patch_stores()
    with (
        p1,
        p2,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_job_status(job_id=str(uuid.uuid4()), current_user=_user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fallback_normalizes_legacy_error_status():
    """A projection row written by a pre-collapse release reads as FAILED."""
    run = SimpleNamespace(status="error", error="boom")
    p1, p2 = _patch_stores()
    with (
        p1,
        p2,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=run),
        ),
    ):
        resp = await get_job_status(job_id=str(uuid.uuid4()), current_user=_user())
    assert resp.status is JobStatus.FAILED
    assert resp.error == "boom"


@pytest.mark.asyncio
async def test_redis_hit_never_touches_postgres():
    user = _user()
    job = {
        "status": "running",
        "user_id": str(user.id),
        "tool_executions": [],
    }
    p1, p2 = _patch_stores(l1_job=job)
    with (
        p1,
        p2,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=None),
        ) as fallback,
    ):
        resp = await get_job_status(job_id=str(uuid.uuid4()), current_user=user)
    assert resp.status is JobStatus.RUNNING
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_hit_exposes_resolved_thread_id():
    user = _user()
    thread_id = uuid.uuid4()
    job = {
        "status": "awaiting_confirmation",
        "user_id": str(user.id),
        "request": {"thread_id": str(thread_id)},
    }
    p1, p2 = _patch_stores(l1_job=job)
    with p1, p2:
        resp = await get_job_status(job_id=str(uuid.uuid4()), current_user=user)

    assert resp.thread_id == str(thread_id)


@pytest.mark.asyncio
async def test_redis_hit_with_legacy_error_status_normalizes():
    """In-Redis records written pre-collapse still poll back as FAILED."""
    user = _user()
    job = {"status": "error", "error": "old pod", "user_id": str(user.id)}
    p1, p2 = _patch_stores(l1_job=job)
    with p1, p2:
        resp = await get_job_status(job_id=str(uuid.uuid4()), current_user=user)
    assert resp.status is JobStatus.FAILED
    assert resp.error == "old pod"


@pytest.mark.asyncio
async def test_owner_mismatch_still_404s_without_fallback_leak():
    """A Redis hit owned by someone else must 404 — and must NOT consult the
    projection (which would only be reachable via the caller's own tenancy)."""
    job = {"status": "completed", "user_id": "someone-else"}
    p1, p2 = _patch_stores(l1_job=job)
    with (
        p1,
        p2,
        patch(
            "src.services.agent.agent_run_service.get_run_fallback",
            new=AsyncMock(return_value=None),
        ) as fallback,
    ):
        with pytest.raises(HTTPException) as exc:
            await get_job_status(job_id=str(uuid.uuid4()), current_user=_user())
    assert exc.value.status_code == 404
    fallback.assert_not_awaited()
