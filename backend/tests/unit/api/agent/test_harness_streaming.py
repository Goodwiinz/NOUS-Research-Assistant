from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from src.api.agent import harness_streaming
from src.schemas.integration_context import IntegrationContext
from src.services.agent.run_event_types import RunEventType


def test_external_resume_cursor_resets_stale_run_identity() -> None:
    run_id = uuid4()

    assert harness_streaming.external_resume_cursor(91, str(uuid4()), run_id) == 0
    assert harness_streaming.external_resume_cursor(91, None, run_id) == 0
    assert (
        harness_streaming.external_resume_cursor(91, str(run_id).upper(), run_id) == 91
    )


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Request:
    def __init__(self) -> None:
        self.disconnect_checks = 0

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return False


@pytest.mark.asyncio
async def test_external_stream_replays_persisted_events_and_stops_on_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    context = cast(
        IntegrationContext,
        SimpleNamespace(organization_id=uuid4(), user_id=uuid4()),
    )
    rows = [
        SimpleNamespace(
            seq=4,
            event_type=RunEventType.ASSISTANT_DELTA.value,
            payload={"text": "persisted answer"},
        ),
        SimpleNamespace(
            seq=5,
            event_type=RunEventType.ARTIFACT_VERSION_CREATED.value,
            payload={"artifact_id": "artifact-a", "version_id": "version-a"},
        ),
        SimpleNamespace(
            seq=6,
            event_type=RunEventType.RUN_COMPLETED.value,
            payload={"assistant_message_id": "message-a"},
        ),
    ]
    reads = []

    async def read_events(_db: Any, actual_run_id: str, **kwargs: Any) -> list[Any]:
        reads.append((actual_run_id, kwargs["after_seq"]))
        return rows

    monkeypatch.setattr(harness_streaming, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(harness_streaming, "read_events", read_events)
    request = _Request()

    frames = [
        frame
        async for frame in harness_streaming.stream_harness_run(
            cast(Any, request), run_id, context, after_seq=3
        )
    ]

    assert "event: token" in frames[0]
    assert 'data: {"content": "persisted answer"}' in frames[0]
    assert "id: 4" in frames[0]
    assert "event: artifact" in frames[1]
    assert '"artifact_id": "artifact-a"' in frames[1]
    assert '"version_id": "version-a"' in frames[1]
    assert "id: 5" in frames[1]
    assert "event: done" in frames[2]
    assert "id: 5" in frames[1]
    assert reads == [(str(run_id), 3)]


@pytest.mark.asyncio
async def test_external_stream_disconnect_only_ends_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = cast(
        IntegrationContext,
        SimpleNamespace(organization_id=uuid4(), user_id=uuid4()),
    )
    request = _Request()
    calls = 0

    async def read_events(_db: Any, _run_id: str, **_kwargs: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        return []

    async def disconnect_after_first_read() -> bool:
        request.disconnect_checks += 1
        return request.disconnect_checks >= 3

    setattr(request, "is_disconnected", disconnect_after_first_read)
    monkeypatch.setattr(harness_streaming, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(harness_streaming, "read_events", read_events)
    monkeypatch.setattr(harness_streaming.asyncio, "sleep", lambda _seconds: _never())

    frames = [
        frame
        async for frame in harness_streaming.stream_harness_run(
            cast(Any, request), uuid4(), context
        )
    ]
    assert frames == []
    assert calls == 1


async def _never() -> None:
    return None
