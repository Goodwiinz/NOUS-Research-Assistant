"""Live wire/lifecycle contracts around the real transport and injected native IO.

These tests establish transport behavior, not hosted runtime or model quality.
The replay service and exact-run stop have separate database-backed coverage.

Mutation proof (2026-10-13), src/api/compat/alphaxiv_live.py:
- Moving ready.set_result before start_live_run at line121 made
  ``python -m pytest -q backend/tests/unit/api/test_alphaxiv_live.py::
  test_ack_waits_for_durable_start_and_same_task_drives_all_pulls`` fail
  because the handler completed before the durable-start barrier.
- Removing the dense sequence check in live_socket.follow at line347 made
  ``python -m pytest -q backend/tests/unit/api/test_alphaxiv_live.py::
  test_live_gap_uses_full_replacement_and_revocation_removes_follower`` fail
  with a delta ('d') instead of the required complete replacement ('e').
Both mutations were restored byte-for-byte before rerunning this suite.
"""

import asyncio
import copy
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from jose import jwt
from starlette.datastructures import Headers

from src.api.compat import alphaxiv_live as route
from src.core import security
from src.core.websocket_auth import WebSocketAuthError
from src.models.user import User
from src.schemas.alphaxiv import AlphaXivLiveChatRequest, AlphaXivStopRequest
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.compat.alphaxiv import CompatibilityError
from src.shared.enums import JobStatus

PAPER = "11111111-1111-4111-8111-111111111111"
CHAT = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
NEXT_RUN = "55555555-5555-4555-8555-555555555555"
CHANNEL = f"assistant-chat;{CHAT}"


def _frame(kind: str, **payload: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


def _request() -> Request:
    return Request(
        {"type": "http", "headers": [], "state": {}, "path": "/assistant/v3/chat"}
    )


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[AlphaXivLiveChatRequest, User]:
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
    monkeypatch.setattr(route.live_store, "start_live_run", AsyncMock())
    recorded = AsyncMock()

    class _Recorder:
        """Producer-side frame writer; records (kind, payload) per write."""

        instances: list[dict[str, Any]] = []
        record_mock = recorded

        def __init__(self, **kwargs: Any) -> None:
            self.instances.append(kwargs)

        async def record(self, kind: str, payload: dict[str, Any]) -> None:
            await recorded(kind=kind, payload=payload)

    monkeypatch.setattr(route.live_store, "LiveRecorder", _Recorder)
    return (
        AlphaXivLiveChatRequest.model_validate(
            {"message": "Explain", "paperVersionId": PAPER}
        ),
        cast(User, SimpleNamespace(id=UUID(USER), organization_id=UUID(PAPER))),
    )


def _recorded() -> AsyncMock:
    """The (kind, payload) writes of the fake producer recorder from _setup."""
    return cast(AsyncMock, getattr(route.live_store.LiveRecorder, "record_mock"))


@pytest.fixture(autouse=True)
async def _close_producers() -> AsyncGenerator[None, None]:
    yield
    await route.shutdown_live_producers()


@pytest.mark.asyncio
async def test_ack_waits_for_durable_start_and_same_task_drives_all_pulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    writing_start, durable_start, release_model = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    pulls: list[asyncio.Task[Any] | None] = []
    closed: list[bool] = []

    async def start(**kwargs: Any) -> None:
        writing_start.set()
        await durable_start.wait()

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            pulls.append(asyncio.current_task())
            assert not await args[1].is_disconnected()
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            await release_model.wait()
            pulls.append(asyncio.current_task())
            yield _frame("token", content="answer")
            pulls.append(asyncio.current_task())
            yield _frame("done", assistant_message_id=PAPER)
        finally:
            closed.append(True)

    monkeypatch.setattr(route.live_store, "start_live_run", start)
    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    handler = asyncio.create_task(route.chat_v3(body, _request(), user))
    await asyncio.wait_for(writing_start.wait(), 1)
    completed, _ = await asyncio.wait({handler}, timeout=0.02)
    assert not completed
    durable_start.set()
    assert await asyncio.wait_for(handler, 1) == {"llmChatId": CHAT, "runId": RUN}
    producers = tuple(route._LIVE_PRODUCERS)
    assert len(producers) == 1
    assert not producers[0].done()
    release_model.set()
    await asyncio.gather(*producers)
    assert len(pulls) == 3 and all(task is producers[0] for task in pulls)
    assert pulls[0] is not handler
    assert closed == [True]
    kinds = [call.kwargs["kind"] for call in _recorded().await_args_list]
    assert kinds == ["status", "token"]
    # One recorder per producer, bound to the validated presentation.
    assert getattr(route.live_store.LiveRecorder, "instances") == [
        {
            "actor_id": UUID(USER),
            "chat_id": UUID(CHAT),
            "run_id": UUID(RUN),
            "paper_id": UUID(PAPER),
        }
    ]


@pytest.mark.asyncio
async def test_http_waiter_cancellation_does_not_cancel_accepted_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    writing_start, durable_start = asyncio.Event(), asyncio.Event()
    completed: list[bool] = []

    async def start(**kwargs: Any) -> None:
        writing_start.set()
        await durable_start.wait()

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
        completed.append(True)
        yield _frame("done")

    monkeypatch.setattr(route.live_store, "start_live_run", start)
    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    handler = asyncio.create_task(route.chat_v3(body, _request(), user))
    await asyncio.wait_for(writing_start.wait(), 1)
    producers = tuple(route._LIVE_PRODUCERS)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert len(producers) == 1 and not producers[0].done()
    durable_start.set()
    await asyncio.gather(*producers)
    assert completed == [True]


@pytest.mark.asyncio
async def test_replay_start_failure_closes_native_source_before_failed_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    closed: list[bool] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            pytest.fail("failed durable replay must not advance generation")
        finally:
            closed.append(True)

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    cast(AsyncMock, route.live_store.start_live_run).side_effect = RuntimeError(
        "database unavailable"
    )
    with pytest.raises(HTTPException) as caught:
        await route.chat_v3(body, _request(), user)
    assert caught.value.status_code == 503
    assert closed == [True]
    _recorded().assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_closes_native_producer_without_writing_fake_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    closed: list[bool] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    await route.chat_v3(body, _request(), user)
    await route.shutdown_live_producers()
    assert closed == [True]
    assert not route._LIVE_PRODUCERS
    assert all(call.kwargs["kind"] == "status" for call in _recorded().await_args_list)


@pytest.mark.asyncio
async def test_stop_passes_exact_chat_run_and_native_read_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, user = _setup(monkeypatch)
    stop = AsyncMock(
        return_value=SimpleNamespace(
            run_id=RUN, status=JobStatus.STOPPING, claimed=True
        )
    )
    monkeypatch.setattr(route, "stop_pdf_chat_run", stop)
    result = await route.stop_v3(
        AlphaXivStopRequest.model_validate({"llmChatId": CHAT, "runId": RUN}),
        _request(),
        user,
    )
    assert result.status_code == 204
    assert result.body == b""
    assert stop.await_args is not None
    assert stop.await_args.kwargs["chat_id"] == UUID(CHAT)
    assert stop.await_args.kwargs["run_id"] == RUN
    assert stop.await_args.kwargs["caller"] is user
    cast(AsyncMock, route.execute._enforce_rate_limit).assert_awaited_once_with(
        route.execute._agent_read_rate_limiter, USER, route.execute._AGENT_READ_PREFIX
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_stop_http_success_is_empty_and_documented_as_no_content(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    _, user = _setup(monkeypatch)
    monkeypatch.setattr(
        route,
        "stop_pdf_chat_run",
        AsyncMock(
            return_value=SimpleNamespace(
                run_id=RUN,
                status=JobStatus.CANCELLED if cancelled else JobStatus.STOPPING,
                claimed=not cancelled,
            )
        ),
    )
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.get_current_user] = lambda: user
    response = TestClient(app).post(
        "/assistant/v3/chat/stop", json={"llmChatId": CHAT, "runId": RUN}
    )
    assert response.status_code == 204
    assert response.content == b""
    assert "content-type" not in response.headers
    responses = app.openapi()["paths"]["/assistant/v3/chat/stop"]["post"]["responses"]
    assert "content" not in responses["204"]
    assert "200" not in responses


def test_stop_http_conflict_keeps_safe_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, user = _setup(monkeypatch)
    monkeypatch.setattr(
        route,
        "stop_pdf_chat_run",
        AsyncMock(side_effect=CompatibilityError(409, "Run is already complete")),
    )
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.get_current_user] = lambda: user
    response = TestClient(app).post(
        "/assistant/v3/chat/stop", json={"llmChatId": CHAT, "runId": RUN}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Run is already complete"}


class _Socket:
    def __init__(self) -> None:
        self.scope = {"subprotocols": ["alphaxiv-live"]}
        self.headers = Headers({"origin": "https://nous.example"})
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[str] = asyncio.Queue()
        self.accepted: str | None = None
        self.closed: list[int] = []

    async def accept(self, *, subprotocol: str) -> None:
        self.accepted = subprotocol

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send_text(self, text: str) -> None:
        await self.outgoing.put(text)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append(code)
        await self.incoming.put({"type": "websocket.disconnect"})

    async def send(self, text: str) -> None:
        await self.incoming.put({"type": "websocket.receive", "text": text})

    async def next(self) -> str:
        return await asyncio.wait_for(self.outgoing.get(), 1)


def _socket_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Socket, dict[str, Any]]:
    monkeypatch.setattr(
        route.WebSocketAuthenticator,
        "authenticate",
        AsyncMock(return_value={"sub": USER}),
    )
    monkeypatch.setattr(route, "_LIVE_POLL_SECONDS", 0.001)
    state: dict[str, Any] = {
        "snapshot": {"runId": RUN, "events": [{"type": "run_start"}]},
        "revoked": False,
    }

    async def snapshot(**kwargs: Any) -> Any:
        assert kwargs == {"actor_id": UUID(USER), "chat_id": UUID(CHAT)}
        if state["revoked"]:
            raise CompatibilityError(404, "Chat not found")
        return copy.deepcopy(state["snapshot"])

    async def update(**kwargs: Any) -> dict[str, Any]:
        if state.get("fail") is not None:
            raise state["fail"]
        current = await snapshot(actor_id=kwargs["actor_id"], chat_id=kwargs["chat_id"])
        if current is None or current["runId"] != str(kwargs["known_run_id"]):
            return {"type": "replace", "payload": current}
        return {
            "type": "delta",
            "payloads": [
                {"runId": current["runId"], "seq": index + 1, "event": event}
                for index, event in enumerate(current["events"])
                if index >= kwargs["after_seq"]
            ],
        }

    monkeypatch.setattr(route.live_store, "read_live_snapshot", snapshot)
    monkeypatch.setattr(route.live_store, "read_live_update", update)
    return _Socket(), state


@pytest.mark.asyncio
async def test_live_snapshot_dense_deltas_new_run_and_unsubscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, state = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        first = await socket.next()
        assert first[0] == "a"
        assert json.loads(first[1:]) == [CHANNEL, state["snapshot"]]
        assert socket.accepted == "alphaxiv-live"
        event = {"type": "delta_output_text", "index": 0, "delta": "line 1\nline 2"}
        state["snapshot"]["events"].append(event)
        delta = await socket.next()
        assert "\n" not in delta
        assert json.loads(delta[1:]) == [
            CHANNEL,
            {"runId": RUN, "seq": 2, "event": event},
        ]
        assert delta[0] == "d"
        state["snapshot"] = {"runId": NEXT_RUN, "events": [{"type": "run_start"}]}
        replacement = await socket.next()
        assert replacement[0] == "e"
        assert json.loads(replacement[1:]) == [CHANNEL, state["snapshot"]]
        # The second command's response is a barrier: unsubscribe has finished.
        await socket.send("b" + CHANNEL + "\nx" + CHANNEL)
        assert (await socket.next()).startswith("b")
        assert socket.outgoing.empty()
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
async def test_socket_disconnect_does_not_stop_the_native_live_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, user = _setup(monkeypatch)
    release_model = asyncio.Event()
    closed: list[bool] = []

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
            await release_model.wait()
            yield _frame("done")
        finally:
            closed.append(True)

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    await route.chat_v3(body, _request(), user)
    producers = tuple(route._LIVE_PRODUCERS)
    socket, _ = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    await socket.send("a" + CHANNEL)
    await socket.next()
    await socket.incoming.put({"type": "websocket.disconnect"})
    await asyncio.wait_for(serving, 1)
    assert closed == []
    assert len(producers) == 1 and not producers[0].done()
    release_model.set()
    await asyncio.gather(*producers)
    assert closed == [True]


@pytest.mark.asyncio
async def test_live_gap_uses_full_replacement_and_revocation_removes_follower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, state = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        await socket.next()
        monkeypatch.setattr(
            route.live_store,
            "read_live_update",
            AsyncMock(
                return_value={
                    "type": "delta",
                    "payloads": [
                        {"runId": RUN, "seq": 3, "event": {"type": "run_end"}}
                    ],
                }
            ),
        )
        replacement = await socket.next()
        assert replacement[0] == "e"
        assert json.loads(replacement[1:])[1] == state["snapshot"]
        state["revoked"] = True
        assert await socket.next() == "c" + CHANNEL
        assert socket.outgoing.empty()
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
async def test_expired_or_revoked_token_closes_live_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    await socket.send("a" + CHANNEL)
    await socket.next()
    monkeypatch.setattr(route, "_AUTH_RECHECK_SECONDS", 0)
    cast(AsyncMock, route.WebSocketAuthenticator.authenticate).side_effect = (
        WebSocketAuthError("revoked")
    )
    assert await socket.next() == "c" + CHANNEL
    await asyncio.wait_for(serving, 1)
    assert socket.closed == [4001]


@pytest.mark.asyncio
async def test_native_verified_token_expiry_allows_live_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_authenticate = route.WebSocketAuthenticator.authenticate
    socket, state = _socket_setup(monkeypatch)
    secret = "alphaxiv-live-expiry-test-only-secret"
    monkeypatch.setattr(security.settings, "SUPABASE_JWT_SECRET", secret)
    monkeypatch.setattr(security.settings, "SUPABASE_JWT_ISSUER", "")
    token = jwt.encode(
        {
            "sub": USER,
            "aud": "authenticated",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )
    socket.headers = Headers({"authorization": f"Bearer {token}"})
    auth = await native_authenticate(cast(WebSocket, socket))
    assert isinstance(auth["exp"], datetime)
    assert auth["exp"].tzinfo is None  # The native verifier's UTC contract.
    authenticate = AsyncMock(wraps=native_authenticate)
    monkeypatch.setattr(route.WebSocketAuthenticator, "authenticate", authenticate)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        assert await socket.next() == "a" + json.dumps([CHANNEL, state["snapshot"]])
        state["snapshot"]["events"].append({"type": "run_end"})
        assert (await socket.next()).startswith("d")
        authenticate.assert_awaited_once()
        assert socket.closed == []
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_hours", [0, -8, 8])
async def test_aware_future_token_expiry_preserves_its_instant(
    monkeypatch: pytest.MonkeyPatch, offset_hours: int
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(
        timezone(timedelta(hours=offset_hours))
    )
    authenticate = cast(AsyncMock, route.WebSocketAuthenticator.authenticate)
    authenticate.return_value = {"sub": USER, "exp": expires_at}
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        assert (await socket.next()).startswith("a")
        authenticate.assert_awaited_once()
        assert socket.closed == []
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_hours", [None, 0, 8], ids=["naive", "utc", "offset"])
@pytest.mark.parametrize("already_subscribed", [False, True])
async def test_expiry_forces_auth_before_the_periodic_recheck(
    monkeypatch: pytest.MonkeyPatch,
    offset_hours: int | None,
    already_subscribed: bool,
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    auth: dict[str, Any] = {
        "sub": USER,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    authenticate = cast(AsyncMock, route.WebSocketAuthenticator.authenticate)
    authenticate.return_value = auth
    snapshot = AsyncMock(wraps=route.live_store.read_live_snapshot)
    monkeypatch.setattr(route.live_store, "read_live_snapshot", snapshot)
    # Freeze elapsed-time reauthentication: only the token expiry may force it.
    monkeypatch.setattr(route, "time", SimpleNamespace(monotonic=lambda: 1.0))
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        if already_subscribed:
            await socket.send("a" + CHANNEL)
            assert (await socket.next()).startswith("a")
            authenticate.side_effect = WebSocketAuthError("expired")
        else:
            authenticate.side_effect = [auth, WebSocketAuthError("expired")]
        expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        auth["exp"] = (
            expires_at.replace(tzinfo=None)
            if offset_hours is None
            else expires_at.astimezone(timezone(timedelta(hours=offset_hours)))
        )
        if already_subscribed:
            assert await socket.next() == "c" + CHANNEL
        else:
            await socket.send("a" + CHANNEL)
        await asyncio.wait_for(serving, 1)
        assert authenticate.await_count == 2
        assert socket.closed == [4001]
        assert socket.outgoing.empty()
        if not already_subscribed:
            snapshot.assert_not_awaited()
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
async def test_binary_live_frame_closes_with_unsupported_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    await socket.incoming.put({"type": "websocket.receive", "bytes": b"binary"})
    await asyncio.wait_for(serving, 1)
    assert socket.closed == [1003]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "close_code"),
    [("a" * (route._MAX_FRAME_CHARS + 1), 1009), ("a\n" * 17, 1008)],
    ids=["oversized", "too-many-records"],
)
async def test_live_input_bounds(
    monkeypatch: pytest.MonkeyPatch, frame: str, close_code: int
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    await socket.send(frame)
    await asyncio.wait_for(serving, 1)
    assert socket.closed == [close_code]


@pytest.mark.asyncio
async def test_live_auth_failure_does_not_accept_or_read_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    cast(AsyncMock, route.WebSocketAuthenticator.authenticate).side_effect = (
        WebSocketAuthError("missing token")
    )
    replay = AsyncMock()
    monkeypatch.setattr(route.live_store, "read_live_snapshot", replay)
    await route.live_socket(cast(WebSocket, socket))
    assert socket.accepted is None
    assert socket.closed == [4001]
    replay.assert_not_awaited()


@pytest.mark.asyncio
async def test_observed_token_subprotocol_preserves_native_origin_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    socket.scope["subprotocols"] = ["alphaxiv-live", "alphaxiv-live.auth.nous-token"]
    await socket.incoming.put({"type": "websocket.disconnect"})
    await route.live_socket(cast(WebSocket, socket))
    authenticator = cast(AsyncMock, route.WebSocketAuthenticator.authenticate)
    assert authenticator.await_args is not None
    headers = authenticator.await_args.args[0].headers
    assert headers["origin"] == "https://nous.example"
    assert headers["sec-websocket-protocol"] == "auth, nous-token"
    assert socket.accepted == "alphaxiv-live"


def test_existing_bearer_takes_precedence_over_observed_token_subprotocol() -> None:
    socket = _Socket()
    socket.scope["subprotocols"] = ["alphaxiv-live", "alphaxiv-live.auth.other-token"]
    socket.headers = Headers(
        {"authorization": "Bearer preferred", "origin": "https://nous.example"}
    )
    adapted = route._authentication_socket(cast(WebSocket, socket))
    assert adapted.headers["authorization"] == "Bearer preferred"
    assert adapted.headers["origin"] == "https://nous.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tokens",
    [["alphaxiv-live.auth."], ["alphaxiv-live.auth.one", "alphaxiv-live.auth.two"]],
)
async def test_ambiguous_or_empty_subprotocol_token_rejected_before_auth(
    monkeypatch: pytest.MonkeyPatch, tokens: list[str]
) -> None:
    socket, _ = _socket_setup(monkeypatch)
    socket.scope["subprotocols"] = ["alphaxiv-live", *tokens]
    await route.live_socket(cast(WebSocket, socket))
    cast(AsyncMock, route.WebSocketAuthenticator.authenticate).assert_not_awaited()
    assert socket.closed == [4001]
    assert socket.accepted is None


def test_v3_chat_requires_bearer_and_protocol_is_not_a_v3_field() -> None:
    app = FastAPI()
    app.include_router(route.router)
    response = TestClient(app).post(
        "/assistant/v3/chat", json={"message": "Explain", "paperVersionId": PAPER}
    )
    assert response.status_code in {401, 403}
    schema = app.openapi()
    request_schema = schema["components"]["schemas"]["AlphaXivLiveChatRequest"]
    assert "protocol" not in request_schema["properties"]
    assert (
        "llmChatId" in schema["components"]["schemas"]["AlphaXivLiveAck"]["properties"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [ConnectionRefusedError("postgres down"), RuntimeError("bad projection")],
    ids=["db-outage-oserror", "runtime-error"],
)
async def test_live_read_failure_is_logged_and_reported_not_a_silent_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    socket, state = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        assert (await socket.next()).startswith("a")
        with caplog.at_level(logging.ERROR, logger=route.logger.name):
            state["fail"] = failure
            reply = await socket.next()
        assert reply[0] == "b"
        assert json.loads(reply[1:]) == [CHANNEL, "Chat subscription unavailable"]
        assert socket.closed == []
        logged = [r for r in caplog.records if r.name == route.logger.name]
        assert logged and logged[-1].exc_info is not None
        assert logged[-1].exc_info[1] is failure
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
async def test_live_5xx_compatibility_error_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    socket, state = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        assert (await socket.next()).startswith("a")
        with caplog.at_level(logging.ERROR, logger=route.logger.name):
            state["fail"] = CompatibilityError(503, "Live replay too large")
            assert (await socket.next())[0] == "b"
        logged = [r for r in caplog.records if r.name == route.logger.name]
        assert logged and "503" in logged[-1].getMessage()
        assert logged[-1].exc_info is not None
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
async def test_failed_send_closes_1011_without_an_error_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    socket, state = _socket_setup(monkeypatch)

    async def broken_send(text: str) -> None:
        raise OSError("peer reset")

    socket.send_text = broken_send  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=route.logger.name):
        await socket.send("a" + CHANNEL)
        await asyncio.wait_for(route.live_socket(cast(WebSocket, socket)), 1)
    assert socket.closed == [1011]
    assert not any(
        r.levelno >= logging.ERROR
        for r in caplog.records
        if r.name == route.logger.name
    )


@pytest.mark.asyncio
async def test_follower_finished_logs_escaped_failure_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    socket, state = _socket_setup(monkeypatch)
    serving = asyncio.create_task(route.live_socket(cast(WebSocket, socket)))
    try:
        await socket.send("a" + CHANNEL)
        assert (await socket.next()).startswith("a")
        failed = asyncio.Event()

        async def broken_send(text: str) -> None:
            failed.set()
            raise RuntimeError("closed")

        socket.send_text = broken_send  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger=route.logger.name):
            # Revocation makes the follower reply "c"; that reply's send fails
            # inside the handler and escapes to the task's done callback.
            state["revoked"] = True
            await asyncio.wait_for(failed.wait(), 1)
            for _ in range(20):
                if any("follower ended" in r.getMessage() for r in caplog.records):
                    break
                await asyncio.sleep(0.01)
        logged = [r for r in caplog.records if "follower ended" in r.getMessage()]
        assert len(logged) == 1
        assert logged[0].exc_info is not None
        assert isinstance(logged[0].exc_info[1], route._SendFailed)
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(serving, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [CompatibilityError(503, "unavailable"), RuntimeError("database unavailable")],
    ids=["compatibility", "unexpected"],
)
async def test_producer_failure_after_ack_is_logged_with_chat_and_run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    body, user = _setup(monkeypatch)

    async def source(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        yield _frame("status", phase="accepted", thread_id=CHAT, run_id=RUN)
        yield _frame("token", content="answer")

    monkeypatch.setattr(route.streaming, "stream_event_generator", source)
    _recorded().side_effect = [None, failure]
    with caplog.at_level(logging.ERROR, logger=route.logger.name):
        assert await route.chat_v3(body, _request(), user) == {
            "llmChatId": CHAT,
            "runId": RUN,
        }
        await asyncio.gather(*tuple(route._LIVE_PRODUCERS))
    logged = [r for r in caplog.records if "after acceptance" in r.getMessage()]
    assert len(logged) == 1
    assert CHAT in logged[0].getMessage() and RUN in logged[0].getMessage()
    assert logged[0].exc_info is not None and logged[0].exc_info[1] is failure
