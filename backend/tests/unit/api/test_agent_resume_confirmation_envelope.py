"""``GET /stream/resume`` re-delivers a parked HITL confirmation.

That frame used to be hand-built as ``event: …\\ndata: {…}`` with no ``id:``
line and none of the envelope fields every live-stream frame carries. Two
consequences: an EventSource client's Last-Event-ID never advanced past it (a
reconnect then replayed from a stale cursor), and the payload was the only
agent SSE frame on the wire without schema_version / sequence / event_id /
trace_id / route — so any consumer keyed on the envelope had to special-case
it. The single-frame response also skipped ``_SSE_HEADERS``, leaving proxy
buffering (``X-Accel-Buffering: no``) unset for the one frame that matters.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from tests.utils.agent_stream import sse_data, sse_event_name, sse_seq

_CONFIRMATION = {
    "tool_name": "create_note",
    "tool_args": {"title": "parked"},
    "message": "Create this note?",
}


def _snapshot_with_interrupt(
    value: dict, *, user_id: Optional[str] = None
) -> SimpleNamespace:
    """A checkpoint snapshot parked on a single ``interrupt()``."""
    return SimpleNamespace(
        values={} if user_id is None else {"user_id": user_id},
        tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value=value),)),),
    )


def _graph_patches(snapshot: Any) -> list:
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    return [
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
    ]


@pytest.mark.asyncio
async def test_resume_confirmation_frame_carries_envelope() -> None:
    """The re-delivered frame must be a full envelope with an ``id:`` line."""
    from src.api.agent.execute import _pending_confirmation_frame
    from src.api.agent.streaming import AGENT_STREAM_SCHEMA_VERSION

    thread_id = str(_uuid.uuid4())
    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(_snapshot_with_interrupt(_CONFIRMATION))

    # Called exactly as the pre-fix resume path called it, so this test fails
    # on the envelope contract rather than on a changed signature.
    with patches[0], patches[1], patches[2]:
        frame = await _pending_confirmation_frame(thread_id, current_user)

    assert frame is not None, "a parked interrupt must produce a frame"
    assert sse_event_name(frame) == "confirmation"

    payload = sse_data(frame)
    # The original payload survives the envelope.
    assert payload["thread_id"] == thread_id
    assert payload["confirmation"] == _CONFIRMATION

    # Envelope fields — same set every live-stream frame carries.
    assert payload["schema_version"] == AGENT_STREAM_SCHEMA_VERSION
    assert payload["route"] == "graph"
    assert payload["occurred_at"].endswith("Z")
    assert payload["trace_id"]
    assert payload["event_id"] == f"{payload['trace_id']}:{payload['sequence']}"

    # The id: line is what becomes Last-Event-ID; it must match the envelope's
    # own sequence and continue past the cursor the client sent.
    assert sse_seq(frame) == payload["sequence"], (
        "frame has no id: line matching its sequence — a client resuming from "
        f"this frame cannot advance its cursor: {frame!r}"
    )


@pytest.mark.asyncio
async def test_resume_confirmation_seq_continues_from_client_cursor() -> None:
    """Echoing this frame's id back as Last-Event-ID must move the cursor on."""
    from src.api.agent.execute import _pending_confirmation_frame

    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(_snapshot_with_interrupt(_CONFIRMATION))

    with patches[0], patches[1], patches[2]:
        frame = await _pending_confirmation_frame(
            str(_uuid.uuid4()), current_user, after=7
        )

    assert frame is not None
    assert sse_seq(frame) == 8
    assert sse_data(frame)["sequence"] == 8


@pytest.mark.asyncio
async def test_resume_confirmation_carries_durable_run_id_after_reload() -> None:
    """A cold-load confirmation must retain the exact Stop identity."""
    from src.api.agent import execute as execute_mod
    from src.api.agent.execute import _pending_confirmation_frame

    thread_id = str(_uuid.uuid4())
    run_id = str(_uuid.uuid4())
    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(
        _snapshot_with_interrupt(_CONFIRMATION, user_id=current_user.id)
    )
    db = object()

    with (
        patches[0],
        patches[1],
        patches[2],
        patch.object(
            execute_mod,
            "get_active_run_for_thread",
            new=AsyncMock(return_value=SimpleNamespace(job_id=run_id)),
        ),
    ):
        frame = await _pending_confirmation_frame(thread_id, current_user, db=db)  # type: ignore[arg-type]

    assert frame is not None
    assert sse_data(frame)["run_id"] == run_id


@pytest.mark.asyncio
async def test_resume_confirmation_does_not_resurrect_acknowledged_stop() -> None:
    """A stale checkpoint cannot re-arm HITL after a terminal run ACK."""
    from src.api.agent import execute as execute_mod
    from src.api.agent.execute import _pending_confirmation_frame

    thread_id = str(_uuid.uuid4())
    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(_snapshot_with_interrupt(_CONFIRMATION))

    with (
        patches[0],
        patches[1],
        patches[2],
        patch.object(
            execute_mod,
            "get_active_run_for_thread",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod,
            "get_latest_run_for_thread",
            new=AsyncMock(return_value=SimpleNamespace(status="cancelled")),
        ),
    ):
        frame = await _pending_confirmation_frame(
            thread_id,
            current_user,
            db=object(),  # type: ignore[arg-type]
        )

    assert frame is None


@pytest.mark.asyncio
async def test_resume_confirmation_does_not_resurrect_stopping_run() -> None:
    """A pending Stop claim suppresses the old checkpoint before ACK."""
    from src.api.agent import execute as execute_mod
    from src.api.agent.execute import _pending_confirmation_frame
    from src.shared.enums import JobStatus

    thread_id = str(_uuid.uuid4())
    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(_snapshot_with_interrupt(_CONFIRMATION))

    with (
        patches[0],
        patches[1],
        patches[2],
        patch.object(
            execute_mod,
            "get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(status=JobStatus.STOPPING.value)
            ),
        ),
    ):
        frame = await _pending_confirmation_frame(
            thread_id,
            current_user,
            db=object(),  # type: ignore[arg-type]
        )

    assert frame is None


@pytest.mark.asyncio
async def test_resume_confirmation_response_uses_sse_headers() -> None:
    """The single-frame resume response must carry the shared SSE headers."""
    from src.api.agent import execute as execute_mod
    from src.api.agent.streaming import _SSE_HEADERS

    thread_id = str(_uuid.uuid4())
    current_user = Mock(id="user-1", organization_id="org-1")

    class _FakeResult:
        def scalar_one_or_none(self) -> object:
            return object()  # ownership check passes

        def scalars(self) -> SimpleNamespace:
            workspace = SimpleNamespace(
                is_deleted=False,
                is_public=False,
                owner_id=current_user.id,
                is_member=lambda _user_id: True,
                can_user_edit=lambda _user_id: True,
            )
            thread = SimpleNamespace(
                id=thread_id,
                conversation_id="conversation-1",
                is_deleted=False,
                conversation=SimpleNamespace(
                    id="conversation-1",
                    is_deleted=False,
                    workspace=workspace,
                ),
            )
            return SimpleNamespace(first=lambda: thread)

    class _FakeDB:
        async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
            return _FakeResult()

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    frame = "id: 1\nevent: confirmation\ndata: {}\n\n"

    with (
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod,
            "_pending_confirmation_frame",
            new=AsyncMock(return_value=frame),
        ),
    ):
        response = await execute_mod.resume_stream(
            request,  # type: ignore[arg-type]
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=current_user,
            db=_FakeDB(),  # type: ignore[arg-type]
        )

    for header, value in _SSE_HEADERS.items():
        assert response.headers.get(header) == value, (
            f"resume confirmation response is missing {header}: "
            f"{dict(response.headers)!r}"
        )


@pytest.mark.asyncio
async def test_resume_confirmation_frame_absent_without_interrupt() -> None:
    """No parked interrupt → None (resume still degrades to its 204)."""
    from src.api.agent.execute import _pending_confirmation_frame

    current_user = Mock(id="user-1", organization_id="org-1")
    patches = _graph_patches(SimpleNamespace(values={}, tasks=()))

    with patches[0], patches[1], patches[2]:
        frame: Optional[str] = await _pending_confirmation_frame(
            str(_uuid.uuid4()), current_user
        )

    assert frame is None


async def _resume_with_checkpoint(snapshot: SimpleNamespace, current_user: Mock) -> Any:
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    db = _resume_db()
    graph_patches = _graph_patches(snapshot)
    with (
        graph_patches[0],
        graph_patches[1],
        graph_patches[2],
        patch.object(
            execute_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation")),
        ),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod,
            "get_active_run_for_thread",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod,
            "get_latest_run_for_thread",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )


@pytest.mark.asyncio
async def test_resume_does_not_redeliver_foreign_checkpoint_to_editable_member() -> (
    None
):
    """Editable thread access must not expose another user's parked HITL."""
    current_user = Mock(id="editor-b", organization_id="org-1")
    response = await _resume_with_checkpoint(
        _snapshot_with_interrupt(_CONFIRMATION, user_id="editor-a"), current_user
    )

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_resume_redelivers_checkpoint_to_its_owner() -> None:
    """The checkpoint owner can still recover a parked confirmation."""
    current_user = Mock(id="editor-a", organization_id="org-1")
    response = await _resume_with_checkpoint(
        _snapshot_with_interrupt(_CONFIRMATION, user_id=current_user.id),
        current_user,
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_resume_denies_checkpoint_without_owner_identity() -> None:
    """A checkpoint without durable ownership is fail-closed on DB-backed resume."""
    current_user = Mock(id="editor-a", organization_id="org-1")
    response = await _resume_with_checkpoint(
        _snapshot_with_interrupt(_CONFIRMATION), current_user
    )

    assert response.status_code == 204


def _resume_db() -> AsyncMock:
    """DB double whose legacy owner-only query denies an editor.

    The canonical editable-thread resolver is patched in the allow cases. If
    the route regresses to its former Workspace.owner_id predicate, these
    tests therefore fail with 404 before reaching the stream/run assertions.
    """
    db = AsyncMock()
    db.execute.return_value = Mock(
        scalar_one_or_none=Mock(return_value=None),
    )
    return db


def _resume_request() -> SimpleNamespace:
    return SimpleNamespace(is_disconnected=AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_resume_allows_editable_member_with_caller_owned_active_stream() -> None:
    """An editor can reconnect to its own active stream through the canonical gate."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    stream_id = str(_uuid.uuid4())
    run_id = str(_uuid.uuid4())
    current_user = Mock(id="editor-1", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    active = AsyncMock(return_value=SimpleNamespace(job_id=run_id, status="running"))
    run_stream = AsyncMock(return_value=stream_id)

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=stream_id),
        ),
        patch.object(execute_mod._stream_buffer, "stream_id_for_run", new=run_stream),
        patch.object(execute_mod, "get_active_run_for_thread", new=active),
    ):
        response = await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert response.status_code == 200
    resolve.assert_awaited_once()
    active.assert_awaited_once_with(
        db,
        _uuid.UUID(thread_id),
        organization_id="org-1",
        user_id="editor-1",
    )
    run_stream.assert_awaited_once_with(run_id)


@pytest.mark.asyncio
async def test_resume_finished_stream_requires_caller_owned_run_mapping() -> None:
    """After active-pointer cleanup, replay still needs caller/run correlation."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    stream_id = str(_uuid.uuid4())
    run_id = str(_uuid.uuid4())
    current_user = Mock(id="admin-1", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    latest = AsyncMock(return_value=SimpleNamespace(job_id=run_id, status="completed"))
    thread_for_stream = AsyncMock(return_value=thread_id)
    run_stream = AsyncMock(return_value=stream_id)

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod._stream_buffer, "thread_id_for_stream", new=thread_for_stream
        ),
        patch.object(execute_mod._stream_buffer, "stream_id_for_run", new=run_stream),
        patch.object(execute_mod, "get_latest_run_for_thread", new=latest),
    ):
        response = await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=3,
            stream=stream_id,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert response.status_code == 200
    latest.assert_awaited_once_with(
        db,
        _uuid.UUID(thread_id),
        organization_id="org-1",
        user_id="admin-1",
    )
    thread_for_stream.assert_awaited_once_with(stream_id)
    run_stream.assert_awaited_once_with(run_id)


@pytest.mark.asyncio
async def test_resume_returns_204_when_active_caller_run_maps_to_another_stream() -> (
    None
):
    """A caller-owned run still cannot authorize a mismatched stream id."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    stream_id = str(_uuid.uuid4())
    current_user = Mock(id="editor-1", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    active = AsyncMock(
        return_value=SimpleNamespace(job_id="caller-run", status="running")
    )
    run_stream = AsyncMock(return_value=str(_uuid.uuid4()))

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=stream_id),
        ),
        patch.object(execute_mod._stream_buffer, "stream_id_for_run", new=run_stream),
        patch.object(execute_mod, "get_active_run_for_thread", new=active),
    ):
        response = await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert response.status_code == 204
    run_stream.assert_awaited_once_with("caller-run")


@pytest.mark.asyncio
async def test_resume_returns_204_when_latest_caller_run_maps_to_another_stream() -> (
    None
):
    """A finished replay must match both caller ownership and run identity."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    stream_id = str(_uuid.uuid4())
    current_user = Mock(id="admin-1", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    latest = AsyncMock(
        return_value=SimpleNamespace(job_id="caller-run", status="completed")
    )
    run_stream = AsyncMock(return_value=str(_uuid.uuid4()))

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod._stream_buffer,
            "thread_id_for_stream",
            new=AsyncMock(return_value=thread_id),
        ),
        patch.object(execute_mod._stream_buffer, "stream_id_for_run", new=run_stream),
        patch.object(execute_mod, "get_latest_run_for_thread", new=latest),
    ):
        response = await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=stream_id,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert response.status_code == 204
    run_stream.assert_awaited_once_with("caller-run")


@pytest.mark.asyncio
async def test_resume_returns_204_for_foreign_active_stream_without_caller_run() -> (
    None
):
    """A same-workspace editor cannot replay another editor's active run."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    active_stream_id = str(_uuid.uuid4())
    current_user = Mock(id="editor-2", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    active = AsyncMock(return_value=None)

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=active_stream_id),
        ),
        patch.object(execute_mod, "get_active_run_for_thread", new=active),
    ):
        responses = [
            await execute_mod.resume_stream(
                _resume_request(),
                thread_id=thread_id,
                after=0,
                stream=stream,
                last_event_id=None,
                current_user=current_user,
                db=db,
            )
            for stream in (None, active_stream_id)
        ]

    assert [response.status_code for response in responses] == [204, 204]
    assert active.await_count == 2


@pytest.mark.asyncio
async def test_resume_returns_204_for_foreign_finished_stream() -> None:
    """A terminal stream is private even when its thread remains editable."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    stream_id = str(_uuid.uuid4())
    current_user = Mock(id="editor-2", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(return_value=(SimpleNamespace(id=thread_id), "conversation"))
    latest = AsyncMock(return_value=None)

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(
            execute_mod._stream_buffer,
            "active_stream_id",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            execute_mod._stream_buffer,
            "thread_id_for_stream",
            new=AsyncMock(return_value=thread_id),
        ),
        patch.object(execute_mod, "get_latest_run_for_thread", new=latest),
    ):
        response = await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=stream_id,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert response.status_code == 204
    latest.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_denies_non_editable_thread_before_redis_or_run_reads() -> None:
    """Viewer/revoked/deleted-parent access fails before any stream lookup."""
    from src.api.agent import execute as execute_mod

    thread_id = str(_uuid.uuid4())
    current_user = Mock(id="viewer-1", organization_id="org-1")
    db = _resume_db()
    resolve = AsyncMock(
        side_effect=execute_mod.AgentThreadResolutionError("Thread not found")
    )
    active_stream = AsyncMock()
    active_run = AsyncMock()

    with (
        patch.object(execute_mod, "_resolve_thread", new=resolve),
        patch.object(execute_mod._stream_buffer, "active_stream_id", new=active_stream),
        patch.object(execute_mod, "get_active_run_for_thread", new=active_run),
        pytest.raises(HTTPException) as exc_info,
    ):
        await execute_mod.resume_stream(
            _resume_request(),
            thread_id=thread_id,
            after=0,
            stream=None,
            last_event_id=None,
            current_user=current_user,
            db=db,
        )

    assert exc_info.value.status_code == 404
    active_stream.assert_not_awaited()
    active_run.assert_not_awaited()
    db.execute.assert_not_awaited()
