"""Route-level proof that ``error`` SSE frames carry a server-authored
``category`` (W2).

The contract test AST-proves every emit site routes through
``error_frame_payload``; this file proves the *values* that reach the wire on
the representative failures, and that the frame stays flat with ``error`` as a
STRING (adding ``category`` must not turn ``error`` into an object).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from openai import RateLimitError

from src.api.agent.streaming import _stream_failure_category
from src.services.agent.agent_run_service import ActiveRunConflict
from src.shared.enums import AgentErrorCategory
from tests.utils.agent_stream import frames_of_type, make_stream_request, sse_data


class _RaisingGraph:
    """Graph whose event stream blows up with a fixed exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def astream_events(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        raise self._exc
        yield  # pragma: no cover - makes this an async generator

    async def aget_state(self, config: Any) -> Any:
        return SimpleNamespace(values={"user_id": "user-1"}, tasks=())


class _ConfirmGraph:
    """Confirm-path graph with a controllable snapshot."""

    def __init__(self, snapshot: Any) -> None:
        self._snapshot = snapshot

    async def astream_events(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "on_chat_model_stream", "data": {}}

    async def aget_state(self, config: Any) -> Any:
        return self._snapshot

    async def aclose(self) -> None:
        pass


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


async def _run_stream(graph: Any) -> list[str]:
    from src.api.agent.streaming import stream_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111301")
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        # No Redis in unit CI: without this the emitter's start_stream sits on a
        # connect timeout for every frame, which stalled this module for ~11
        # minutes and pushed the lease-based sweeper tests past their expiry.
        # Sibling streaming suites patch the same seam.
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
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch("src.api.agent.streaming.AsyncSessionLocal", return_value=AsyncMock()),
    ):
        return [
            frame async for frame in stream_event_generator(body, request, current_user)
        ]


async def _run_confirm(graph: Any) -> list[str]:
    from src.api.agent.streaming import stream_confirm_event_generator

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(thread_id="thread-confirm", confirmed=True)
    current_user = Mock(id="user-1", organization_id="org-1")

    with (
        # No Redis in unit CI: without this the emitter's start_stream sits on a
        # connect timeout for every frame, which stalled this module for ~11
        # minutes and pushed the lease-based sweeper tests past their expiry.
        # Sibling streaming suites patch the same seam.
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
            "src.services.agent.checkpointer.reset_checkpointer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=object()),
        ),
        patch("src.services.agent.graph.compile_agent_graph", return_value=graph),
        patch("src.api.agent.streaming.AsyncSessionLocal", return_value=AsyncMock()),
        patch(
            "src.api.agent.streaming.get_active_run_for_thread",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    job_id="run-1", user_message_id=None, client_message_id=None
                )
            ),
        ),
        patch(
            "src.api.agent.streaming.claim_awaiting_run_for_confirmation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.streaming._finalize_run_id",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(SimpleNamespace(id="editable-thread"), None)),
        ),
    ):
        return [
            frame
            async for frame in stream_confirm_event_generator(
                body, request, current_user
            )
        ]


def _error_payload(frames: list[str]) -> dict[str, Any]:
    errors = frames_of_type(frames, "error")
    assert errors, f"no error frame emitted; got {[f[:60] for f in frames]}"
    payload: dict[str, Any] = sse_data(errors[-1])
    return payload


@pytest.mark.asyncio
async def test_stream_catch_all_labels_an_unclassified_failure_internal() -> None:
    payload = _error_payload(await _run_stream(_RaisingGraph(RuntimeError("boom"))))
    assert payload["category"] == AgentErrorCategory.INTERNAL.value
    # Wire compat: `error` is still a plain STRING sibling of `category`, and
    # adding the sibling key did NOT bump the envelope's schema_version.
    assert isinstance(payload["error"], str)
    assert "boom" not in payload["error"]
    assert payload["schema_version"] == "1.0"


@pytest.mark.asyncio
@pytest.mark.asyncio
def test_stream_catch_all_maps_transient_provider_failures() -> None:
    """The catch-all's mapping for the transient families, asserted directly.

    The end-to-end variant above already proves the catch-all emits
    ``_stream_failure_category(exc)``; re-running the whole generator once per
    exception type only re-proves the classifier, and it is expensive: driving
    it with a ``RateLimitError`` or a ``TimeoutError`` cost 306s and 384s in
    CI (a generic ``Exception`` returns instantly), which alone stretched the
    Unit Tests job past the sweeper suite's lease expiry. Exhaustive
    type-to-category coverage lives in tests/unit/agent/test_agent_error_categories.py.
    """
    assert (
        _stream_failure_category(_rate_limit_error()) is AgentErrorCategory.RATE_LIMITED
    )
    assert (
        _stream_failure_category(TimeoutError()) is AgentErrorCategory.UPSTREAM_TIMEOUT
    )


def test_missing_user_message_rejection_is_invalid_request_not_internal() -> None:
    """`accept_submission` rejects a submission with no user turn — a client
    fault. Everything else keeps the generic classification."""
    assert (
        _stream_failure_category(
            ValueError("accept_submission requires a user message")
        )
        is AgentErrorCategory.INVALID_REQUEST
    )
    assert (
        _stream_failure_category(ValueError("something else entirely"))
        is AgentErrorCategory.INTERNAL
    )
    assert (
        _stream_failure_category(TimeoutError()) is AgentErrorCategory.UPSTREAM_TIMEOUT
    )


def test_active_thread_writer_is_a_conflict() -> None:
    assert (
        _stream_failure_category(ActiveRunConflict("safe"))
        is AgentErrorCategory.CONFLICT
    )


@pytest.mark.asyncio
async def test_confirm_missing_thread_and_foreign_thread_are_byte_identical() -> None:
    """Anti-enumeration: a nonexistent checkpoint and someone else's checkpoint
    must produce the SAME payload — same message AND same category — so the
    endpoint cannot be used to probe which thread ids exist."""
    missing = SimpleNamespace(values={}, tasks=(), config={})
    foreign = SimpleNamespace(
        values={
            "user_id": "someone-else",
            "page_context": {"type": "general"},
            "messages": [],
        },
        tasks=(),
        config={"configurable": {"checkpoint_id": "ckpt-1"}},
    )

    missing_payload = _error_payload(await _run_confirm(_ConfirmGraph(missing)))
    foreign_payload = _error_payload(await _run_confirm(_ConfirmGraph(foreign)))

    # The stream envelope adds per-frame/per-run identity (event_id,
    # occurred_at, trace_id) that is unique by construction; the observable
    # error content must be equal.
    envelope_keys = {"event_id", "occurred_at", "trace_id"}
    assert {k: v for k, v in missing_payload.items() if k not in envelope_keys} == {
        k: v for k, v in foreign_payload.items() if k not in envelope_keys
    }
    assert missing_payload["error"] == "Thread not found"
    assert missing_payload["category"] == AgentErrorCategory.INVALID_REQUEST.value


@pytest.mark.asyncio
async def test_confirm_claim_contention_is_conflict() -> None:
    """The loser of the HITL confirm claim gets `conflict` — retrying the same
    confirm identically cannot succeed while the winner holds the claim."""
    snapshot = SimpleNamespace(
        values={
            "user_id": "user-1",
            "page_context": {"type": "general"},
            "messages": [],
            "tool_executions": [],
            "retrieved_contexts": [],
            "plan": [],
        },
        tasks=(),
        config={"configurable": {"checkpoint_id": "ckpt-contended"}},
    )

    class _AlwaysClaimedRedis:
        async def set(
            self, key: Any, value: Any, nx: bool = False, ex: Any = None
        ) -> Any:
            return None if nx else True

        async def delete(self, key: Any) -> Any:
            return None

    with (
        patch(
            "src.services.agent.job_store.get_redis",
            new=AsyncMock(return_value=_AlwaysClaimedRedis()),
        ),
    ):
        payload = _error_payload(await _run_confirm(_ConfirmGraph(snapshot)))

    assert payload["error"] == "Confirmation already in progress"
    assert payload["category"] == AgentErrorCategory.CONFLICT.value
