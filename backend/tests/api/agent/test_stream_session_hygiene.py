"""SSE session hygiene + stream-exit ordering (agent audit round 8, slice S6).

R8-D1  the durable Stop poll is time-gated and never pins the stream session.
R8-D2  replay paths end the read transaction before streaming.
R8-D5  a pre-graph failure after Stop acknowledges the cancellation.
R8-D8  FAILED is committed before the ERROR frame (``/stream`` and Luna).
R8-C5  ``/stream`` heartbeats ``agent_runs.updated_at`` during the graph phase.

Fakes only — no Postgres/Redis. Each test was run red against the unfixed
code (see the PR description for the verbatim failures).

Mutation checks (testing.md): the guard each test protects lives in
``backend/src/api/agent/streaming.py`` / ``execute.py``:
- D1: ``_graph_events_with_keepalive`` ``stop_due`` gate + ``_durable_stop_poller``
  fresh session —
  ``pytest -q backend/tests/api/agent/test_stream_session_hygiene.py -k stop_poll``
- D2: ``await db.rollback()`` before the replay loop / ``StreamingResponse`` —
  ``-k replay``
- D5: ``durable_stop_requested``/``cancel_current_stream`` bound before ``try`` —
  ``-k pre_graph``
- D8: FAILED finalize before the ERROR emit — ``-k error_frame``
- C5: ``_run_heartbeat`` around the graph loop — ``-k heartbeat``
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessageChunk

from src.models.user import User
from src.services.agent.agent_submission_service import AcceptedSubmission
from src.services.agent.run_event_types import RunEventType
from src.services.agent.runtime_snapshot import empty_runtime_snapshot
from src.services.agent.stream_buffer import BufferedFrame
from src.shared.enums import JobStatus
from tests.utils.agent_stream import make_stream_request, sse_event_name

pytestmark = pytest.mark.unit

THREAD_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
RUN_ID = "55555555-5555-5555-5555-555555555555"


class _SpySession:
    """Stream-session double that models SQLAlchemy autobegin.

    Any read through it opens a transaction that only commit/rollback/close
    end — exactly the state that pins a pooled connection.
    """

    def __init__(self) -> None:
        self.txn = False

    def in_transaction(self) -> bool:
        return self.txn

    async def commit(self) -> None:
        self.txn = False

    async def rollback(self) -> None:
        self.txn = False

    async def close(self) -> None:
        self.txn = False


def _token_event(text: str) -> dict[str, Any]:
    return {
        "event": "on_chat_model_stream",
        "name": "llm_node",
        "metadata": {"langgraph_node": "llm_node"},
        "data": {"chunk": SimpleNamespace(content=text)},
    }


class _ScriptedGraph:
    """``astream_events`` double driven by a per-pull callback."""

    def __init__(self, pull: Callable[[int], Any]) -> None:
        self._pull = pull
        self._n = 0
        self.aclosed = False

    def astream_events(self, *_args: Any, **_kwargs: Any) -> _ScriptedGraph:
        return self

    def __aiter__(self) -> _ScriptedGraph:
        return self

    async def __anext__(self) -> dict[str, Any]:
        self._n += 1
        result = self._pull(self._n)
        if asyncio.iscoroutine(result):
            result = await result
        return cast(dict[str, Any], result)

    async def aclose(self) -> None:
        self.aclosed = True

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(values={"messages": []}, tasks=())


def _user() -> User:
    return cast(
        User,
        SimpleNamespace(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            organization_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        ),
    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(request_id="s6-request"),
        is_disconnected=AsyncMock(return_value=False),
    )


def _acceptance(*, replayed: bool = False) -> AcceptedSubmission:
    return AcceptedSubmission(
        run_id=RUN_ID,
        thread_id=str(THREAD_ID),
        user_message_id="66666666-6666-6666-6666-666666666666",
        outbox_id="77777777-7777-7777-7777-777777777777",
        idempotency_key="idem-s6",
        replayed=replayed,
    )


@contextlib.contextmanager
def _graph_stream_env(
    *,
    db: Any,
    graph: Any,
    cancel_poll: Any,
    finalize: Any,
    acceptance: Optional[AcceptedSubmission] = None,
    runtime_snapshot: Any = None,
    resolve_thread: Any = None,
    get_run: Any = None,
) -> Iterator[None]:
    from src.api.agent import streaming as streaming_mod

    with contextlib.ExitStack() as stack:
        for target, attr, value in (
            (streaming_mod, "AsyncSessionLocal", Mock(return_value=db)),
            (
                streaming_mod,
                "_resolve_thread",
                resolve_thread
                or AsyncMock(
                    return_value=(SimpleNamespace(id=THREAD_ID), "conversation-1")
                ),
            ),
            (
                streaming_mod,
                "accept_submission",
                AsyncMock(return_value=acceptance or _acceptance()),
            ),
            (
                streaming_mod,
                "mark_submission_dispatched",
                AsyncMock(return_value=True),
            ),
            (streaming_mod, "is_run_cancellation_requested", cancel_poll),
            (streaming_mod, "_resolve_and_bind_project", AsyncMock(return_value=None)),
            (
                streaming_mod,
                "_clear_stale_pending_confirmation",
                AsyncMock(return_value=None),
            ),
            (streaming_mod, "_finalize_run", finalize),
            (streaming_mod, "get_run", get_run or AsyncMock(return_value=None)),
            (
                streaming_mod._jobs_mod,
                "_persist_assistant_message_safe",
                AsyncMock(return_value="assistant-row-1"),
            ),
            (
                streaming_mod._stream_buffer,
                "start_stream",
                AsyncMock(return_value="stream-1"),
            ),
            (streaming_mod._stream_buffer, "append", AsyncMock(return_value=None)),
            (
                streaming_mod._stream_buffer,
                "finish_stream",
                AsyncMock(return_value=None),
            ),
        ):
            stack.enter_context(patch.object(target, attr, new=value))
        for dotted, value in (
            ("src.services.agent.observability.configure_langsmith", Mock()),
            (
                "src.services.agent.checkpointer.get_checkpointer",
                AsyncMock(return_value=object()),
            ),
            (
                "src.services.agent.memory.get_memory_store",
                AsyncMock(return_value=object()),
            ),
            ("src.services.agent.graph.compile_agent_graph", Mock(return_value=graph)),
            (
                "src.services.agent.fast_path.classify_fast_path_turn",
                Mock(return_value=SimpleNamespace(eligible=False)),
            ),
            (
                "src.services.agent.runtime_snapshot.create_runtime_snapshot",
                runtime_snapshot or AsyncMock(return_value=empty_runtime_snapshot()),
            ),
        ):
            stack.enter_context(patch(dotted, new=value))
        yield


async def _drain(
    generator: Any, on_frame: Callable[[str], None] = lambda _f: None
) -> list[str]:
    frames: list[str] = []
    async for frame in generator:
        on_frame(frame)
        frames.append(frame)
    return frames


def _stream(body: Any = None) -> Any:
    from src.api.agent import streaming as streaming_mod

    return streaming_mod.stream_event_generator(
        body
        or make_stream_request(
            messages=[
                {
                    "role": "user",
                    "content": "write a long answer",
                    "client_message_id": str(uuid.uuid4()),
                }
            ],
            thread_id=str(THREAD_ID),
        ),
        _request(),
        _user(),
    )


# ---------------------------------------------------------------------------
# R8-D1 — Stop poll: time-gated, own short session, recovers after a failure
# ---------------------------------------------------------------------------


async def test_stop_poll_is_time_gated_and_never_pins_stream_session() -> None:
    from src.api.agent import streaming as streaming_mod

    db = _SpySession()
    calls: list[float] = []
    txn_seen_by_pull: list[bool] = []
    window: dict[str, float] = {}

    async def cancel_poll(session: Any, *_args: Any, **_kwargs: Any) -> bool:
        calls.append(time.monotonic())
        if session is db:
            db.txn = True  # autobegin; nothing in the stream ever ends it
        return False

    def pull(n: int) -> dict[str, Any]:
        txn_seen_by_pull.append(db.in_transaction())
        if n == 1:
            window["start"] = time.monotonic()
            window["calls_at_start"] = len(calls)
        if n > 500:
            window["end"] = time.monotonic()
            window["calls_at_end"] = len(calls)
            raise StopAsyncIteration
        return _token_event("x")

    with _graph_stream_env(
        db=db,
        graph=_ScriptedGraph(pull),
        cancel_poll=AsyncMock(side_effect=cancel_poll),
        finalize=AsyncMock(return_value=True),
    ):
        frames = await _drain(_stream())

    assert sse_event_name(frames[-1]) == "done"
    loop_polls = window["calls_at_end"] - window["calls_at_start"]
    elapsed = window["end"] - window["start"]
    assert (
        loop_polls <= elapsed / streaming_mod._SSE_DISCONNECT_POLL_SECONDS + 2
    ), f"{loop_polls} stop-marker polls for 500 graph events in {elapsed:.3f}s"
    assert not any(txn_seen_by_pull), (
        "the stream session held an open transaction while the graph pulled "
        "its next event (pinned pooled connection)"
    )


async def test_stop_poll_failure_on_a_killed_connection_does_not_swallow_stop() -> None:
    """One dead connection must not hide every later Stop for the turn."""
    db = _SpySession()
    failed_sessions: list[Any] = []
    first_failure = True

    async def cancel_poll(session: Any, *_args: Any, **_kwargs: Any) -> bool:
        nonlocal first_failure
        if first_failure or any(session is s for s in failed_sessions):
            # A killed connection: this session raises on every later use
            # until it is rolled back / replaced (PendingRollbackError).
            first_failure = False
            failed_sessions.append(session)
            raise ConnectionError("terminating connection due to idle timeout")
        return True

    never = asyncio.Event()

    async def blocked_pull(n: int) -> dict[str, Any]:
        if n == 1:
            return _token_event("partial")
        await never.wait()
        raise StopAsyncIteration

    finalize = AsyncMock(return_value=True)
    with _graph_stream_env(
        db=db,
        graph=_ScriptedGraph(blocked_pull),
        cancel_poll=AsyncMock(side_effect=cancel_poll),
        finalize=finalize,
    ):
        await asyncio.wait_for(_drain(_stream()), timeout=5)

    finalize.assert_awaited_once()
    assert finalize.await_args is not None
    kwargs = finalize.await_args.kwargs
    assert kwargs["status"] is JobStatus.CANCELLED
    assert kwargs["payload"]["reason"] == "user_requested"


# ---------------------------------------------------------------------------
# R8-D2 — replay paths release the read transaction before streaming
# ---------------------------------------------------------------------------


async def test_resume_replay_ends_read_transaction_before_first_read_after() -> None:
    from src.api.agent import execute as execute_mod

    thread_id = str(THREAD_ID)
    stream_id = str(uuid.uuid4())
    db = _SpySession()
    txn_at_read: list[bool] = []

    async def resolve(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        session.txn = True
        return SimpleNamespace(id=thread_id), "conversation"

    async def active_run(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        session.txn = True
        return SimpleNamespace(job_id=RUN_ID, status="running")

    async def read_after(*_args: Any, **_kwargs: Any) -> list[BufferedFrame]:
        txn_at_read.append(db.in_transaction())
        return [BufferedFrame(seq=1, frame="id: 1\nevent: done\ndata: {}\n\n")]

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=stream_id),
        ),
        patch.object(
            execute_mod._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value=stream_id),
        ),
        patch.object(execute_mod._stream_buffer, "read_after", new=read_after),
        patch.object(execute_mod, "get_active_run_for_thread", new=active_run),
    ):
        response = await execute_mod.resume_stream(
            _request(),  # type: ignore[arg-type]
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=_user(),
            db=db,  # type: ignore[arg-type]
        )
        frames = [frame async for frame in response.body_iterator]

    assert frames, "the replay produced no frames"
    assert txn_at_read == [False], (
        "the request-scoped session still held its read transaction when the "
        "replay loop started"
    )


async def test_replayed_acceptance_ends_read_transaction_before_replay() -> None:
    db = _SpySession()
    txn_at_read: list[bool] = []

    async def get_run(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        session.txn = True
        return SimpleNamespace(thread_id=THREAD_ID)

    async def read_after(*_args: Any, **_kwargs: Any) -> list[BufferedFrame]:
        txn_at_read.append(db.in_transaction())
        return [BufferedFrame(seq=1, frame="id: 1\nevent: done\ndata: {}\n\n")]

    from src.api.agent import streaming as streaming_mod

    with (
        _graph_stream_env(
            db=db,
            graph=_ScriptedGraph(lambda _n: {}),
            cancel_poll=AsyncMock(return_value=False),
            finalize=AsyncMock(return_value=True),
            acceptance=_acceptance(replayed=True),
            get_run=get_run,
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "stream_id_for_run",
            new=AsyncMock(return_value="stream-1"),
        ),
        patch.object(streaming_mod._stream_buffer, "read_after", new=read_after),
    ):
        frames = await _drain(_stream())

    assert frames and sse_event_name(frames[-1]) == "done"
    assert txn_at_read == [False]


# ---------------------------------------------------------------------------
# R8-D5 — pre-graph failure after Stop must ACK the cancellation
# ---------------------------------------------------------------------------


async def test_pre_graph_failure_after_stop_acknowledges_cancellation() -> None:
    db = _SpySession()
    state = {"stopping": False}

    async def snapshot_then_fail(*_args: Any, **_kwargs: Any) -> Any:
        # The user clicks Stop while the snapshot write is in flight:
        # request_run_cancellation moves QUEUED -> STOPPING.
        state["stopping"] = True
        raise RuntimeError("runtime snapshot store unavailable")

    async def cancel_poll(*_args: Any, **_kwargs: Any) -> bool:
        return state["stopping"]

    async def finalize(*_args: Any, **kwargs: Any) -> bool:
        # finalize_submission refuses FAILED over STOPPING (guarded UPDATE).
        return not (state["stopping"] and kwargs["status"] is JobStatus.FAILED)

    finalize_mock = AsyncMock(side_effect=finalize)
    with _graph_stream_env(
        db=db,
        graph=_ScriptedGraph(lambda _n: {}),
        cancel_poll=AsyncMock(side_effect=cancel_poll),
        finalize=finalize_mock,
        runtime_snapshot=snapshot_then_fail,
    ):
        frames = await _drain(_stream())

    statuses = [c.kwargs["status"] for c in finalize_mock.await_args_list]
    assert statuses == [JobStatus.FAILED, JobStatus.CANCELLED]
    cancelled = finalize_mock.await_args_list[-1].kwargs
    assert cancelled["event_type"] is RunEventType.RUN_CANCELLED
    assert cancelled["payload"]["reason"] == "user_requested"
    # The durable terminal is CANCELLED: like the done path, do not also
    # publish a terminal ERROR for a run the user stopped.
    assert not any(sse_event_name(frame) == "error" for frame in frames)


# ---------------------------------------------------------------------------
# R8-D8 — FAILED commits before the ERROR frame is exposed
# ---------------------------------------------------------------------------


async def test_graph_error_frame_follows_failed_commit() -> None:
    db = _SpySession()
    order: list[str] = []

    def pull(_n: int) -> dict[str, Any]:
        raise RuntimeError("graph exploded")

    async def finalize(*_args: Any, **kwargs: Any) -> bool:
        order.append(f"finalize:{kwargs['status'].value}")
        return True

    finalize_mock = AsyncMock(side_effect=finalize)

    def on_frame(frame: str) -> None:
        if sse_event_name(frame) == "error":
            order.append("frame:error")

    with _graph_stream_env(
        db=db,
        graph=_ScriptedGraph(pull),
        cancel_poll=AsyncMock(return_value=False),
        finalize=finalize_mock,
    ):
        await _drain(_stream(), on_frame)

    assert order == ["finalize:failed", "frame:error"]
    assert finalize_mock.await_args is not None
    failed = finalize_mock.await_args.kwargs
    assert failed["error_code"] == "stream_failed"
    assert failed["error"]


async def test_graph_error_frame_still_sent_when_failed_finalize_raises() -> None:
    db = _SpySession()

    def pull(_n: int) -> dict[str, Any]:
        raise RuntimeError("graph exploded")

    with _graph_stream_env(
        db=db,
        graph=_ScriptedGraph(pull),
        cancel_poll=AsyncMock(return_value=False),
        finalize=AsyncMock(side_effect=ConnectionError("db down")),
    ):
        frames = await _drain(_stream())

    assert sse_event_name(frames[-1]) == "error"


class _FailingLuna:
    async def astream(self, _messages: Any, *, config: Any = None) -> Any:
        yield AIMessageChunk(content="partial")
        raise RuntimeError("model stream broke")


async def test_luna_error_frame_follows_failed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import streaming as streaming_mod
    from src.core.config import get_settings
    from src.services.agent import llm_factory

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_MAX_INPUT_CHARS", 8_000)
    monkeypatch.setattr(llm_factory, "build_fast_path_llm", lambda: _FailingLuna())

    order: list[str] = []

    async def finalize(*_args: Any, **kwargs: Any) -> bool:
        order.append(f"finalize:{kwargs['status'].value}")
        return True

    def on_frame(frame: str) -> None:
        if sse_event_name(frame) == "error":
            order.append("frame:error")

    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": "Explain why rainbows form",
                "client_message_id": str(uuid.uuid4()),
            }
        ],
        page_context={"type": "chat"},
        use_rag=False,
        thread_id=str(THREAD_ID),
    )
    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=_SpySession()),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id=THREAD_ID), "c-1")),
        ),
        patch.object(
            streaming_mod,
            "accept_submission",
            new=AsyncMock(return_value=_acceptance()),
        ),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "is_run_cancellation_requested",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            streaming_mod, "_finalize_run", new=AsyncMock(side_effect=finalize)
        ),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value="partial-row"),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value="stream-1"),
        ),
        patch.object(streaming_mod._stream_buffer, "append", new=AsyncMock()),
        patch.object(streaming_mod._stream_buffer, "finish_stream", new=AsyncMock()),
    ):
        await _drain(_stream(body), on_frame)

    assert order == ["finalize:failed", "frame:error"]


# ---------------------------------------------------------------------------
# R8-C5 — /stream heartbeats the run while the graph runs
# ---------------------------------------------------------------------------


class _BeatSession:
    async def __aenter__(self) -> _BeatSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_stream_graph_phase_heartbeats_the_run() -> None:
    from src.api.agent import streaming as streaming_mod
    from src.services.agent import agent_execution_service

    touched: list[str] = []

    async def touch(_session: Any, job_id: str) -> bool:
        touched.append(job_id)
        return True

    async def slow_pull(n: int) -> dict[str, Any]:
        if n > 5:
            raise StopAsyncIteration
        await asyncio.sleep(0.03)
        return _token_event("x")

    fast_heartbeat = functools.partial(
        agent_execution_service._run_heartbeat, interval_seconds=0.01
    )
    with (
        _graph_stream_env(
            db=_SpySession(),
            graph=_ScriptedGraph(slow_pull),
            cancel_poll=AsyncMock(return_value=False),
            finalize=AsyncMock(return_value=True),
        ),
        patch.object(streaming_mod._jobs_mod, "_run_heartbeat", new=fast_heartbeat),
        patch.object(agent_execution_service, "AsyncSessionLocal", new=_BeatSession),
        patch("src.services.agent.agent_run_service.touch_run_updated_at", new=touch),
    ):
        frames = await _drain(_stream())

    assert sse_event_name(frames[-1]) == "done"
    assert touched and set(touched) == {RUN_ID}, (
        "the live /stream run never refreshed agent_runs.updated_at, so the "
        "stale-run sweeper cannot tell it from a dead one"
    )
