"""Tests for agent-exec-session-pinning (audit M9 + L11).

M9: ``_run_agent_graph``/``_resume_agent_graph`` open an ``AsyncSessionLocal``
session and never commit before handing off to ``graph.ainvoke()``, which can
run for up to 360s. SQLAlchemy autobegins a transaction on the first read
(``_resolve_thread`` in the run path, ``get_run`` in the resume path), so the
uncommitted session pins one pooled connection for the whole graph run —
under modest concurrency this exhausts the pool. The fix commits (a cheap
read-only txn end) immediately before each ``asyncio.timeout(360)`` block;
these tests prove that commit happens, and happens BEFORE ``ainvoke``, by
recording both calls into one ordered list.

L11: the sync ``_set_job`` compat writer (``job_store._l1`` alias) bypassed
job_store's monotonic ``(created_at, _seq)`` guard — an unconditional
``_jobs[job_id] = data`` that could stomp a newer record written by
``job_store.set_job`` and never stamped ``_seq`` on what it wrote. The fix
reuses ``job_store._is_newer_or_equal`` and job_store's own ``_seq`` counter
(shared, not a local copy, so both writers order against one sequence).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

from tests.utils.agent_thread_access import editable_thread_getter

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


def _make_mock_user(user_id: str = "user-pinning-test") -> Mock:
    user = Mock()
    user.id = user_id
    user.email = "pinning@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = "test-org"
    user.is_active = True
    return user


def _make_mock_db() -> AsyncMock:
    """Mirrors test_agent_cancellation.py's helper: every SELECT finds
    nothing, so ``_resolve_thread`` falls through to "no workspace" (no
    thread created, no commit) and ``get_run`` finds no durable row."""
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
    )
    db.add = Mock()
    return db


@asynccontextmanager
async def _async_session_yielding(db: Any) -> AsyncIterator[Any]:
    yield db


# ---------------------------------------------------------------------------
# M9 — commit before the long graph run, not after
# ---------------------------------------------------------------------------


async def test_run_agent_graph_commits_before_ainvoke() -> None:
    from src.services.agent.agent_execution_service import _run_agent_graph
    from src.services.agent.schemas import AgentExecuteRequest

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()
    events: list[str] = []
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))

    request = AgentExecuteRequest(
        messages=[{"role": "user", "content": "hi"}],
        page_context={"type": "unknown"},
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )

    def _record_ainvoke(*a: Any, **kw: Any) -> dict[str, Any]:
        # `events.append(...) or {...}` reads as an expression but append
        # returns None, which the type gate rejects; ordering is the whole
        # point of this test, so record it explicitly.
        events.append("ainvoke")
        return {"messages": []}

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=_record_ainvoke)
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_yielding(db),
        ),
    ):
        await _run_agent_graph(job_id, request, user)

    assert "commit" in events, (
        "db.commit() was never called — the session's read transaction "
        "stays open across the full graph.ainvoke() call, pinning a pooled "
        "connection for up to 360s (audit M9)"
    )
    assert events.index("commit") < events.index("ainvoke"), (
        "db.commit() must run BEFORE graph.ainvoke(), not merely somewhere "
        "in the call — a commit issued after ainvoke does not free the "
        "connection during the run"
    )


async def test_resume_agent_graph_commits_before_ainvoke() -> None:
    from src.services.agent.agent_execution_service import _resume_agent_graph, _set_job

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()
    events: list[str] = []
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))

    # awaiting_confirmation seed, same shape as job_store expects on resume.
    # thread_id is required: the resume refuses to run without one (R7-L13 —
    # it used to fall back to thread_id=job_id and skip every guard). The
    # terminal PG projection branch stays unreached via the get_run patch.
    _set_job(
        job_id,
        {
            "status": "awaiting_confirmation",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "ingest paper"}],
                "page_context": {"type": "unknown"},
                "model": "model-router",
                "use_rag": True,
                "max_context_docs": 5,
            },
        },
    )

    def _record_ainvoke(*a: Any, **kw: Any) -> dict[str, Any]:
        # `events.append(...) or {...}` reads as an expression but append
        # returns None, which the type gate rejects; ordering is the whole
        # point of this test, so record it explicitly.
        events.append("ainvoke")
        return {"messages": []}

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=_record_ainvoke)
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            return_value=_async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_execution_service.get_run",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _resume_agent_graph(job_id, True, user)

    assert "commit" in events, (
        "db.commit() was never called before the resume's graph.ainvoke() — "
        "get_run()'s bare SELECT pins a pooled connection for up to 360s on "
        "every HITL confirm (audit M9)"
    )
    assert events.index("commit") < events.index(
        "ainvoke"
    ), "db.commit() must run BEFORE graph.ainvoke() on the resume path too"


# ---------------------------------------------------------------------------
# L11 — _set_job must not bypass the L1 monotonic guard
# ---------------------------------------------------------------------------


def test_set_job_does_not_stomp_a_newer_l1_record() -> None:
    from src.services.agent import job_store
    from src.services.agent.agent_execution_service import _set_job

    job_id = f"job-{uuid4()}"
    newer_record = {
        "status": "completed",
        "created_at": time.time() + 3600,  # unambiguously newer than "now"
        "_seq": 10**9,
        "user_id": "owner-1",
    }
    with job_store._l1_lock:
        job_store._l1[job_id] = dict(newer_record)
    try:
        stale_payload = {"status": "running", "user_id": "owner-1"}
        _set_job(job_id, stale_payload)

        with job_store._l1_lock:
            stored = job_store._l1.get(job_id)

        # The newer "completed" record must survive the stale/delayed write.
        assert stored is not None
        assert stored["status"] == "completed"
        # Stamped even though this write lost the race — L11 also fixed the
        # missing _seq, independent of whether the write wins.
        assert "_seq" in stale_payload
    finally:
        with job_store._l1_lock:
            job_store._l1.pop(job_id, None)


def test_set_job_still_writes_when_no_existing_record() -> None:
    """The guard must be a no-op for the real-world case (audit's stated
    exposure): creation-time callers writing the FIRST record for a job."""
    from src.services.agent import job_store
    from src.services.agent.agent_execution_service import _get_job, _set_job

    job_id = f"job-{uuid4()}"
    with job_store._l1_lock:
        job_store._l1.pop(job_id, None)
    try:
        _set_job(job_id, {"status": "running", "user_id": "owner-2"})

        job = _get_job(job_id)
        assert job is not None
        assert job["status"] == "running"
        assert "_seq" in job
    finally:
        with job_store._l1_lock:
            job_store._l1.pop(job_id, None)
