"""Audit S2-M3 regressions: no ERROR frame after a terminal, and no
absorbing FAILED write racing an AWAITING_CONFIRMATION park."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_thread_access import editable_thread_getter


def _frame_event(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("event: "):
            return line.removeprefix("event: ").strip()
    return ""


def _snapshot_with_interrupt(value: dict) -> SimpleNamespace:
    return SimpleNamespace(
        values={
            "user_id": "user-1",
            "page_context": {},
            "messages": [],
            "tool_executions": [],
        },
        tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value=value),)),),
        config={"configurable": {"checkpoint_id": f"ckpt-{id(value)}"}},
    )


_CONFIRMATION = {
    "tool_name": "create_note",
    "tool_args": {"title": "parked"},
    "message": "Create this note?",
}


@pytest.mark.asyncio
async def test_graph_park_failure_keeps_confirmation_terminal_and_never_fails_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finalize_run(AWAITING_CONFIRMATION) raising after the CONFIRMATION
    frame must not emit ERROR nor write FAILED."""
    from src.api.agent import streaming as streaming_mod
    from src.services.agent import agent_execution_service as jobs_mod

    thread = SimpleNamespace(id="t-park-1", conversation_id="c-park-1")
    user = Mock(id="user-1", organization_id="org-1")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    finalize_calls: list[Any] = []

    async def exploding_finalize(*args: Any, **kwargs: Any) -> None:
        finalize_calls.append(kwargs.get("status"))
        raise RuntimeError("db gone")

    body = SimpleNamespace(
        messages=[SimpleNamespace(role="user", content="hi")],
        use_rag=False,
        thread_id="t-park-1",
        page_context={"type": "chat"},
        model="",
        client_message_id=None,
    )

    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", False)

    async def _no_stream(*_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
        return
        yield  # pragma: no cover

    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=_snapshot_with_interrupt(_CONFIRMATION)),
        astream_events=_no_stream,
        aupdate_state=AsyncMock(),
    )
    fake_session = SimpleNamespace(close=AsyncMock())

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(
            streaming_mod, "_resolve_thread", new=AsyncMock(return_value=(thread, None))
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(streaming_mod, "_accept_eligible", new=Mock(return_value=False)),
        patch.object(
            streaming_mod, "accept_submission", new=AsyncMock(return_value=None)
        ),
        patch.object(
            streaming_mod,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch.object(streaming_mod, "_finalize_run", new=exploding_finalize),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch.object(jobs_mod, "_persist_assistant_message_safe", new=AsyncMock()),
    ):
        frames = [
            f async for f in streaming_mod.stream_event_generator(body, request, user)
        ]

    events = [_frame_event(f) for f in frames]
    assert events[-1] == "confirmation", events
    assert "error" not in events[1:], "no ERROR after terminal"
    assert all(s == "awaiting_confirmation" for s in finalize_calls), finalize_calls
    payload = json.loads(
        next(l for l in frames[-1].splitlines() if l.startswith("data: ")).removeprefix(
            "data: "
        )
    )
    assert payload["confirmation"]["tool_name"] == "create_note"


@pytest.mark.asyncio
async def test_graph_park_stop_race_finalizes_cancelled_before_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost RUNNING -> AWAITING park must let the producer ACK Stop.

    The Stop request can claim ``STOPPING`` after the pre-park poll. The
    guarded park then returns False; publishing a confirmation at that point
    would exit the only producer and leave the run stopping forever.
    """
    from src.api.agent import streaming as streaming_mod
    from src.services.agent import agent_execution_service as jobs_mod
    from src.services.agent.agent_submission_service import AcceptedSubmission
    from src.shared.enums import JobStatus

    thread = SimpleNamespace(id="t-park-race", conversation_id="c-park-race")
    user = Mock(id="user-1", organization_id="org-1")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    acceptance = AcceptedSubmission(
        run_id="run-park-race",
        thread_id="t-park-race",
        user_message_id="user-message-park-race",
        outbox_id="outbox-park-race",
        idempotency_key="idem-park-race",
    )
    body = SimpleNamespace(
        messages=[SimpleNamespace(role="user", content="hi")],
        use_rag=False,
        thread_id="t-park-race",
        page_context={"type": "chat"},
        model="",
        client_message_id=None,
    )
    finalize_statuses: list[Any] = []

    async def finalize(*_args: Any, **kwargs: Any) -> bool:
        status = kwargs["status"]
        finalize_statuses.append(status)
        return status is not JobStatus.AWAITING_CONFIRMATION

    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_FAST_PATH_ENABLED", False)

    async def _no_stream(*_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
        return
        yield  # pragma: no cover

    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=_snapshot_with_interrupt(_CONFIRMATION)),
        astream_events=_no_stream,
        aupdate_state=AsyncMock(),
    )
    fake_session = SimpleNamespace(close=AsyncMock())

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=fake_session),
        patch.object(
            streaming_mod,
            "_resolve_thread",
            new=AsyncMock(return_value=(thread, None)),
        ),
        patch.object(
            streaming_mod,
            "_accept_eligible",
            new=Mock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "accept_submission",
            new=AsyncMock(return_value=acceptance),
        ),
        patch.object(
            streaming_mod,
            "mark_submission_dispatched",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "is_run_cancellation_requested",
            new=AsyncMock(side_effect=[False, False, False, False, True]),
        ),
        patch.object(
            streaming_mod,
            "_persist_user_message_guarded",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "_resolve_and_bind_project",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
        patch.object(streaming_mod, "_finalize_run", new=finalize),
        patch.object(jobs_mod, "_persist_assistant_message_safe", new=AsyncMock()),
        patch(
            "src.services.agent.checkpointer.get_checkpointer",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
    ):
        frames = [
            f async for f in streaming_mod.stream_event_generator(body, request, user)
        ]

    events = [_frame_event(f) for f in frames]
    assert "confirmation" not in events
    assert finalize_statuses == [
        JobStatus.AWAITING_CONFIRMATION,
        JobStatus.CANCELLED,
    ]
    fake_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_park_failure_after_nested_confirmation_stays_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested-confirm twin: _finalize_run_id raising after CONFIRMATION must
    not produce a trailing ERROR frame."""
    from src.api.agent import streaming as streaming_mod

    db = SimpleNamespace(close=AsyncMock())
    request = SimpleNamespace(
        state=SimpleNamespace(request_id="req-term-guard"),
        is_disconnected=AsyncMock(return_value=False),
    )
    body = SimpleNamespace(
        thread_id=str(__import__("uuid").uuid4()), confirmed=True, model=""
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    class _Graph:
        snapshot = _snapshot_with_interrupt(_CONFIRMATION)

        async def aget_state(self, _config: Any) -> Any:
            return self.snapshot

        async def astream_events(
            self, *_a: Any, **_k: Any
        ) -> AsyncIterator[dict[str, Any]]:
            yield {
                "event": "on_chat_model_stream",
                "name": "llm_node",
                "metadata": {"langgraph_node": "llm_node"},
                "data": {"chunk": SimpleNamespace(content="ok")},
            }

        ainvoke = AsyncMock(return_value={})

    graph = _Graph()
    finalize_statuses: list[Any] = []

    async def exploding_finalize_id(*args: Any, **kwargs: Any) -> None:
        finalize_statuses.append(kwargs.get("status"))
        raise RuntimeError("db gone")

    with (
        patch.object(streaming_mod, "AsyncSessionLocal", return_value=db),
        patch.object(
            streaming_mod,
            "get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-1",
                    thread_id=body.thread_id,
                    user_message_id=None,
                    client_message_id=None,
                )
            ),
        ),
        patch.object(
            streaming_mod,
            "claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch.object(
            streaming_mod,
            "is_run_cancellation_requested",
            new=AsyncMock(return_value=False),
        ),
        patch.object(streaming_mod, "_finalize_run_id", new=exploding_finalize_id),
        patch.object(
            streaming_mod._jobs_mod,
            "_persist_assistant_message_safe",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            streaming_mod._stream_buffer,
            "start_stream",
            new=AsyncMock(return_value="sid-tg"),
        ),
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=None),
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
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=editable_thread_getter(),
        ),
    ):
        frames = [
            f
            async for f in streaming_mod.stream_confirm_event_generator(
                body, request, current_user
            )
        ]

    events = [_frame_event(f) for f in frames]
    assert events.count("confirmation") == 1
    assert events[-1] == "confirmation", "no ERROR after nested terminal"
    assert all(s == "awaiting_confirmation" for s in finalize_statuses)
