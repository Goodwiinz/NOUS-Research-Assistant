"""Exercise real compatibility transport around an injected native producer.

These are transport tests, not evidence of an LLM run or hosted alphaXiv parity.

Mutation proof (2026-10-13): removing the shielded source close in
src/api/compat/alphaxiv.py:66 (_NativeStreamResponse.__call__) made
``python -m pytest -q backend/tests/unit/api/test_alphaxiv_compat.py::
test_asgi_failure_before_first_body_pull_closes_primed_source`` fail with
``closed == []`` instead of ``[True]``. Restored the source exactly and reran.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.types import Message

from src.api.compat import alphaxiv as route
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivChatRequest
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage

PAPER = "11111111-1111-4111-8111-111111111111"
CHAT = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"


def _frame(kind: str, **payload: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


def _request() -> Request:
    return Request(
        {"type": "http", "headers": [], "state": {}, "path": "/assistant/v2/chat"}
    )


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[AlphaXivChatRequest, User]:
    monkeypatch.setattr(route.execute, "_enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(route, "AsyncSessionLocal", lambda: AsyncMock())
    monkeypatch.setattr(
        route,
        "prepare_request",
        AsyncMock(
            return_value=AgentExecuteRequest(
                messages=[AgentMessage(role="user", content="Explain")],
                attachment_ids=[UUID(PAPER)],
            )
        ),
    )
    return (
        AlphaXivChatRequest.model_validate(
            {"message": "Explain", "paperVersionId": PAPER}
        ),
        cast(User, SimpleNamespace(id=UUID(RUN), organization_id=UUID(PAPER))),
    )


@pytest.mark.asyncio
async def test_header_follows_native_acceptance_and_body_is_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    lifecycle: list[str] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        try:
            lifecycle.append("committed")
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            lifecycle.append("generated")
            yield _frame("token", content="Evidence-backed answer")
            yield _frame("done", assistant_message_id=PAPER)
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    assert response.headers["llm-chat-id"] == CHAT
    assert lifecycle == ["committed"]
    raw = "".join([cast(str, frame) async for frame in response.body_iterator])
    events = [
        json.loads(frame.removeprefix("data: ")) for frame in raw.strip().split("\n\n")
    ]
    assert events[0]["type"] == "run_start"
    assert events[2] == {
        "type": "delta_output_text",
        "index": 0,
        "delta": "Evidence-backed answer",
    }
    assert events[-1] == {"type": "run_end", "kind": "completed"}
    assert lifecycle == ["committed", "generated", "closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,status",
    [
        (_frame("error", category="conflict", error="already active"), 409),
        (_frame("status", phase="accepted", thread_id=None, run_id=None), 409),
        (_frame("error", category="invalid_request", error="invalid branch"), 422),
    ],
)
async def test_pre_header_failure_closes_native_source(
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    status: int,
) -> None:
    body, user = _setup(monkeypatch)
    closed: list[bool] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        try:
            yield first
            pytest.fail("Native producer must not advance after rejected acceptance")
        finally:
            closed.append(True)

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    with pytest.raises(HTTPException) as caught:
        await route.chat_v2(body, _request(), user)
    assert caught.value.status_code == status
    assert closed == [True]


@pytest.mark.asyncio
async def test_asgi_failure_before_first_body_pull_closes_primed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    closed: list[bool] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        try:
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            pytest.fail("ASGI never consumed body")
        finally:
            closed.append(True)

    async def send(message: Message) -> None:
        raise OSError("connection closed")

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    with pytest.raises(Exception):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send
        )
    assert closed == [True]


@pytest.mark.asyncio
async def test_disconnect_facade_does_not_bind_cancellation_to_handler_task() -> None:
    request = _request()
    signal = asyncio.Event()
    setattr(request.state, route.AGENT_DISCONNECT_EVENT, signal)
    proxy = route._NativeRequest(request)
    assert not hasattr(proxy.state, route.AGENT_DISCONNECT_EVENT)
    assert not await proxy.is_disconnected()
    signal.set()
    assert await proxy.is_disconnected()


@pytest.mark.asyncio
async def test_disconnect_during_body_closes_producer_without_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    disconnect = asyncio.Event()
    closed: list[bool] = []
    sent: list[dict[str, Any]] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        try:
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            yield _frame("token", content="Partial answer")
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    async def receive() -> dict[str, Any]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(dict(message))
        if b"Partial answer" in message.get("body", b""):
            disconnect.set()

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    await asyncio.wait_for(
        response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send),
        timeout=2,
    )
    assert closed == [True]
    assert not any(b'"kind": "completed"' in item.get("body", b"") for item in sent)


@pytest.mark.asyncio
async def test_adapter_failure_emits_error_instead_of_successful_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
        yield "event: broken\ndata: not-json\n\n"

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    raw = "".join([cast(str, frame) async for frame in response.body_iterator])
    assert '"type": "error"' in raw
    assert '"kind": "completed"' not in raw
    assert "not-json" not in raw


@pytest.mark.asyncio
async def test_native_heartbeat_reaches_client_as_sse_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
        yield _frame("heartbeat", elapsed_ms=10000)
        yield _frame("done", assistant_message_id=PAPER)

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    frames = [cast(str, frame) async for frame in response.body_iterator]
    assert frames[2] == ": keepalive\n\n"
    assert json.loads(frames[-1].removeprefix("data: ")) == {
        "type": "run_end",
        "kind": "completed",
    }


def test_no_bearer_auth_cannot_start_compatibility_chat() -> None:
    app = FastAPI()
    app.include_router(route.router)
    result = TestClient(app).post(
        "/assistant/v2/chat", json={"message": "Explain", "paperVersionId": PAPER}
    )
    assert result.status_code in {401, 403}


@pytest.mark.asyncio
async def test_run_start_exposes_actual_normalized_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.schemas.alphaxiv_files import AlphaXivFileReference

    body, user = _setup(monkeypatch)
    file = AlphaXivFileReference(
        content_type="text/plain", url=f"/api/v1/files/{RUN}/download"
    )
    body.files = [file]

    async def source(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
        yield _frame("done")

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    response = await route.chat_v2(body, _request(), user)
    frames = [cast(str, frame) async for frame in response.body_iterator]
    first = json.loads(frames[0].removeprefix("data: "))
    assert first["userFiles"] == [file.model_dump(by_alias=True)]
