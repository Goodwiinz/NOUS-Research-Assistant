"""``accepted`` on ``/stream`` must imply committed state (P0-C).

Before this change the SSE generator emitted ``status/accepted`` as its very
first line, before a single row existed. A crash anywhere after it left the
client holding an acknowledgment for a run the system had no record of — the
repo's dominant "fake-success" shape, at the protocol level.

These tests drive the REAL ``stream_event_generator`` against a SQLite database
carrying the real tables, and fault-inject at every statement boundary inside
the accept transaction. The contract asserted in all cases: no ``accepted``
frame, an ``error`` frame instead, and not one surviving row.

Note on libpq: the generator lazily imports ``psycopg``; this host may have no
libpq, so a stub is installed the same way ``test_agent_stream_accepted_latency``
does (see nous-libpq-test-env).
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.chat_message import ChatMessage
from src.models.thread import Thread
from src.shared.enums import AgentErrorCategory, AgentOutboxStatus, JobStatus
from tests.utils.agent_stream import frames_of_type, make_stream_request, sse_data


def _install_psycopg_stub() -> None:
    if "psycopg" in sys.modules:
        return
    stub = types.ModuleType("psycopg")
    stub.OperationalError = OSError  # type: ignore[attr-defined]
    sys.modules["psycopg"] = stub


_install_psycopg_stub()

ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
THREAD_B_ID = uuid.UUID("55555555-5555-5555-5555-555555555556")
CONVERSATION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")

# Every statement boundary inside the accept transaction, plus the commit
# itself. The parametrization is the point: "accepted implies committed" has to
# hold no matter where the transaction dies, not just at a convenient spot.
_BOUNDARIES = ["user_message", "run", "event", "outbox", "commit"]


class _FakeGraph:
    """Minimal graph: one user-visible token, then a clean finish."""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error
        self.consumed = False

    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.consumed = True
        if self._error is not None:
            raise self._error
        yield {
            "event": "on_chat_model_stream",
            "name": "llm",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {"chunk": SimpleNamespace(content="hi")},
        }

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(
            values={"messages": [SimpleNamespace(type="ai", content="hi")]},
            tasks=(),
        )

    async def aupdate_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _InterruptGraph:
    """Emit a public reasoning prefix, then propagate a persisted interrupt."""

    def __init__(self, summary: str) -> None:
        self.summary = summary

    async def astream_events(
        self, *_args: Any, **_kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        yield {
            "event": "on_chat_model_stream",
            "name": "llm",
            "metadata": {"langgraph_node": "llm_node"},
            "data": {
                "chunk": SimpleNamespace(
                    content=[
                        {
                            "type": "reasoning",
                            "summary": [{"type": "summary_text", "text": self.summary}],
                        }
                    ]
                )
            },
        }
        raise GraphInterrupt(
            (Interrupt(value={"message": "Confirm this action"}, id="interrupt-1"),)
        )

    async def aget_state(self, _config: Any) -> Any:
        return SimpleNamespace(
            values={"messages": []},
            tasks=(
                SimpleNamespace(
                    interrupts=(
                        SimpleNamespace(
                            value={"message": "Confirm this action"},
                        ),
                    )
                ),
            ),
        )

    async def aupdate_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _AdvancingClock:
    """Deterministic monotonic clock advanced by the patched replay sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now = round(self.now + delay, 10)


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")

    # SQLAlchemy's documented sqlite caveat: without this, SAVEPOINT usage
    # inside the accept transaction implicitly COMMITs it, and the rollback
    # assertions below would pass vacuously.
    @event.listens_for(engine.sync_engine, "connect")
    def _disable_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(ChatMessage.__table__.create)
        await conn.run_sync(AgentRun.__table__.create)
        await conn.run_sync(AgentRunEvent.__table__.create)
        await conn.run_sync(AgentOutbox.__table__.create)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        setup.add_all(
            [
                Thread(
                    id=thread_id,
                    conversation_id=CONVERSATION_ID,
                    title="t",
                    created_by_id=USER_ID,
                    message_count=0,
                )
                for thread_id in (THREAD_ID, THREAD_B_ID)
            ]
        )
        await setup.commit()
    yield factory
    await engine.dispose()


def _thread_row(thread_id: uuid.UUID = THREAD_ID) -> Any:
    """What ``_resolve_thread`` hands back: an ownership-verified thread."""
    return SimpleNamespace(id=thread_id, conversation_id=CONVERSATION_ID)


async def _drive_stream(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    extra_patches: tuple[Any, ...] = (),
    graph: Any | None = None,
    resolve_thread_error: BaseException | None = None,
    thread_id: uuid.UUID = THREAD_ID,
    client_message_id: uuid.UUID | None = None,
    request: Any | None = None,
) -> list[str]:
    from src.api.agent import streaming as streaming_mod

    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": "hello",
                "client_message_id": str(client_message_id or uuid.uuid4()),
            }
        ],
        thread_id=str(thread_id),
    )
    request = request or SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current_user = SimpleNamespace(id=USER_ID, organization_id=ORG_ID)
    graph = graph or _FakeGraph()
    resolve_thread = (
        AsyncMock(side_effect=resolve_thread_error)
        if resolve_thread_error is not None
        else AsyncMock(return_value=(_thread_row(thread_id), str(CONVERSATION_ID)))
    )

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", session_factory),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=resolve_thread,
        ),
        patch.object(
            streaming_mod, "_resolve_and_bind_project", new=AsyncMock(return_value=None)
        ),
        patch(
            "src.services.agent.observability.configure_langsmith", return_value=None
        ),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.graph.compile_agent_graph",
            new=lambda **_kwargs: graph,
        ),
        patch(
            "src.services.agent.agent_execution_service._persist_assistant_message_safe",
            new=AsyncMock(return_value=str(uuid.uuid4())),
        ),
    ):
        with _nested(extra_patches):
            return [
                frame
                async for frame in streaming_mod.stream_event_generator(
                    body,
                    request,
                    current_user,  # type: ignore[arg-type]  # duck-typed User
                )
            ]


class _nested:
    """Apply a tuple of already-built patchers as one context."""

    def __init__(self, patchers: tuple[Any, ...]) -> None:
        self._patchers = patchers

    def __enter__(self) -> None:
        for patcher in self._patchers:
            patcher.start()

    def __exit__(self, *_exc: Any) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()


def _accepted_frames(frames: list[str]) -> list[dict[str, Any]]:
    return [
        data
        for data in (sse_data(frame) for frame in frames_of_type(frames, "status"))
        if data.get("phase") == "accepted"
    ]


async def _count(db: AsyncSession, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _accept_without_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: uuid.UUID,
    client_message_id: uuid.UUID,
) -> str:
    from src.services.agent.agent_submission_service import accept_submission

    body = make_stream_request(
        messages=[
            {
                "role": "user",
                "content": "hello",
                "client_message_id": str(client_message_id),
            }
        ],
        thread_id=str(thread_id),
    )
    async with session_factory() as db:
        accepted = await accept_submission(
            db,
            current_user=SimpleNamespace(id=USER_ID, organization_id=ORG_ID),
            request=body,
            thread=_thread_row(thread_id),
        )
    return accepted.run_id


def _boundary_patcher(boundary: str) -> Any:
    """A patcher that breaks exactly one statement of the accept transaction."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"injected failure at {boundary}")

    if boundary == "commit":
        return patch.object(_AsyncSession, "commit", new=boom)
    target = {
        "user_message": "_insert_user_message",
        "run": "_insert_run",
        "event": "append_event",
        "outbox": "_insert_outbox",
    }[boundary]
    return patch(
        f"src.services.agent.agent_submission_service.{target}",
        new=boom,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", _BOUNDARIES)
async def test_stream_accepted_implies_committed(
    session_factory: async_sessionmaker[AsyncSession], boundary: str
) -> None:
    """Break the accept transaction anywhere: no acknowledgment, no rows."""
    frames = await _drive_stream(
        session_factory, extra_patches=(_boundary_patcher(boundary),)
    )

    assert not _accepted_frames(frames), (
        f"an `accepted` frame was emitted although the accept transaction "
        f"failed at the {boundary} boundary — the client would be holding an "
        f"acknowledgment for a run that does not exist"
    )
    assert frames_of_type(
        frames, "error"
    ), f"a failed accept ({boundary}) must surface as an error frame: {frames}"

    async with session_factory() as verify:
        assert await _count(verify, ChatMessage) == 0
        assert await _count(verify, AgentRun) == 0
        assert await _count(verify, AgentRunEvent) == 0
        assert await _count(verify, AgentOutbox) == 0
        thread = await verify.get(Thread, THREAD_ID)
        assert thread is not None and thread.message_count == 0


@pytest.mark.asyncio
async def test_thread_resolution_failure_is_not_accepted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    frames = await _drive_stream(
        session_factory,
        resolve_thread_error=ValueError("malformed thread id"),
    )

    assert not _accepted_frames(frames)
    assert frames_of_type(frames, "error")
    async with session_factory() as verify:
        assert await _count(verify, ChatMessage) == 0
        assert await _count(verify, AgentRun) == 0


@pytest.mark.asyncio
async def test_stream_failure_closes_the_durable_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    frames = await _drive_stream(
        session_factory, graph=_FakeGraph(RuntimeError("boom"))
    )

    assert frames_of_type(frames, "error")
    async with session_factory() as verify:
        run = (await verify.execute(select(AgentRun))).scalar_one()
        assert run.status == JobStatus.FAILED.value
        failed = (
            await verify.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run.job_id,
                    AgentRunEvent.event_type == "run.failed",
                )
            )
        ).scalar_one()
        assert set(failed.payload) == {"code", "message"}


@pytest.mark.asyncio
async def test_unsaved_interrupt_closes_the_durable_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    interrupt = GraphInterrupt(
        (Interrupt(value={"message": "confirm"}, id="interrupt-1"),)
    )
    frames = await _drive_stream(session_factory, graph=_FakeGraph(interrupt))

    assert frames_of_type(frames, "error")
    async with session_factory() as verify:
        run = (await verify.execute(select(AgentRun))).scalar_one()
        assert run.status == JobStatus.FAILED.value
        failed = (
            await verify.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run.job_id,
                    AgentRunEvent.event_type == "run.failed",
                )
            )
        ).scalar_one()
        assert failed.payload["code"] == "interrupt_not_checkpointed"
        assert set(failed.payload) == {"code", "message"}


@pytest.mark.asyncio
async def test_propagated_interrupt_parks_public_reasoning_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    summary = "I compared the evidence before asking for approval."
    frames = await _drive_stream(
        session_factory,
        graph=_InterruptGraph(summary),
    )

    assert frames_of_type(frames, "confirmation")
    async with session_factory() as verify:
        run = (await verify.execute(select(AgentRun))).scalar_one()
        assert run.status == JobStatus.AWAITING_CONFIRMATION.value
        assert run.run_metadata["reasoning_summary"] == summary
        assert run.run_metadata["progress_steps"]


@pytest.mark.asyncio
async def test_accepted_carries_committed_run_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The run id on the accepted frame must already exist in ``agent_runs``."""
    frames = await _drive_stream(session_factory)

    accepted = _accepted_frames(frames)
    assert len(accepted) == 1, f"expected exactly one accepted frame: {frames}"
    run_id = accepted[0]["run_id"]
    assert run_id, "the accepted frame must carry the committed run id"

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None, "accepted named a run that is not in agent_runs"
        assert run.thread_id == THREAD_ID
        assert run.organization_id == ORG_ID

        # The rest of the accept transaction landed with it.
        assert await _count(verify, ChatMessage) == 1
        assert await _count(verify, AgentOutbox) == 1
        outbox = (await verify.execute(select(AgentOutbox))).scalar_one()
        assert outbox.run_id == run_id
        # Dispatch happened in-process right after the commit, so the record is
        # closed out rather than left for a relay that does not exist yet.
        assert outbox.status == AgentOutboxStatus.DISPATCHED.value

        seqs = (
            await verify.execute(
                select(AgentRunEvent.seq, AgentRunEvent.event_type)
                .where(AgentRunEvent.run_id == run_id)
                .order_by(AgentRunEvent.seq)
            )
        ).all()
        assert seqs[0] == (1, "run.created"), "run.created is seq 1"
        assert run.status == JobStatus.COMPLETED.value
        assert [row[1] for row in seqs][-1] == "run.completed"


@pytest.mark.asyncio
async def test_accepted_frame_precedes_every_other_frame(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Moving accept before the acknowledgment must not reorder the stream."""
    frames = await _drive_stream(session_factory)
    first = sse_data(frames[0])
    assert first.get("phase") == "accepted"
    assert first["schema_version"] == "1.0"
    assert first["sequence"] == 1
    assert frames[0].startswith("id: 1\n"), "the resume cursor line must survive"


@pytest.mark.asyncio
async def test_cross_thread_retry_never_replays_the_original_threads_frames(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One user's repeated cmid must not attach thread B to thread A's run."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )
    replay_calls: list[tuple[str, str]] = []

    async def leaking_replay(
        _request: Any,
        *,
        thread_id: str,
        stream_id: str,
        wait_for_start: bool,
    ) -> AsyncIterator[str]:
        replay_calls.append((thread_id, stream_id))
        yield 'id: 1\nevent: token\ndata: {"content": "thread-a-secret"}\n\n'

    frames = await _drive_stream(
        session_factory,
        thread_id=THREAD_B_ID,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=AsyncMock(return_value="thread-a-stream"),
            ),
            patch.object(streaming_mod, "replay_buffered_stream", new=leaking_replay),
        ),
    )

    errors = frames_of_type(frames, "error")
    assert errors
    assert sse_data(errors[0])["category"] == AgentErrorCategory.CONFLICT.value
    assert not frames_of_type(frames, "token")
    assert replay_calls == []

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.thread_id == THREAD_ID
        assert run.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_retry_waits_for_original_stream_startup_before_terminalizing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A first empty buffer lookup must not kill a dispatch racing startup."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )
    stream_lookup = AsyncMock(side_effect=[None, "started-stream"])
    clock = _AdvancingClock()
    sleep = AsyncMock(side_effect=clock.sleep)
    replay_calls: list[tuple[str, str, bool]] = []

    async def replay_started_stream(
        _request: Any,
        *,
        thread_id: str,
        stream_id: str,
        wait_for_start: bool,
    ) -> AsyncIterator[str]:
        replay_calls.append((thread_id, stream_id, wait_for_start))
        yield 'id: 1\nevent: token\ndata: {"content": "started"}\n\n'
        yield "id: 2\nevent: done\ndata: {}\n\n"

    frames = await _drive_stream(
        session_factory,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=stream_lookup,
            ),
            patch.object(streaming_mod.asyncio, "sleep", new=sleep),
            patch.object(streaming_mod.time, "monotonic", new=clock.monotonic),
            patch.object(
                streaming_mod,
                "replay_buffered_stream",
                new=replay_started_stream,
            ),
        ),
    )

    assert not frames_of_type(frames, "error")
    assert len(frames_of_type(frames, "token")) == 1
    assert len(frames_of_type(frames, "done")) == 1
    assert replay_calls == [(str(THREAD_ID), "started-stream", True)]
    sleep.assert_awaited_once_with(0.1)

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.status == JobStatus.QUEUED.value
        failed_count = int(
            (
                await verify.execute(
                    select(func.count())
                    .select_from(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type == "run.failed",
                    )
                )
            ).scalar_one()
        )
        assert failed_count == 0


@pytest.mark.asyncio
async def test_retry_does_not_fail_run_that_started_during_grace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The post-grace CAS must lose to a concurrent QUEUED-to-RUNNING update."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )
    lookup_count = 0
    clock = _AdvancingClock()

    async def missing_buffer_while_dispatch_starts(_run_id: str) -> None:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 2:
            async with session_factory() as dispatch_db:
                run = await dispatch_db.get(AgentRun, run_id)
                assert run is not None
                run.status = JobStatus.RUNNING.value
                await dispatch_db.commit()
        return None

    frames = await _drive_stream(
        session_factory,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=missing_buffer_while_dispatch_starts,
            ),
            patch.object(
                streaming_mod.asyncio,
                "sleep",
                new=AsyncMock(side_effect=clock.sleep),
            ),
            patch.object(streaming_mod.time, "monotonic", new=clock.monotonic),
        ),
    )

    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    error = sse_data(errors[0])
    assert error["category"] == AgentErrorCategory.CONFLICT.value
    assert error["error"] == "The original response stream is unavailable."

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.status == JobStatus.RUNNING.value
        failed_count = int(
            (
                await verify.execute(
                    select(func.count())
                    .select_from(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type == "run.failed",
                    )
                )
            ).scalar_one()
        )
        assert failed_count == 0


@pytest.mark.asyncio
async def test_retry_disconnect_never_cancels_original_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replay is an observer, so its disconnect cannot own cancellation."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )
    disconnect_signal = asyncio.Event()
    request = SimpleNamespace(
        state=SimpleNamespace(agent_disconnect_event=disconnect_signal),
        is_disconnected=AsyncMock(return_value=False),
    )

    async def disconnect_during_lookup(_run_id: str) -> None:
        disconnect_signal.set()
        await asyncio.sleep(1)

    with pytest.raises(asyncio.CancelledError):
        await _drive_stream(
            session_factory,
            client_message_id=client_message_id,
            request=request,
            extra_patches=(
                patch.object(
                    streaming_mod._stream_buffer,
                    "stream_id_for_run",
                    new=disconnect_during_lookup,
                ),
            ),
        )

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.status == JobStatus.QUEUED.value
        terminal_count = int(
            (
                await verify.execute(
                    select(func.count())
                    .select_from(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type.in_(
                            ("run.failed", "run.cancelled", "run.completed")
                        ),
                    )
                )
            ).scalar_one()
        )
        assert terminal_count == 0


@pytest.mark.asyncio
async def test_retry_lookup_error_never_fails_original_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replay infrastructure failure must not transfer run ownership."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )

    frames = await _drive_stream(
        session_factory,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=AsyncMock(side_effect=RuntimeError("redis unavailable")),
            ),
        ),
    )

    assert len(frames_of_type(frames, "error")) == 1
    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.status == JobStatus.QUEUED.value
        terminal_count = int(
            (
                await verify.execute(
                    select(func.count())
                    .select_from(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run_id,
                        AgentRunEvent.event_type.in_(
                            ("run.failed", "run.cancelled", "run.completed")
                        ),
                    )
                )
            ).scalar_one()
        )
        assert terminal_count == 0


@pytest.mark.asyncio
async def test_original_never_executes_after_abandonment_claim_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A delayed original must stop when replay cleanup owns terminalization."""
    from src.api.agent import streaming as streaming_mod
    from src.services.agent.agent_submission_service import fail_queued_submission

    graph = _FakeGraph()
    client_message_id = uuid.uuid4()
    stream_id = "lost-claim-stream"
    buffered_frames: list[str] = []

    async def record_frame(_stream_id: str, _seq: int, frame: str) -> None:
        buffered_frames.append(frame)

    async def abandon_instead_of_dispatch(
        db: AsyncSession,
        *,
        run_id: str,
        organization_id: Any,
        user_id: Any,
        **_kwargs: Any,
    ) -> bool:
        message = "The original response did not start. Please retry."
        abandoned = await fail_queued_submission(
            db,
            run_id=run_id,
            thread_id=THREAD_ID,
            organization_id=organization_id,
            user_id=user_id,
            error_code="stream_not_dispatched",
            error=message,
            payload={"code": "stream_not_dispatched", "message": message},
        )
        assert abandoned
        return False

    frames = await _drive_stream(
        session_factory,
        graph=graph,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "start_stream",
                new=AsyncMock(return_value=stream_id),
            ),
            patch.object(
                streaming_mod._stream_buffer,
                "append",
                new=record_frame,
            ),
            patch.object(
                streaming_mod._stream_buffer,
                "finish_stream",
                new=AsyncMock(),
            ),
            patch.object(
                streaming_mod,
                "mark_submission_dispatched",
                new=abandon_instead_of_dispatch,
            ),
        ),
    )

    assert not graph.consumed
    assert not frames_of_type(frames, "token")
    assert not frames_of_type(frames, "done")
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    error = sse_data(errors[0])
    assert error["category"] == AgentErrorCategory.CONFLICT.value
    assert error["error"] == "This response is no longer active. Please retry."

    async with session_factory() as verify:
        run = (await verify.execute(select(AgentRun))).scalar_one()
        assert run.status == JobStatus.FAILED.value
        failed_count = int(
            (
                await verify.execute(
                    select(func.count())
                    .select_from(AgentRunEvent)
                    .where(
                        AgentRunEvent.run_id == run.job_id,
                        AgentRunEvent.event_type == "run.failed",
                    )
                )
            ).scalar_one()
        )
        assert failed_count == 1

    async def replay_recorded_frames(
        _request: Any,
        *,
        thread_id: str,
        stream_id: str,
        wait_for_start: bool,
    ) -> AsyncIterator[str]:
        assert thread_id == str(THREAD_ID)
        assert stream_id == "lost-claim-stream"
        assert wait_for_start is True
        for frame in buffered_frames:
            yield frame

    replayed_frames = await _drive_stream(
        session_factory,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=AsyncMock(return_value=stream_id),
            ),
            patch.object(
                streaming_mod,
                "replay_buffered_stream",
                new=replay_recorded_frames,
            ),
        ),
    )
    replayed_errors = frames_of_type(replayed_frames, "error")
    assert len(replayed_errors) == 1
    assert (
        sse_data(replayed_errors[0])["error"]
        == "This response is no longer active. Please retry."
    )


@pytest.mark.asyncio
async def test_undispatched_retry_fails_after_grace_and_releases_the_thread_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A committed run still missing its buffer after grace is terminalized."""
    from src.api.agent import streaming as streaming_mod

    client_message_id = uuid.uuid4()
    run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=client_message_id,
    )
    stream_lookup = AsyncMock(return_value=None)
    clock = _AdvancingClock()
    sleep = AsyncMock(side_effect=clock.sleep)

    frames = await _drive_stream(
        session_factory,
        client_message_id=client_message_id,
        extra_patches=(
            patch.object(
                streaming_mod._stream_buffer,
                "stream_id_for_run",
                new=stream_lookup,
            ),
            patch.object(streaming_mod.asyncio, "sleep", new=sleep),
            patch.object(streaming_mod.time, "monotonic", new=clock.monotonic),
        ),
    )

    assert stream_lookup.await_count == 51
    assert sleep.await_count == 50
    errors = frames_of_type(frames, "error")
    assert len(errors) == 1
    error = sse_data(errors[0])
    assert error["category"] == AgentErrorCategory.CONFLICT.value
    assert error["error"] == "The original response did not start. Please retry."

    async with session_factory() as verify:
        run = await verify.get(AgentRun, run_id)
        assert run is not None
        assert run.status == JobStatus.FAILED.value
        failed = (
            await verify.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_type == "run.failed",
                )
            )
        ).scalar_one()
        assert failed.payload["code"] == "stream_not_dispatched"
        outbox = (
            await verify.execute(
                select(AgentOutbox).where(AgentOutbox.run_id == run_id)
            )
        ).scalar_one()
        assert outbox.status == AgentOutboxStatus.FAILED.value

    next_run_id = await _accept_without_dispatch(
        session_factory,
        thread_id=THREAD_ID,
        client_message_id=uuid.uuid4(),
    )
    assert next_run_id != run_id
