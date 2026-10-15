from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.api.agent import harness_streaming
from src.core.config import settings
from src.schemas.integration_context import IntegrationContext
from src.services.agent.run_event_types import RunEventType
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
)


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
    assert "id: 6" in frames[2]
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


_OWNER = SimpleNamespace(id=uuid4(), organization_id=uuid4())
_CHAT = SimpleNamespace(id=uuid4(), source_project_id=uuid4())


async def _start_codex(db: Any = None, *, thread: Any = _CHAT) -> Any:
    return await harness_streaming.create_chat_context(
        db if db is not None else SimpleNamespace(),
        current_user=_OWNER,
        thread=thread,
        device_id=uuid4(),
        workspace_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_a_computer_without_access_to_the_project_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(
        harness_streaming,
        "_authorize_chat_run",
        AsyncMock(side_effect=IntegrationAccessDenied()),
    )
    with pytest.raises(DeviceNotConnectedToProject):
        await _start_codex()


@pytest.mark.asyncio
async def test_a_chat_bound_computer_keeps_its_own_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(
        harness_streaming,
        "_authorize_chat_run",
        AsyncMock(side_effect=DeviceBoundToAnotherChat()),
    )
    with pytest.raises(DeviceBoundToAnotherChat) as raised:
        await _start_codex()
    assert type(raised.value) is DeviceBoundToAnotherChat


@pytest.mark.asyncio
async def test_server_policy_refusals_stay_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock()
    monkeypatch.setattr(harness_streaming, "_authorize_chat_run", authorize)
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    with pytest.raises(ValueError, match="disabled by server policy"):
        await _start_codex()
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    with pytest.raises(ValueError, match="project-bound"):
        await _start_codex(thread=SimpleNamespace(id=uuid4(), source_project_id=None))
    authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_binding_is_not_a_denial_the_user_can_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain PermissionError is a server fault, not an IntegrationAccessDenied:
    # it keeps the generic treatment instead of blaming the user's computer.
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(
        harness_streaming,
        "_authorize_chat_run",
        AsyncMock(side_effect=PermissionError("could not bind")),
    )
    with pytest.raises(PermissionError) as raised:
        await _start_codex()
    assert not isinstance(raised.value, IntegrationAccessDenied)


@pytest.mark.asyncio
async def test_a_folder_this_chat_cannot_use_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No folder row matches this device and folder: the real folder check runs
    # and nothing is minted.
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    mint = AsyncMock()
    monkeypatch.setattr(harness_streaming, "mint_integration_grant", mint)
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(DeviceNotConnectedToProject):
        await _start_codex(db)
    mint.assert_not_awaited()
