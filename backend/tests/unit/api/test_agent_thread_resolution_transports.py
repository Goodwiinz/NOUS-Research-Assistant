from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.models.user import User
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _frame_payload(frame: str) -> dict[Any, Any]:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return cast(dict[Any, Any], json.loads(data_line.removeprefix("data: ")))


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(), headers={})


async def test_execute_maps_authoritative_resolution_failure_before_job_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import execute
    from src.services.agent.agent_execution_service import AgentThreadResolutionError

    set_job = Mock()
    monkeypatch.setattr(execute, "_enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(
        execute,
        "_resolve_thread",
        AsyncMock(side_effect=AgentThreadResolutionError("Thread not found")),
    )
    monkeypatch.setattr(execute, "_set_job", set_job)
    request = AgentExecuteRequest(
        messages=[AgentMessage(role="user", content="hello")],
        thread_id=str(uuid4()),
    )

    with pytest.raises(HTTPException) as exc_info:
        await execute.execute_agent(
            request,
            background_tasks=Mock(),
            current_user=cast(
                User, SimpleNamespace(id=uuid4(), organization_id=uuid4())
            ),
            db=AsyncMock(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Thread not found"
    set_job.assert_not_called()


async def test_stream_rejects_inaccessible_explicit_thread_before_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import streaming
    from src.services.agent.agent_execution_service import AgentThreadResolutionError

    db = AsyncMock()
    monkeypatch.setattr(streaming, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(
        streaming,
        "_resolve_thread",
        AsyncMock(side_effect=AgentThreadResolutionError("Thread not found")),
    )
    accept = AsyncMock()
    monkeypatch.setattr(streaming, "accept_submission", accept)

    frames = [
        frame
        async for frame in streaming.stream_event_generator(
            AgentExecuteRequest(
                messages=[AgentMessage(role="user", content="hello")],
                thread_id=str(uuid4()),
            ),
            _request(),
            cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4())),
        )
    ]

    assert len(frames) == 1
    assert "event: error" in frames[0]
    assert _frame_payload(frames[0])["error"] == "Thread not found"
    accept.assert_not_awaited()


async def test_stream_confirm_checks_current_edit_access_before_checkpoint_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.agent import streaming
    from src.api.agent.execute import StreamConfirmRequest
    from src.services.agent import checkpointer
    from src.services.agent import graph as graph_module
    from src.services.agent import memory
    from src.services.agent.agent_execution_service import AgentThreadResolutionError

    db = AsyncMock()
    user = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=SimpleNamespace(
                values={
                    "user_id": str(user.id),
                    "thread_persistence": "durable",
                },
                tasks=(),
                config={"configurable": {"checkpoint_id": "checkpoint-owned"}},
            )
        ),
        astream_events=AsyncMock(),
    )
    monkeypatch.setattr(streaming, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(streaming, "_bootstrap_langsmith", Mock())
    resolve = AsyncMock(side_effect=AgentThreadResolutionError("Thread not found"))
    monkeypatch.setattr(streaming, "_resolve_thread", resolve)
    get_checkpointer = AsyncMock(return_value=object())
    monkeypatch.setattr(checkpointer, "get_checkpointer", get_checkpointer)
    monkeypatch.setattr(memory, "get_memory_store", AsyncMock(return_value=object()))
    monkeypatch.setattr(graph_module, "compile_agent_graph", Mock(return_value=graph))
    get_active_run = AsyncMock()
    claim_run = AsyncMock()
    monkeypatch.setattr(streaming, "get_active_run_for_thread", get_active_run)
    monkeypatch.setattr(
        streaming,
        "claim_awaiting_run_for_confirmation",
        claim_run,
    )

    frames = [
        frame
        async for frame in streaming.stream_confirm_event_generator(
            StreamConfirmRequest(thread_id=str(uuid4()), confirmed=True),
            _request(),
            user,
        )
    ]

    assert len(frames) == 1
    assert "event: error" in frames[0]
    payload = _frame_payload(frames[0])
    assert payload["error"] == "Thread not found"
    assert payload["category"] == "invalid_request"
    resolve.assert_awaited_once()
    get_checkpointer.assert_awaited_once()
    graph.astream_events.assert_not_called()
    get_active_run.assert_not_awaited()
    claim_run.assert_not_awaited()
