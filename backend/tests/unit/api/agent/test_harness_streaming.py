from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.api.agent import harness_streaming
from src.services.agent.run_event_types import RunEventType


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Request:
    def __init__(self):
        self.disconnect_checks = 0

    async def is_disconnected(self):
        self.disconnect_checks += 1
        return False


@pytest.mark.asyncio
async def test_external_stream_replays_persisted_events_and_stops_on_terminal(
    monkeypatch,
):
    run_id = uuid4()
    context = SimpleNamespace(organization_id=uuid4(), user_id=uuid4())
    rows = [
        SimpleNamespace(
            seq=4,
            event_type=RunEventType.ASSISTANT_DELTA.value,
            payload={"text": "persisted answer"},
        ),
        SimpleNamespace(
            seq=5,
            event_type=RunEventType.RUN_COMPLETED.value,
            payload={"assistant_message_id": "message-a"},
        ),
    ]
    reads = []

    async def read_events(_db, actual_run_id, **kwargs):
        reads.append((actual_run_id, kwargs["after_seq"]))
        return rows

    monkeypatch.setattr(harness_streaming, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(harness_streaming, "read_events", read_events)
    request = _Request()

    frames = [
        frame
        async for frame in harness_streaming.stream_harness_run(
            request, run_id, context, after_seq=3
        )
    ]

    assert "event: token" in frames[0]
    assert 'data: {"content": "persisted answer"}' in frames[0]
    assert "id: 4" in frames[0]
    assert "event: done" in frames[1]
    assert "id: 5" in frames[1]
    assert reads == [(str(run_id), 3)]


@pytest.mark.asyncio
async def test_external_stream_disconnect_only_ends_observation(monkeypatch):
    context = SimpleNamespace(organization_id=uuid4(), user_id=uuid4())
    request = _Request()
    calls = 0

    async def read_events(_db, _run_id, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    async def disconnect_after_first_read():
        return request.disconnect_checks >= 3

    request.is_disconnected = disconnect_after_first_read
    monkeypatch.setattr(harness_streaming, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(harness_streaming, "read_events", read_events)
    monkeypatch.setattr(harness_streaming.asyncio, "sleep", lambda _seconds: _never())

    frames = [
        frame
        async for frame in harness_streaming.stream_harness_run(
            request, uuid4(), context
        )
    ]
    assert frames == []
    assert calls == 1


async def _never():
    return None
