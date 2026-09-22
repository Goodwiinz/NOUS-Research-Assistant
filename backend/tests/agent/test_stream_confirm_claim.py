"""Double-POST /stream/confirm must resume the graph exactly once (CX1).

The job confirm endpoint has a CAS (execute.py:409); the SSE confirm path
had none — two concurrent confirms both issued Command(resume=...) and a
destructive HITL tool could execute twice.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.utils.agent_thread_access import editable_thread

THREAD_ID = "11111111-1111-4111-8111-111111111611"


class _FakeRedisLock:
    """Minimal in-memory stand-in for redis.asyncio, NX-lock semantics only."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):  # noqa: ANN001
        if nx and key in self._store:
            return None  # redis-py: NX miss returns None (falsy)
        self._store[key] = value
        return True

    async def delete(self, key):  # noqa: ANN001
        self._store.pop(key, None)


class _FakeGraph:
    """aget_state returns a fixed snapshot (same checkpoint_id every call);
    astream_events is swapped per-test on the instance."""

    def __init__(self, snapshot) -> None:  # noqa: ANN001
        self._snapshot = snapshot
        self.astream_events = self._default_astream_events

    async def _default_astream_events(self, *args, **kwargs):
        yield {"event": "on_chat_model_stream", "data": {}}

    async def aget_state(self, config):  # noqa: ANN001
        return self._snapshot

    async def aclose(self):
        pass


def _make_snapshot(checkpoint_id: str):
    ai_msg = SimpleNamespace(type="ai", content="Confirmed and done.")
    return SimpleNamespace(
        values={
            "page_context": {"type": "general"},
            "user_id": "user-1",
            "messages": [ai_msg],
            "tool_executions": [],
            "retrieved_contexts": [],
            "plan": [],
        },
        tasks=(),
        config={"configurable": {"checkpoint_id": checkpoint_id}},
    )


@pytest.fixture
def stream_confirm_harness(monkeypatch):
    import src.api.agent.streaming as streaming_mod

    # The in-process confirm-claim fallback (R2-H4) is module-global and
    # TTL'd like its Redis counterpart — correct in prod, but it leaks
    # claims across tests sharing the same thread/checkpoint key. Isolate.
    monkeypatch.setattr(streaming_mod, "_local_confirm_claims", {})

    snapshot = _make_snapshot("ckpt-fixed-1")
    graph = _FakeGraph(snapshot)
    redis = _FakeRedisLock()
    fake_db = AsyncMock()
    fake_db.close = AsyncMock()

    monkeypatch.setattr(
        "src.services.agent.observability.configure_langsmith", lambda: None
    )
    monkeypatch.setattr(
        "src.services.agent.checkpointer.get_checkpointer",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "src.services.agent.checkpointer.reset_checkpointer",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.services.agent.memory.get_memory_store",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "src.services.agent.graph.compile_agent_graph",
        lambda **kwargs: graph,
    )
    monkeypatch.setattr(streaming_mod, "AsyncSessionLocal", lambda: fake_db)
    monkeypatch.setattr(
        streaming_mod,
        "get_active_run_for_thread",
        AsyncMock(
            return_value=SimpleNamespace(
                job_id="run-1", user_message_id=None, client_message_id=None
            )
        ),
    )
    monkeypatch.setattr(
        streaming_mod,
        "claim_awaiting_run_for_confirmation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        streaming_mod,
        "release_confirmation_claim",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(streaming_mod, "_finalize_run_id", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "src.services.agent.agent_execution_service._persist_assistant_message_safe",
        AsyncMock(return_value="assistant-msg-1"),
    )
    monkeypatch.setattr(
        streaming_mod, "_latest_user_client_message_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.services.threads.workspace_access.get_thread",
        AsyncMock(return_value=editable_thread(THREAD_ID)),
    )
    # The claim code does a lazy `from src.services.agent.job_store import
    # get_redis` inside the generator, so patching the source attribute is
    # picked up on every call — same pattern the file already uses for
    # checkpointer/graph/memory (all lazily imported per-call too).
    monkeypatch.setattr(
        "src.services.agent.job_store.get_redis", AsyncMock(return_value=redis)
    )

    def make_confirm_generator():
        body = SimpleNamespace(thread_id=THREAD_ID, confirmed=True, model="gpt-5")
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        current_user = Mock(id="user-1", organization_id="org-1")
        return streaming_mod.stream_confirm_event_generator(body, request, current_user)

    return SimpleNamespace(
        graph=graph,
        redis=redis,
        make_confirm_generator=make_confirm_generator,
    )


@pytest.mark.asyncio
async def test_concurrent_stream_confirms_resume_once(stream_confirm_harness):
    """Two overlapping confirm generators for the same interrupt: exactly one
    reaches graph.astream_events; the loser yields an error frame and stops."""
    harness = stream_confirm_harness
    resume_calls = []

    async def fake_astream_events(*a, **k):
        resume_calls.append(1)
        yield {"event": "on_chat_model_stream", "data": {}}

    harness.graph.astream_events = fake_astream_events

    async def drain(gen):
        return [frame async for frame in gen]

    out1, out2 = await asyncio.gather(
        drain(harness.make_confirm_generator()),
        drain(harness.make_confirm_generator()),
    )

    assert len(resume_calls) == 1
    loser = out1 if len(out1) < len(out2) else out2
    assert any("already in progress" in f.lower() for f in loser)


@pytest.mark.asyncio
async def test_claim_released_on_pre_resume_failure(stream_confirm_harness):
    """If the winner fails BEFORE the graph starts streaming, the claim is
    released so a legit retry is not locked out for the full TTL."""
    harness = stream_confirm_harness
    harness.graph.astream_events = AsyncMock(side_effect=RuntimeError("boom"))

    _ = [f async for f in harness.make_confirm_generator()]
    # Retry must be able to claim again
    frames = [f async for f in harness.make_confirm_generator()]
    assert not any("already in progress" in f.lower() for f in frames)


@pytest.mark.asyncio
async def test_shared_environment_does_not_resume_without_redis(
    stream_confirm_harness, monkeypatch
):
    harness = stream_confirm_harness
    resume_calls = []

    async def fake_astream_events(*args, **kwargs):
        resume_calls.append(1)
        yield {"event": "on_chat_model_stream", "data": {}}

    harness.graph.astream_events = fake_astream_events
    monkeypatch.setattr(
        "src.services.agent.job_store.get_redis", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.api.agent.streaming.process_local_confirmation_coordination_allowed",
        lambda: False,
    )

    frames = [frame async for frame in harness.make_confirm_generator()]

    assert resume_calls == []
    assert any("temporarily unavailable" in frame.lower() for frame in frames)
