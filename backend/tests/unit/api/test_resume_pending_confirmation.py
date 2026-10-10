"""Resume must re-deliver a HITL confirmation the client never saw.

Live incident (dev, 2026-07-25 22:50-22:52): "Start a project about rag
testing" correctly routed to ``create_project``, raised the HITL interrupt,
and the SSE stream ended. The client's reconnect hit
``GET /agent/stream/resume/{thread_id}`` and got **204** — the stream buffer's
active pointer is cleared the moment a stream ends, so there was nothing to
replay. The user saw a turn that produced nothing, re-sent twice, and the
backend logged "Abandoned HITL interrupt silently dropped ... create_project"
each time.

The interrupt outlives the stream: it is resolved only by ``/confirm`` or
discarded by the next turn. So resume must consult the checkpoint, not just
the buffer. Detection mirrors ``streaming.py`` (``aget_state`` +
``snapshot.tasks[*].interrupts``) because with a checkpointer attached
``interrupt()`` returns state rather than raising ``GraphInterrupt``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from tests.utils.agent_stream import sse_event_name, sse_seq

pytestmark = pytest.mark.unit


class _Interrupt:
    def __init__(self, value: dict) -> None:
        self.id = "test-interrupt"
        self.value = value


class _Snapshot:
    def __init__(self, tasks: tuple) -> None:
        self.config = {"configurable": {"checkpoint_id": "saved-interrupt"}}
        self.tasks = tasks
        self.values: dict[str, Any] = {"messages": []}


class _Graph:
    def __init__(self, snapshot: Any) -> None:
        self._snapshot = snapshot

    async def aget_state(self, _config: dict) -> Any:
        return self._snapshot


def _patch_graph(monkeypatch: pytest.MonkeyPatch, snapshot: Any) -> None:
    """Point the helper's lazy imports at a stub graph."""
    from src.services.agent import checkpointer as ckpt_mod
    from src.services.agent import graph as graph_mod
    from src.services.agent import memory as memory_mod

    async def _fake_checkpointer() -> object:
        return object()

    async def _fake_store() -> object:
        return object()

    monkeypatch.setattr(ckpt_mod, "get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr(memory_mod, "get_memory_store", _fake_store)
    monkeypatch.setattr(
        graph_mod, "compile_agent_graph", lambda **_kw: _Graph(snapshot)
    )


def _user() -> Any:
    """Only .id / .organization_id are read; a real User needs a DB row."""
    return cast(Any, SimpleNamespace(id=uuid4(), organization_id=uuid4()))


async def test_pending_interrupt_is_redelivered_as_a_confirmation_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent.execute import _pending_confirmation_frame

    confirmation = {"tools": [{"name": "create_project", "args": {"name": "rag"}}]}
    user = _user()
    snapshot = _Snapshot((SimpleNamespace(interrupts=[_Interrupt(confirmation)]),))
    snapshot.values["user_id"] = str(user.id)
    _patch_graph(monkeypatch, snapshot)
    thread_id = str(uuid4())
    frame = await _pending_confirmation_frame(thread_id, user)

    assert frame is not None, "a parked interrupt must be recoverable after resume"
    # Anchored to the event line, not the first byte: the frame leads with the
    # envelope's ``id:`` line (Last-Event-ID cursor).
    assert sse_event_name(frame) == "confirmation"
    assert sse_seq(frame) is not None
    assert "create_project" in frame
    assert thread_id in frame
    assert frame.endswith("\n\n"), "SSE frames terminate with a blank line"


async def test_no_interrupt_returns_none_so_resume_still_204s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished run must not be resurrected as a confirmation."""
    from src.api.agent.execute import _pending_confirmation_frame

    _patch_graph(monkeypatch, _Snapshot((SimpleNamespace(interrupts=[]),)))

    assert await _pending_confirmation_frame(str(uuid4()), _user()) is None


async def test_missing_checkpoint_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.agent.execute import _pending_confirmation_frame

    _patch_graph(monkeypatch, None)

    assert await _pending_confirmation_frame(str(uuid4()), _user()) is None


async def test_checkpoint_failure_degrades_to_none_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume previously always succeeded; a lookup fault must not change that."""
    from src.api.agent.execute import _pending_confirmation_frame
    from src.services.agent import checkpointer as ckpt_mod

    async def _boom() -> object:
        raise RuntimeError("checkpointer down")

    monkeypatch.setattr(ckpt_mod, "get_checkpointer", _boom)

    assert await _pending_confirmation_frame(str(uuid4()), _user()) is None
