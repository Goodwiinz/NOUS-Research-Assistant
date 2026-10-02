"""Unit tests for cancellation handling in ``_run_agent_graph`` and
``_resume_agent_graph``.

CancelledError inherits from BaseException (not Exception) since
Python 3.8, so the broad ``except Exception`` clauses in these
runners do NOT catch it. Without explicit handlers the job stayed
stuck in ``"running"`` forever when the background task was
cancelled (client disconnect, server shutdown, parent timeout, etc.).

These tests assert the job is marked ``"cancelled"`` and the
exception is re-raised so the task tears down cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

from tests.utils.agent_thread_access import editable_thread_getter

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


@pytest.fixture(autouse=True)
def _stub_durable_status_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a unit-test decision without connecting to the database."""
    from src.services.agent.agent_run_service import RunStatusDecision
    from src.shared.enums import JobStatus

    async def record_job_status(
        job_id: str,
        data: dict[str, Any],
        *,
        raise_on_error: bool = False,
    ) -> RunStatusDecision:
        del raise_on_error
        status = JobStatus(data["status"])
        return RunStatusDecision(
            job_id=job_id,
            requested_status=status,
            effective_status=status,
            user_id=data.get("user_id"),
            organization_id=data.get("organization_id"),
            thread_id=data.get("thread_id"),
            error=data.get("error"),
            cancel_requested_at=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(
        "src.services.agent.agent_run_service.record_job_status", record_job_status
    )


def _make_mock_user(user_id: str = "user-cancel-test"):
    user = Mock()
    user.id = user_id
    user.email = "cancel@example.com"
    user.first_name = "Test"
    user.last_name = "User"
    user.role = Mock(value="user")
    user.organization_id = "test-org"
    user.is_active = True
    return user


def _make_mock_db():
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=Mock(return_value=None))
    )
    db.add = Mock()
    return db


@asynccontextmanager
async def _async_session_yielding(db):
    yield db


async def test_run_agent_graph_marks_job_cancelled_and_reraises():
    from src.api.agent.execute import AgentExecuteRequest, _get_job, _set_job
    from src.services.agent.agent_execution_service import _run_agent_graph

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()

    # Pre-seed job in "running" state — same as what /execute does.
    _set_job(
        job_id,
        {
            "status": "running",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
                "model": "model-router",
                "use_rag": True,
                "max_context_docs": 5,
            },
        },
    )

    request = AgentExecuteRequest(
        messages=[{"role": "user", "content": "hi"}],
        page_context={"type": "unknown"},
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            new=lambda: _async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _run_agent_graph(job_id, request, user)

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "cancelled"
    assert job["error"] == "execution cancelled"


async def test_resume_agent_graph_marks_job_cancelled_and_reraises():
    from src.api.agent.execute import _get_job, _set_job
    from src.services.agent.agent_execution_service import _resume_agent_graph

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()

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

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            new=lambda: _async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _resume_agent_graph(job_id, True, user)

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "cancelled"
    assert job["error"] == "resume cancelled"


async def test_run_agent_graph_still_marks_failed_for_regular_exceptions():
    """Regression check: the new CancelledError handler must not swallow
    plain Exception failures, which still need ``status="failed"``."""
    from src.api.agent.execute import AgentExecuteRequest, _get_job, _set_job
    from src.services.agent.agent_execution_service import _run_agent_graph

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()

    _set_job(
        job_id,
        {
            "status": "running",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "hi"}],
                "page_context": {"type": "unknown"},
                "model": "model-router",
                "use_rag": True,
                "max_context_docs": 5,
            },
        },
    )

    request = AgentExecuteRequest(
        messages=[{"role": "user", "content": "hi"}],
        page_context={"type": "unknown"},
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("kaboom"))
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            new=lambda: _async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            AsyncMock(return_value=False),
        ),
    ):
        # Plain Exception is caught — no re-raise.
        await _run_agent_graph(job_id, request, user)

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    # Error detail is no longer leaked to the client-facing job record.
    assert job["error"] == "The request could not be completed. Please retry."
    assert "kaboom" not in job["error"]


async def test_resume_agent_graph_reparks_on_chained_interrupt():
    """A multi-step destructive flow re-fires interrupt() during resume.

    The resume runner must catch GraphInterrupt and re-park the job as
    ``awaiting_confirmation`` (mirroring _run_agent_graph) rather than letting
    it fall through to ``except Exception`` and marking the job ``failed`` —
    which would silently drop the second confirmation and break HITL.
    """
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    from src.api.agent.execute import _get_job, _set_job
    from src.services.agent.agent_execution_service import _resume_agent_graph

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()

    _set_job(
        job_id,
        {
            "status": "awaiting_confirmation",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": {
                "thread_id": str(uuid4()),
                "messages": [{"role": "user", "content": "ingest then note"}],
                "page_context": {"type": "unknown"},
                "model": "model-router",
                "use_rag": True,
                "max_context_docs": 5,
            },
        },
    )

    confirmation = {
        "tools": [{"name": "create_note", "args": {"title": "n"}}],
        "message": "The agent wants to execute 1 action(s) that modify your data.",
    }

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        side_effect=GraphInterrupt((Interrupt(value=confirmation, id="i2"),))
    )
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            new=lambda: _async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            AsyncMock(return_value=False),
        ),
    ):
        # GraphInterrupt is control flow — caught, not re-raised.
        await _resume_agent_graph(job_id, True, user)

    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "awaiting_confirmation"
    assert job["confirmation"] == confirmation


async def test_queued_cancel_interrupts_graph() -> None:
    """A durable cancel accepted before startup must prevent graph work."""
    graph = _BlockedGraph()
    checker_calls: list[tuple[Any, tuple[Any, ...]]] = []
    terminal_writes: list[dict[str, Any]] = []
    sessions: list[Any] = []

    async def check(db: Any, *scope: Any) -> bool:
        checker_calls.append((db, scope))
        return True

    async with _start_test_runner(
        "initial", graph, [check], terminal_writes, sessions
    ) as (task, main_db, job_id, user):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)

        assert graph.started.is_set() is False
        assert graph.cancelled.is_set() is False
        assert len(checker_calls) == 1
        assert checker_calls[0][0] is not main_db
        assert checker_calls[0][1] == (job_id, "test-org", str(user.id))
        assert [write["status"] for write in terminal_writes] == ["cancelled"]


async def test_resumed_cancel_interrupts_graph() -> None:
    """A durable cancel during a confirmation resume cancels and joins it."""
    graph = _BlockedGraph(resume=True, owner_id="user-cancel-test")
    checker_calls: list[tuple[Any, tuple[Any, ...]]] = []
    terminal_writes: list[dict[str, Any]] = []
    sessions: list[Any] = []
    results = iter([False, True])

    async def check(db: Any, *scope: Any) -> bool:
        checker_calls.append((db, scope))
        return next(results)

    async with _start_test_runner(
        "resume", graph, [check], terminal_writes, sessions
    ) as (task, main_db, job_id, user):
        await asyncio.wait_for(graph.started.wait(), timeout=1)
        await asyncio.wait_for(graph.cancelled.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert graph.started.is_set()
        assert graph.cancelled.is_set()
        assert len(checker_calls) >= 2
        assert all(db is not main_db for db, _scope in checker_calls)
        assert all(
            scope == (job_id, "test-org", str(user.id)) for _, scope in checker_calls
        )
        assert [write["status"] for write in terminal_writes] == ["cancelled"]


async def test_cancel_race_emits_one_terminal() -> None:
    """If cancellation commits as ainvoke returns, the final marker read
    prevents completion and writes exactly one cancelled outcome."""
    graph = _FinishingGraph()
    checker_calls: list[tuple[Any, tuple[Any, ...]]] = []
    terminal_writes: list[dict[str, Any]] = []
    sessions: list[Any] = []
    results = iter([False, True])

    async def check(db: Any, *scope: Any) -> bool:
        checker_calls.append((db, scope))
        return next(results)

    async with _start_test_runner(
        "initial", graph, [check], terminal_writes, sessions
    ) as (task, main_db, job_id, user):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert len(checker_calls) == 2
        assert all(db is not main_db for db, _scope in checker_calls)
        assert [write["status"] for write in terminal_writes] == ["cancelled"]
        assert (
            len(
                [
                    write
                    for write in terminal_writes
                    if write["status"] in {"cancelled", "completed"}
                ]
            )
            == 1
        )


async def test_transient_stop_poll_error_does_not_fail_the_turn() -> None:
    """R8-C1: one failed Stop-marker read mid-turn is advisory, not fatal.

    Mutation check: make ``monitor()`` in
    ``agent_execution_service._invoke_graph_with_cancellation_monitor``
    re-raise on the first poll error (drop the consecutive-failure budget) and
    ``pytest -q backend/tests/unit/services/test_agent_cancellation.py
    -k transient_stop_poll`` fails with ``['failed']`` instead of
    ``['completed']``.
    """
    from sqlalchemy.exc import OperationalError

    graph = _SlowFinishingGraph()
    terminal_writes: list[dict[str, Any]] = []
    sessions: list[Any] = []
    calls = 0

    async def check(_db: Any, *_scope: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:  # first monitor poll; the pre-start check is call 1
            raise OperationalError("SELECT agent_runs", {}, Exception("blip"))
        return False

    async with _start_test_runner(
        "initial", graph, [check], terminal_writes, sessions
    ) as (task, _main_db, _job_id, _user):
        await asyncio.wait_for(task, timeout=2)

    assert [write["status"] for write in terminal_writes] == ["completed"]
    assert graph.finished is True
    assert calls > 2, "the monitor must keep polling after one failed read"


async def test_persistent_stop_poll_errors_still_fail_the_turn() -> None:
    """R8-C1: an unreadable Stop marker cannot be ignored forever."""
    from sqlalchemy.exc import OperationalError

    graph = _BlockedGraph()
    terminal_writes: list[dict[str, Any]] = []
    sessions: list[Any] = []
    calls = 0

    async def check(_db: Any, *_scope: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        raise OperationalError("SELECT agent_runs", {}, Exception("down"))

    async with _start_test_runner(
        "initial", graph, [check], terminal_writes, sessions
    ) as (task, _main_db, _job_id, _user):
        await asyncio.wait_for(task, timeout=2)

    assert graph.cancelled.is_set()
    assert [write["status"] for write in terminal_writes] == ["failed"]


async def test_thread_reresolution_db_error_fails_the_run() -> None:
    """R8-C6: a DB error re-checking thread access must not fall through to an
    unverified checkpoint run labelled ephemeral."""
    from sqlalchemy.exc import OperationalError

    from src.api.agent.execute import AgentExecuteRequest, _get_job, _set_job
    from src.services.agent.agent_execution_service import _run_agent_graph

    job_id = str(uuid4())
    user = _make_mock_user()
    db = _make_mock_db()
    thread_id = str(uuid4())
    request = AgentExecuteRequest(
        messages=[{"role": "user", "content": "hi"}],
        page_context={"type": "unknown"},
        model="model-router",
        use_rag=True,
        max_context_docs=5,
        thread_id=thread_id,
    )
    _set_job(
        job_id,
        {
            "status": "running",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": request.model_dump(mode="json"),
        },
    )

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(return_value={"messages": []})
    mock_graph.aget_state = AsyncMock(return_value=None)

    with (
        patch(
            "src.services.threads.workspace_access.get_thread",
            AsyncMock(
                side_effect=OperationalError(
                    "SELECT threads", {}, Exception("pool timeout")
                )
            ),
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer", new_callable=AsyncMock
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=mock_graph),
        patch(
            "src.services.agent.agent_execution_service.AsyncSessionLocal",
            new=lambda: _async_session_yielding(db),
        ),
        patch(
            "src.services.agent.agent_run_service.is_run_cancellation_requested",
            AsyncMock(return_value=False),
        ),
    ):
        await _run_agent_graph(job_id, request, user)

    mock_graph.ainvoke.assert_not_awaited()
    job = _get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert job["error"] == "The request could not be completed. Please retry."


class _SlowFinishingGraph:
    """Finishes after enough wall time for the monitor to poll repeatedly."""

    def __init__(self) -> None:
        self.finished = False

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        from langchain_core.messages import AIMessage, HumanMessage

        await asyncio.sleep(0.1)
        self.finished = True
        return {
            "messages": [
                HumanMessage(content="current request"),
                AIMessage(content="answer"),
            ],
            "tool_executions": [],
        }

    async def aget_state(self, _config: dict[str, Any]) -> None:
        return None


class _BlockedGraph:
    def __init__(self, resume: bool = False, owner_id: str = "") -> None:
        self.resume = resume
        self.owner_id = owner_id
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.state_reads = 0

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocked graph unexpectedly completed")

    async def aget_state(self, _config: dict[str, Any]) -> Any:
        if not self.resume:
            return None
        self.state_reads += 1
        if self.state_reads > 1:
            return None
        return SimpleNamespace(
            values={"user_id": self.owner_id},
            config={"configurable": {"checkpoint_id": "resume-checkpoint"}},
            tasks=[SimpleNamespace(interrupts=[object()])],
        )


class _FinishingGraph:
    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        from langchain_core.messages import AIMessage, HumanMessage

        return {
            "messages": [
                HumanMessage(content="current request"),
                AIMessage(content="answer"),
            ],
            "tool_executions": [],
        }

    async def aget_state(self, _config: dict[str, Any]) -> None:
        return None


@asynccontextmanager
async def _start_test_runner(
    runner: str,
    graph: Any,
    checkers: list[Callable[..., Any]],
    terminal_writes: list[dict[str, Any]],
    sessions: list[Any],
) -> AsyncIterator[tuple[asyncio.Task[None], Any, str, Any]]:
    from src.api.agent.execute import AgentExecuteRequest
    from src.services.agent import agent_execution_service as service

    user = _make_mock_user()
    job_id = f"cancel-monitor-{runner}-{uuid4()}"
    request = AgentExecuteRequest(
        messages=[{"role": "user", "content": "cancel this request"}],
        page_context={"type": "unknown"},
        model="model-router",
        use_rag=True,
        max_context_docs=5,
    )
    with service._jobs_lock:
        service._jobs[job_id] = {
            "status": "awaiting_confirmation" if runner == "resume" else "running",
            "tool_executions": [],
            "user_id": str(user.id),
            "request": request.model_dump(mode="json"),
        }

    main_db = _make_mock_db()

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        db = main_db if not sessions else _make_mock_db()
        sessions.append(db)
        yield db

    async def record_job(
        job_key: str,
        data: dict[str, Any],
        *,
        require_durable_decision: bool = False,
        **_kwargs: Any,
    ) -> Any:
        terminal_writes.append(data)
        with service._jobs_lock:
            service._jobs[job_key] = {**service._jobs.get(job_key, {}), **data}
        if not require_durable_decision:
            return None
        from src.services.agent.agent_run_service import RunStatusDecision
        from src.shared.enums import JobStatus

        status = JobStatus(data["status"])
        return RunStatusDecision(
            job_id=job_key,
            requested_status=status,
            effective_status=status,
            user_id=data.get("user_id"),
            organization_id=data.get("organization_id"),
            thread_id=data.get("thread_id"),
            error=data.get("error"),
            cancel_requested_at=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def cancellation_check(
        db: Any, job_key: str, organization_id: str, user_id: str
    ) -> bool:
        checker = checkers[0]
        return await checker(db, job_key, organization_id, user_id)

    from src.services.agent import agent_run_service

    patches = (
        patch.object(service, "AsyncSessionLocal", session),
        patch.object(service, "_resolve_thread", AsyncMock(return_value=(None, None))),
        patch.object(
            service, "_resolve_and_bind_project", AsyncMock(return_value=None)
        ),
        patch.object(service, "_persist_user_message_guarded", AsyncMock()),
        patch.object(service, "_clear_stale_pending_confirmation", AsyncMock()),
        patch.object(service, "_set_job_async", record_job),
        patch("src.services.agent.checkpointer.get_checkpointer", AsyncMock()),
        patch("src.services.agent.memory.get_memory_store", AsyncMock()),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.agent.runtime_snapshot.create_runtime_snapshot",
            AsyncMock(return_value=SimpleNamespace(id="cancel-test-snapshot")),
        ),
        patch(
            "src.services.agent.runtime_snapshot.runtime_state_fields", return_value={}
        ),
        patch(
            "src.services.agent.runtime_snapshot.runtime_config_fields", return_value={}
        ),
        patch(
            "src.services.agent.runtime_snapshot.resume_runtime_config_fields",
            return_value={},
        ),
        patch.object(service, "get_run", AsyncMock(return_value=None)),
        patch.object(
            agent_run_service, "is_run_cancellation_requested", cancellation_check
        ),
        patch.object(service, "_run_heartbeat", _no_heartbeat),
        patch.object(service, "_CANCELLATION_POLL_SECONDS", 0.001, create=True),
    )
    with contextlib.ExitStack() as stack:
        for context in patches:
            stack.enter_context(context)
        task = asyncio.create_task(
            service._run_agent_graph(job_id, request, user)
            if runner == "initial"
            else service._resume_agent_graph(job_id, True, user)
        )
        await asyncio.sleep(0)
        try:
            yield task, main_db, job_id, user
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@asynccontextmanager
async def _no_heartbeat(_job_id: str) -> AsyncIterator[None]:
    yield
