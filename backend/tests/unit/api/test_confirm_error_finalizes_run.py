"""Regression (R2-H1 + codex review follow-ups on #1413).

stream_confirm_event_generator's ``except Exception`` handler previously
emitted an ERROR frame but never finalized the durable run — the job stayed
``awaiting_confirmation`` forever. The review pass then found two defects in
the first fix:

- finalizing on PRE-resume failures stranded the retry the released claim
  had just enabled (terminal run states are absorbing), so finalize must
  fire only once ``events_started`` is True;
- the payload ``{reason, error, request_id}`` failed ``RunFailedPayload``
  validation (``extra="forbid"``, requires code+message) inside
  ``append_event`` — rolling back the status write and silently defeating
  the fix. The payload must be ``{code, message}``.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_thread_access import editable_thread_getter

THREAD_ID = "11111111-1111-4111-8111-111111111625"


class _FakeGraph:
    """Configurable resume: optionally yield events before raising."""

    def __init__(self, events_before_error: int = 0) -> None:
        self._remaining = events_before_error

    def astream_events(self, *args: object, **kwargs: object) -> "Any":
        return self

    def __aiter__(self) -> "Any":
        return self

    async def __anext__(self) -> Any:
        if self._remaining > 0:
            self._remaining -= 1
            # Shape-minimal langchain-style event; downstream processing may
            # itself raise on it — either way the failure happens AFTER the
            # first event, which is the scenario under test.
            return {"event": "on_chat_model_stream", "data": {}, "name": "llm"}
        raise RuntimeError("resume boom")

    async def aclose(self) -> None:
        pass

    async def aget_state(self, config: object) -> "Any":
        return SimpleNamespace(
            values={
                "user_id": "user-1",
                "page_context": {},
                "messages": [],
                "tool_executions": [],
            },
            tasks=(),
        )


async def _run_confirm(graph: _FakeGraph) -> "tuple[list, AsyncMock]":
    import src.api.agent.streaming as streaming_mod

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(thread_id=THREAD_ID, confirmed=True, model="")
    current_user = Mock(id="user-1", organization_id="org-1")
    active_run = SimpleNamespace(
        job_id="run-abc-123", user_message_id=None, client_message_id=None
    )
    finalize_mock = AsyncMock()

    with (
        patch(
            "src.api.agent.streaming._stream_buffer.start_stream",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
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
            return_value=graph,
        ),
        patch(
            "src.api.agent.streaming.get_active_run_for_thread",
            new=AsyncMock(return_value=active_run),
        ),
        patch(
            "src.api.agent.streaming.claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.streaming.is_run_cancellation_requested",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=editable_thread_getter(),
        ),
        patch.object(streaming_mod, "_finalize_run_id", new=finalize_mock),
        patch(
            "src.api.agent.streaming.AsyncSessionLocal",
            return_value=AsyncMock(),
        ),
    ):
        events = []
        async for event in streaming_mod.stream_confirm_event_generator(
            body, request, current_user
        ):
            events.append(event)
    return events, finalize_mock


@pytest.mark.asyncio
async def test_pre_resume_error_keeps_run_awaiting_for_retry() -> None:
    """A failure BEFORE any resume event released the claim so the confirm
    can be retried — terminalizing the run as FAILED would strand that retry
    (terminal states are absorbing). The run must stay awaiting."""
    events, finalize_mock = await _run_confirm(_FakeGraph(events_before_error=0))
    assert any("event: error" in e for e in events)
    finalize_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_event_error_finalizes_run_with_valid_payload() -> None:
    """Once the resume produced events, a failure finalizes the run as
    FAILED — with the {code, message} payload RunFailedPayload accepts."""
    from src.services.agent.run_event_types import RunFailedPayload
    from src.shared.enums import JobStatus

    events, finalize_mock = await _run_confirm(_FakeGraph(events_before_error=1))
    assert any("event: error" in e for e in events)
    finalize_mock.assert_awaited_once()
    assert finalize_mock.await_args is not None
    args, kwargs = finalize_mock.await_args
    assert args[1] == "run-abc-123"
    assert kwargs["status"] == JobStatus.FAILED
    # The exact regression: this constructor rejected the old payload shape
    # ({reason, error, request_id}) and rolled the finalize back.
    RunFailedPayload(**kwargs["payload"])
