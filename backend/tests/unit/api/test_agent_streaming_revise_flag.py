"""Unit tests for the `revising` flag in the SSE reflection event.

The reflection_gate emits an ``event: reflection`` SSE payload. When the
gate decides to loop back to llm_node (revise), the payload must include
``"revising": true`` so the client can reset streamed content before the
second answer begins.

Routing predicate (from reflection_route in
backend/src/services/agent/reflection.py):
    not result.passed AND result.severity == "major" AND current_count < 2

Both the main stream generator (stream_event_generator) and the
confirm/resume generator (stream_confirm_event_generator) are tested.

Note on libpq: stream_event_generator does a lazy ``from psycopg import
OperationalError`` inside the generator body. On this host libpq is
absent, so the import raises ImportError. We stub ``psycopg`` in
sys.modules before importing the module-under-test so the retry branch
never fires and the stream runs through to completion. The confirm
generator does not have this import, so its tests run cleanly without
the stub. This mirrors the known pattern in nous-libpq-test-env.md.
"""

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.utils.agent_thread_access import editable_thread_getter


@pytest.fixture(autouse=True)
def _allow_durable_thread_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        editable_thread_getter(),
    )


from tests.utils.agent_stream import frames_of_type, make_stream_request

# ---------------------------------------------------------------------------
# psycopg stub — prevents ImportError on hosts without libpq
# (stream_event_generator does `from psycopg import OperationalError` lazily)
# ---------------------------------------------------------------------------


def _install_psycopg_stub():
    """Inject a minimal psycopg stub into sys.modules if libpq is absent."""
    if "psycopg" in sys.modules:
        return  # real psycopg is available — no stub needed
    stub = types.ModuleType("psycopg")
    stub.OperationalError = OSError  # any exception subclass works
    sys.modules["psycopg"] = stub


_install_psycopg_stub()


# ---------------------------------------------------------------------------
# Fake graph helpers
# ---------------------------------------------------------------------------


def _make_reflection_graph(passed: bool, severity: str, reflection_count: int):
    """Return a _FakeGraph whose astream_events yields a reflection_gate event."""

    class _FakeGraph:
        async def astream_events(self, *args, **kwargs):
            # Emit a token first (satisfies trace-event ordering logic)
            yield {
                "event": "on_chat_model_stream",
                "name": "llm_node",
                "metadata": {"langgraph_node": "llm_node"},
                "data": {"chunk": SimpleNamespace(content="hello")},
            }
            # Emit the reflection_gate on_chain_end event
            yield {
                "event": "on_chain_end",
                "name": "reflection_gate",
                "metadata": {},
                "data": {
                    "output": {
                        "_reflection_result": SimpleNamespace(
                            passed=passed,
                            issues=["bad response"] if not passed else [],
                            severity=severity,
                        ),
                        "reflection_count": reflection_count,
                    }
                },
            }

        async def aget_state(self, config):
            return SimpleNamespace(
                values={
                    "user_id": "user-1",
                    "messages": [SimpleNamespace(type="ai", content="hello")],
                    "tool_executions": [],
                },
                tasks=(),
            )

    return _FakeGraph()


_COMMON_PATCHES = [
    (
        "src.services.agent.observability.configure_langsmith",
        None,
    ),
    (
        "src.services.agent.checkpointer.get_checkpointer",
        AsyncMock(return_value=object()),
    ),
    (
        "src.api.agent.streaming._persist_user_message_guarded",
        AsyncMock(return_value=None),
    ),
    (
        "src.api.agent.streaming.AsyncSessionLocal",
        AsyncMock(),
    ),
]


def _collect_patches(graph):
    """Return the patch stack with the provided graph injected."""
    return [
        *_COMMON_PATCHES,
        (
            "src.services.agent.graph.compile_agent_graph",
            graph,
        ),
    ]


def _extract_reflection_payload(events: list[str]) -> dict:
    """Find the first ``event: reflection`` SSE frame and parse its JSON payload."""
    reflection_events = frames_of_type(events, "reflection")
    assert reflection_events, f"No reflection event in: {events}"
    return json.loads(reflection_events[0].split("data: ", 1)[1].strip())


# ---------------------------------------------------------------------------
# stream_event_generator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_event_generator_revising_true_when_major_fail_under_budget():
    """passed=False, severity=major, round_num=1 (< 2) → revising=True."""
    from src.api.agent.streaming import stream_event_generator

    graph = _make_reflection_graph(passed=False, severity="major", reflection_count=1)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111201")
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is True, f"Expected revising=True, got: {payload}"
    assert payload["passed"] is False
    assert payload["round"] == 1


@pytest.mark.asyncio
async def test_stream_event_generator_revising_false_when_passed():
    """passed=True → revising=False regardless of severity."""
    from src.api.agent.streaming import stream_event_generator

    graph = _make_reflection_graph(passed=True, severity="none", reflection_count=0)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111202")
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is False, f"Expected revising=False, got: {payload}"
    assert payload["passed"] is True


@pytest.mark.asyncio
async def test_stream_event_generator_revising_false_when_minor_severity():
    """passed=False but severity=minor → revising=False (minor never routes to revise)."""
    from src.api.agent.streaming import stream_event_generator

    graph = _make_reflection_graph(passed=False, severity="minor", reflection_count=0)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111203")
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is False, f"Expected revising=False, got: {payload}"


@pytest.mark.asyncio
async def test_stream_event_generator_revising_false_when_budget_exhausted():
    """passed=False, severity=major, but round_num=2 (>= 2) → revising=False (budget cap)."""
    from src.api.agent.streaming import stream_event_generator

    graph = _make_reflection_graph(passed=False, severity="major", reflection_count=2)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = make_stream_request(thread_id="11111111-1111-1111-1111-111111111204")
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        events = []
        async for event in stream_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is False, f"Expected revising=False, got: {payload}"


# ---------------------------------------------------------------------------
# stream_confirm_event_generator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_confirm_event_generator_revising_true_when_major_fail_under_budget():
    """confirm path: passed=False, severity=major, round_num=1 → revising=True."""
    from src.api.agent.streaming import stream_confirm_event_generator

    graph = _make_reflection_graph(passed=False, severity="major", reflection_count=1)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-4111-8111-111111111621", confirmed=True
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
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
    ):
        events = []
        async for event in stream_confirm_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is True, f"Expected revising=True, got: {payload}"


@pytest.mark.asyncio
async def test_stream_confirm_event_generator_revising_false_when_passed():
    """confirm path: passed=True → revising=False."""
    from src.api.agent.streaming import stream_confirm_event_generator

    graph = _make_reflection_graph(passed=True, severity="none", reflection_count=0)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    body = SimpleNamespace(
        thread_id="11111111-1111-4111-8111-111111111622", confirmed=True
    )
    current_user = Mock(id="user-1", organization_id="org-1")

    patches = _collect_patches(graph)
    with (
        patch(patches[0][0], return_value=patches[0][1]),
        patch(patches[1][0], new=patches[1][1]),
        patch(patches[2][0], new=patches[2][1]),
        patch(patches[3][0], return_value=patches[3][1]),
        patch(patches[4][0], return_value=patches[4][1]),
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
    ):
        events = []
        async for event in stream_confirm_event_generator(body, request, current_user):
            events.append(event)

    payload = _extract_reflection_payload(events)
    assert payload["revising"] is False, f"Expected revising=False, got: {payload}"
