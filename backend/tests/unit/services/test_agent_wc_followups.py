"""Tests for WC-1 / WC-2 hardening in jobs.py.

Covers:

* WC-1: ``_clear_stale_pending_confirmation`` also resets the per-turn
  ephemeral counters (``tool_loop_count``, ``error_count``,
  ``reflection_count``) so stale state from an abandoned turn cannot bleed
  into the next turn even if a future graph refactor stops running
  ``preprocessing_node`` on the resume path.
* WC-2: ``_resume_agent_graph`` short-circuits when the snapshot no longer
  contains a ``pending_confirmation`` (the interrupt has already been
  consumed). The existing ``_jobs_lock`` in ``confirm_agent_action`` already
  prevents a double-resume race; this is the in-depth guard for the
  resume-side.
* The existing double-confirm invariant: a second ``POST /confirm/{id}``
  while the job is in ``running`` returns HTTP 409.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.agent.execute import router
from src.services.agent.agent_execution_service import (
    _clear_stale_pending_confirmation,
    _get_job,
    _resume_agent_graph,
    _set_job,
)
from tests.utils.agent_thread_access import editable_thread_getter


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interrupt_task() -> SimpleNamespace:
    """A pending LangGraph task carrying a live interrupt (the real HITL signal —
    `pending_confirmation` is always `{}` while an interrupt is live)."""
    return SimpleNamespace(interrupts=[SimpleNamespace(value={"tools": []})])


def _snapshot(values: dict, tasks: tuple = ()) -> SimpleNamespace:
    """Build a minimal LangGraph snapshot stub with the given values + tasks."""
    return SimpleNamespace(values=values, tasks=tasks)


def _make_graph(snapshot_values: dict | None, tasks: tuple = ()) -> Mock:
    graph = Mock()
    graph.aget_state = AsyncMock(
        return_value=(
            _snapshot(snapshot_values, tasks) if snapshot_values is not None else None
        )
    )
    graph.aupdate_state = AsyncMock()
    graph.ainvoke = AsyncMock(return_value={"messages": [], "tool_executions": []})
    return graph


def _make_mock_user(user_id: str = "user-aaa"):
    user = Mock()
    user.id = user_id
    user.email = "test@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = "test-org"
    user.is_active = True
    return user


@asynccontextmanager
async def _async_session_cm():
    """Async context manager that yields a mocked AsyncSession."""
    yield AsyncMock()


# ---------------------------------------------------------------------------
# WC-1: defensive reset in _clear_stale_pending_confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_stale_pending_confirmation_resets_counters():
    """When wiping a stale interrupt, also reset tool/error/reflection counters."""
    graph = _make_graph(
        {
            "pending_confirmation": {},
            "user_confirmed": False,
            "tool_loop_count": 5,
            "error_count": 2,
            "reflection_count": 1,
        },
        tasks=(_interrupt_task(),),
    )
    config = {"configurable": {"thread_id": "thread-abc"}}

    cleared = await _clear_stale_pending_confirmation(graph, config)

    # Now returns the dropped tool name(s) (truthy) instead of bool True.
    assert cleared
    graph.aupdate_state.assert_awaited_once()
    args, _kwargs = graph.aupdate_state.call_args
    assert args[0] is config
    assert args[1] == {
        "pending_confirmation": {},
        "user_confirmed": False,
        "tool_loop_count": 0,
        "error_count": 0,
        "reflection_count": 0,
    }


@pytest.mark.asyncio
async def test_clear_stale_pending_confirmation_skips_when_no_interrupt():
    """When no pending_confirmation exists, the helper is a no-op."""
    graph = _make_graph(
        {
            "pending_confirmation": {},
            "tool_loop_count": 3,  # noisy but should not be touched
        }
    )
    config = {"configurable": {"thread_id": "thread-xyz"}}

    cleared = await _clear_stale_pending_confirmation(graph, config)

    # No interrupt → None (was bool False before the Optional[list] return).
    assert cleared is None
    graph.aupdate_state.assert_not_called()


# ---------------------------------------------------------------------------
# WC-2: idempotency guard in _resume_agent_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_short_circuits_when_interrupt_already_consumed():
    """If the checkpoint has no pending_confirmation, resume must not call ainvoke.

    Simulates the race the audit flagged: a stale background resume task
    fires after the interrupt has already been answered. ``Command(resume=...)``
    would have nothing to resume against.
    """
    user = _make_mock_user()
    job_id = str(uuid4())
    _set_job(
        job_id,
        {
            "status": "running",
            "user_id": str(user.id),
            "tool_executions": [],
            # The resume needs a thread_id: without one it refuses outright
            # (R7-L13) instead of falling back to thread_id=job_id.
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
            },
        },
    )

    graph = _make_graph({"user_id": str(user.id), "pending_confirmation": {}})

    with (
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_cm(),
        ),
    ):
        await _resume_agent_graph(job_id, confirmed=True, current_user=user)

    graph.ainvoke.assert_not_called()
    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"  # legacy "error" collapsed (audit C7)
    assert "already consumed" in (job.get("error") or "").lower()


@pytest.mark.asyncio
async def test_resume_proceeds_when_interrupt_present():
    """When the snapshot still has pending_confirmation, resume reaches ainvoke."""
    user = _make_mock_user()
    job_id = str(uuid4())
    _set_job(
        job_id,
        {
            "status": "running",
            "user_id": str(user.id),
            "tool_executions": [],
            # The resume needs a thread_id: without one it refuses outright
            # (R7-L13) instead of falling back to thread_id=job_id.
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
            },
        },
    )

    graph = _make_graph(
        {"user_id": str(user.id), "pending_confirmation": {}},
        tasks=(_interrupt_task(),),
    )

    with (
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_cm(),
        ),
    ):
        await _resume_agent_graph(job_id, confirmed=True, current_user=user)

    graph.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_rejects_ownerless_checkpoint():
    """Ownerless legacy checkpoints must not be resumable from a thread id."""
    user = _make_mock_user()
    job_id = str(uuid4())
    _set_job(
        job_id,
        {
            "status": "running",
            "user_id": str(user.id),
            "tool_executions": [],
            # The resume needs a thread_id: without one it refuses outright
            # (R7-L13) instead of falling back to thread_id=job_id.
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
            },
        },
    )

    graph = _make_graph(
        {"pending_confirmation": {"tools": [{"name": "ingest_arxiv_papers"}]}}
    )

    with (
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_cm(),
        ),
    ):
        await _resume_agent_graph(job_id, confirmed=True, current_user=user)

    graph.ainvoke.assert_not_called()
    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"  # legacy "error" collapsed (audit C7)
    assert job["error"] == "Thread not found"


# ---------------------------------------------------------------------------
# Existing double-confirm invariant — protected by _jobs_lock
# ---------------------------------------------------------------------------


@pytest.fixture
def confirm_client():
    """FastAPI test client with the agent router and dependency overrides."""
    from src.core.database import get_db
    from src.core.dependencies import get_current_user

    user = _make_mock_user()

    app = FastAPI()
    app.include_router(router)

    async def override_get_current_user():
        return user

    async def override_get_db():
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
        )
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        return db

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    app.router.lifespan_context = _no_lifespan
    with TestClient(app) as client:
        yield client, user


def test_double_confirm_second_request_returns_409(confirm_client):
    """Second POST /confirm/{id} after the first flips status to running returns 409.

    This is the existing _jobs_lock invariant — included so a future
    refactor that loosens the lock fails this test loudly.
    """
    client, user = confirm_client
    job_id = str(uuid4())
    _set_job(
        job_id,
        {
            "status": "awaiting_confirmation",
            "confirmation": {"tools": [], "message": "Confirm?"},
            "tool_executions": [],
            "user_id": str(user.id),
        },
    )

    with (
        patch("src.api.agent.execute._resume_agent_graph", new_callable=AsyncMock),
        # Force the in-memory CAS path so the test is deterministic regardless of
        # whether a Redis server is reachable in the test environment.
        patch(
            "src.services.agent.job_store._get_redis",
            new=AsyncMock(return_value=None),
        ),
    ):
        first = client.post(f"/api/v1/agent/confirm/{job_id}", json={"confirmed": True})
        second = client.post(
            f"/api/v1/agent/confirm/{job_id}", json={"confirmed": True}
        )

    assert first.status_code == 200
    assert first.json()["status"] == "running"
    assert second.status_code == 409


def test_confirm_falls_back_to_redis_after_l1_eviction(confirm_client):
    """A pending job in Redis should still be confirmable after L1 eviction."""
    client, user = confirm_client
    job_id = str(uuid4())
    redis_job = {
        "status": "awaiting_confirmation",
        "confirmation": {"tools": [], "message": "Confirm?"},
        "tool_executions": [],
        "user_id": str(user.id),
    }

    with (
        # The endpoint's validation read is Redis-first (get_job_fresh — P1.3
        # cross-process freshness); the CAS's in-memory fallback still reads
        # via get_job. Patch both to serve the Redis-only record.
        patch(
            "src.services.agent.job_store.get_job_fresh",
            new=AsyncMock(return_value=redis_job),
        ),
        patch(
            "src.services.agent.job_store.get_job",
            new=AsyncMock(return_value=redis_job),
        ),
        patch(
            "src.services.agent.job_store.set_job",
            new=AsyncMock(),
        ) as mock_set_job,
        patch("src.api.agent.execute._resume_agent_graph", new_callable=AsyncMock),
        # Force the in-memory CAS path (store-API level, which this test mocks)
        # so it doesn't depend on a reachable Redis server.
        patch(
            "src.services.agent.job_store._get_redis",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = client.post(
            f"/api/v1/agent/confirm/{job_id}", json={"confirmed": True}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    # The CAS copies before mutating (audit S-L11) — the transition is
    # observable through the write-through, not by in-place mutation of the
    # record other readers may hold.
    assert redis_job["status"] == "awaiting_confirmation"
    mock_set_job.assert_awaited_once()
    assert mock_set_job.await_args.args[1]["status"] == "running"


# ---------------------------------------------------------------------------
# R7-L13 review follow-up: "no thread_id" is not always "expired"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_uses_job_id_when_payload_has_no_thread_id():
    """A workspace-less user's run is checkpointed under ``thread_id=job_id``.

    ``_resolve_thread`` returns None for them, so ``_run_agent_graph`` writes
    the checkpoint under the job id. The payload is present and simply carries
    no thread_id — the resume must follow the run there instead of declaring
    the confirmation expired.
    """
    user = _make_mock_user()
    job_id = str(uuid4())
    _set_job(
        job_id,
        {
            "status": "running",
            "user_id": str(user.id),
            "tool_executions": [],
            "request": {
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
            },
        },
    )

    graph = _make_graph(
        {"user_id": str(user.id), "pending_confirmation": {}},
        tasks=(_interrupt_task(),),
    )

    with (
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_cm(),
        ),
    ):
        await _resume_agent_graph(job_id, confirmed=True, current_user=user)

    graph.ainvoke.assert_awaited_once()
    read_config = graph.aget_state.await_args.args[0]
    assert read_config["configurable"]["thread_id"] == job_id


@pytest.mark.asyncio
async def test_resume_fails_expired_when_job_payload_is_gone():
    """No job record at all → the request (and its thread_id) is unrecoverable."""
    user = _make_mock_user()
    job_id = str(uuid4())  # never _set_job'd

    graph = _make_graph(
        {"user_id": str(user.id), "pending_confirmation": {}},
        tasks=(_interrupt_task(),),
    )

    with (
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_cm(),
        ),
    ):
        await _resume_agent_graph(job_id, confirmed=True, current_user=user)

    graph.aget_state.assert_not_awaited()
    graph.ainvoke.assert_not_called()
    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert "expired" in (job.get("error") or "").lower()
