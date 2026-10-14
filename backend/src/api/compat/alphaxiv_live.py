"""Detached native PDF turns and the observed alphaXiv live wire transport.

The native producer owns messages and run finalization. Durable replay is
owned by the compatibility service; sockets only read its authorized log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from src.api.agent import execute, streaming
from src.core.database import AsyncSessionLocal
from src.core.dependencies import get_current_user
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError
from src.models.user import User
from src.schemas.alphaxiv import (
    AlphaXivLiveAck,
    AlphaXivLiveChatRequest,
    AlphaXivStopRequest,
)
from src.services.agent.schemas import AgentExecuteRequest
from src.services.compat import alphaxiv_live as live_store
from src.services.compat.alphaxiv import (
    CompatibilityError,
    decode_native_frame,
    prepare_request,
)
from src.services.compat.alphaxiv_stop import stop_pdf_chat_run

router = APIRouter(tags=["alphaXiv compatibility"])
logger = logging.getLogger(__name__)

_LIVE_PRODUCERS: set[asyncio.Task[None]] = set()
_LIVE_POLL_SECONDS = 0.5
_MAX_CHANNELS = 8
_MAX_FRAME_CHARS = 8192
_MAX_FRAME_RECORDS = 16
_SEND_TIMEOUT_SECONDS = 10.0
_AUTH_RECHECK_SECONDS = 30.0


class _DetachedRequest:
    """A live turn belongs to its native run, never to an HTTP/socket reader."""

    def __init__(self, request: Request) -> None:
        self.headers = dict(request.headers)
        self.state = SimpleNamespace(
            request_id=getattr(request.state, "request_id", None)
        )

    async def is_disconnected(self) -> bool:
        return False


def _consume_ready_exception(ready: asyncio.Future[dict[str, str]]) -> None:
    # The HTTP caller may leave before acceptance. Retrieving its exception
    # prevents an abandoned waiter from producing an unhandled-future warning.
    if not ready.cancelled():
        ready.exception()


def _producer_finished(task: asyncio.Task[None]) -> None:
    _LIVE_PRODUCERS.discard(task)
    if not task.cancelled() and (failure := task.exception()) is not None:
        logger.error(
            "Detached PDF producer failed",
            exc_info=(type(failure), failure, failure.__traceback__),
        )


async def shutdown_live_producers() -> None:
    """Finalize detached native runs before closing their application pools."""
    pending = tuple(_LIVE_PRODUCERS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _drive_live_turn(
    body: AlphaXivLiveChatRequest,
    native: AgentExecuteRequest,
    request: _DetachedRequest,
    user: User,
    ready: asyncio.Future[dict[str, str]],
    started_at: float,
) -> None:
    source: AsyncGenerator[str, None] | None = None
    failure: HTTPException | None = None
    try:
        # First pull and every subsequent pull run in this same task, so
        # native cancellation ownership never points at the HTTP handler.
        source = cast(
            AsyncGenerator[str, None],
            streaming.stream_event_generator(
                native, request, user, request_started_at=started_at
            ),
        )
        first = await anext(source)
        kind, accepted = decode_native_frame(first)
        if kind != "status" or accepted.get("phase") != "accepted":
            code = {"conflict": 409, "invalid_request": 422}.get(
                str(accepted.get("category")), 503
            )
            raise HTTPException(code, "The response could not be started")
        chat_id = UUID(str(accepted["thread_id"]))
        run_id = UUID(str(accepted["run_id"]))
        if body.llm_chat_id is not None and chat_id != body.llm_chat_id:
            raise HTTPException(409, "The chat identity changed")
        actor_id = UUID(str(user.id))
        await live_store.start_live_run(
            actor_id=actor_id, chat_id=chat_id, run_id=run_id, body=body
        )
        ready.set_result({"llmChatId": str(chat_id), "runId": str(run_id)})

        await live_store.record_live_frame(
            actor_id=actor_id,
            chat_id=chat_id,
            run_id=run_id,
            kind=kind,
            payload=accepted,
        )
        async for frame in source:
            kind, payload = decode_native_frame(frame)
            # Terminal state is reconstructed from the native ledger. It is
            # already closed by the time the native terminal frame arrives.
            if kind not in {"heartbeat", "done", "error"}:
                await live_store.record_live_frame(
                    actor_id=actor_id,
                    chat_id=chat_id,
                    run_id=run_id,
                    kind=kind,
                    payload=payload,
                )
    except HTTPException as exc:
        failure = exc
    except CompatibilityError as exc:
        failure = HTTPException(exc.status_code, str(exc))
    except (KeyError, ValueError, StopAsyncIteration):
        failure = HTTPException(409, "A durable NOUS chat workspace is required")
        logger.warning("Invalid native acceptance or live frame", exc_info=True)
    except asyncio.CancelledError:
        failure = HTTPException(503, "The response was interrupted before acceptance")
        raise
    except Exception:
        failure = HTTPException(503, "The response could not be started")
        logger.exception("Detached PDF chat failed")
    finally:
        try:
            if source is not None:
                await streaming._run_interrupted_cleanup(source.aclose)
        finally:
            # Before-ack failures become visible only after the native writer
            # has released/finalized its accepted run, including replay errors.
            if not ready.done():
                ready.set_exception(
                    failure or HTTPException(503, "The response could not be started")
                )


@router.post("/assistant/v3/chat", response_model=AlphaXivLiveAck)
async def chat_v3(
    body: AlphaXivLiveChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    started_at = time.monotonic()
    await execute._enforce_rate_limit(
        execute._agent_rate_limiter, str(current_user.id), execute._AGENT_TURN_PREFIX
    )
    try:
        async with AsyncSessionLocal() as db:
            native = await prepare_request(body, db, current_user)
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    ready: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    ready.add_done_callback(_consume_ready_exception)
    task = asyncio.create_task(
        _drive_live_turn(
            body, native, _DetachedRequest(request), current_user, ready, started_at
        ),
        name="alphaxiv-live-producer",
    )
    _LIVE_PRODUCERS.add(task)
    task.add_done_callback(_producer_finished)
    # A dropped acknowledgement connection does not abandon an accepted run.
    return await asyncio.shield(ready)


@router.post("/assistant/v3/chat/stop", status_code=204, response_class=Response)
async def stop_v3(
    body: AlphaXivStopRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    await execute._enforce_rate_limit(
        execute._agent_read_rate_limiter,
        str(current_user.id),
        execute._AGENT_READ_PREFIX,
    )
    try:
        async with AsyncSessionLocal() as db:
            await stop_pdf_chat_run(
                db,
                caller=current_user,
                chat_id=body.llm_chat_id,
                run_id=str(body.run_id),
                request_id=getattr(request.state, "request_id", None),
            )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return Response(status_code=204)


def _channel_chat_id(channel: str) -> UUID:
    prefix, separator, value = channel.partition(";")
    if prefix != "assistant-chat" or not separator:
        raise ValueError("Unsupported live channel")
    return UUID(value)


def _snapshot_cursor(snapshot: Any) -> tuple[UUID | None, int]:
    if snapshot is None:
        return None, 0
    if not isinstance(snapshot, Mapping):
        raise ValueError("Invalid live snapshot")
    events = snapshot.get("events")
    if (
        not isinstance(events, list)
        or not events
        or not isinstance(events[0], dict)
        or events[0].get("type") != "run_start"
    ):
        raise ValueError("Invalid live run start")
    return UUID(str(snapshot["runId"])), len(events)


def _authentication_socket(websocket: WebSocket) -> WebSocket:
    """Adapt the observed token subprotocol to the native NOUS auth verifier."""
    prefix = "alphaxiv-live.auth."
    tokens = [
        protocol.removeprefix(prefix)
        for protocol in websocket.scope.get("subprotocols", [])
        if protocol.startswith(prefix)
    ]
    if len(tokens) > 1 or (tokens and not tokens[0]):
        raise WebSocketAuthError("Ambiguous live authentication")
    if not tokens:
        return websocket
    headers = dict(websocket.headers)
    if not headers.get("authorization", "").startswith("Bearer "):
        headers["sec-websocket-protocol"] = "auth, " + tokens[0]
    # Native authentication consumes headers only: preserve Origin and an
    # existing bearer header, and never accept cookies or query credentials.
    return cast(WebSocket, SimpleNamespace(headers=Headers(headers)))


@router.websocket("/live")
async def live_socket(websocket: WebSocket) -> None:
    try:
        auth_socket = _authentication_socket(websocket)
        auth = await WebSocketAuthenticator.authenticate(auth_socket)
        actor_id = UUID(str(auth["sub"]))
    except (WebSocketAuthError, KeyError, ValueError):
        await websocket.close(code=4001, reason="Authentication required")
        return
    protocols = websocket.scope.get("subprotocols", [])
    if "alphaxiv-live" not in protocols:
        await websocket.close(code=1002, reason="alphaxiv-live protocol required")
        return
    await websocket.accept(subprotocol="alphaxiv-live")

    send_lock = asyncio.Lock()
    auth_lock = asyncio.Lock()
    authenticated_at = time.monotonic()
    subscriptions: dict[str, asyncio.Task[None]] = {}

    async def refresh_auth() -> None:
        nonlocal auth, authenticated_at
        async with auth_lock:
            expires_at = auth.get("exp")
            # Native token verification returns naive UTC; retain the instant
            # of aware expiries while giving the native shape its UTC marker.
            if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            expired = isinstance(expires_at, datetime) and expires_at <= datetime.now(
                timezone.utc
            )
            if (
                not expired
                and time.monotonic() - authenticated_at < _AUTH_RECHECK_SECONDS
            ):
                return
            refreshed = await WebSocketAuthenticator.authenticate(auth_socket)
            if UUID(str(refreshed["sub"])) != actor_id:
                raise WebSocketAuthError("Authentication changed")
            auth = refreshed
            authenticated_at = time.monotonic()

    async def send_record(code: str, payload: Any, *, raw: bool = False) -> None:
        frame = code + (
            str(payload) if raw else json.dumps(payload, ensure_ascii=False)
        )
        async with send_lock:
            await asyncio.wait_for(
                websocket.send_text(frame), timeout=_SEND_TIMEOUT_SECONDS
            )

    async def follow(channel: str, chat_id: UUID) -> None:
        subscribed = False
        try:
            await refresh_auth()
            snapshot = await live_store.read_live_snapshot(
                actor_id=actor_id, chat_id=chat_id
            )
            run_id, after_seq = _snapshot_cursor(snapshot)
            await send_record("a", [channel, snapshot])
            subscribed = True
            while True:
                await asyncio.sleep(_LIVE_POLL_SECONDS)
                await refresh_auth()
                update = await live_store.read_live_update(
                    actor_id=actor_id,
                    chat_id=chat_id,
                    known_run_id=run_id,
                    after_seq=after_seq,
                )
                if update["type"] == "replace":
                    snapshot = update["payload"]
                    run_id, after_seq = _snapshot_cursor(snapshot)
                    await send_record("e", [channel, snapshot])
                elif update["type"] == "delta":
                    for payload in update["payloads"]:
                        if (
                            UUID(str(payload["runId"])) != run_id
                            or payload["seq"] != after_seq + 1
                        ):
                            # Never emit a gap or duplicate. A fresh whole-log
                            # replacement gives the client its own replay cursor.
                            snapshot = await live_store.read_live_snapshot(
                                actor_id=actor_id, chat_id=chat_id
                            )
                            run_id, after_seq = _snapshot_cursor(snapshot)
                            await send_record("e", [channel, snapshot])
                            break
                        await send_record("d", [channel, payload])
                        after_seq = payload["seq"]
                else:
                    raise ValueError("Invalid live update")
        except WebSocketAuthError:
            if subscribed:
                await send_record("c", channel, raw=True)
            await websocket.close(code=4001, reason="Authentication required")
        except CompatibilityError as exc:
            if subscribed and exc.status_code in {401, 403, 404}:
                await send_record("c", channel, raw=True)
            else:
                await send_record("b", [channel, "Chat subscription unavailable"])
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            # A follower's failed socket send never affects the producer.
            await websocket.close(code=1011, reason="Live connection unavailable")
        except Exception:
            logger.exception("PDF live subscription failed")
            await send_record("b", [channel, "Chat subscription unavailable"])
        finally:
            if subscriptions.get(channel) is asyncio.current_task():
                subscriptions.pop(channel, None)

    async def unsubscribe(channel: str) -> None:
        task = subscriptions.pop(channel, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def follower_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("PDF live follower ended after connection failure")

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            frame = message.get("text")
            if not isinstance(frame, str):
                await websocket.close(code=1003, reason="Text live frames required")
                return
            if len(frame) > _MAX_FRAME_CHARS:
                await websocket.close(code=1009, reason="Live frame too large")
                return
            records = frame.splitlines()
            if len(records) > _MAX_FRAME_RECORDS:
                await websocket.close(code=1008, reason="Too many live records")
                return
            for record in records:
                if not record:
                    continue
                code, channel = record[0], record[1:]
                try:
                    chat_id = _channel_chat_id(channel)
                except ValueError:
                    await send_record("b", [channel, "Unsupported live channel"])
                    continue
                if code == "b":
                    await unsubscribe(channel)
                elif code == "a":
                    await unsubscribe(channel)
                    if len(subscriptions) >= _MAX_CHANNELS:
                        await send_record("b", [channel, "Too many live subscriptions"])
                        continue
                    task = asyncio.create_task(
                        follow(channel, chat_id), name="alphaxiv-live-follower"
                    )
                    subscriptions[channel] = task
                    task.add_done_callback(follower_finished)
                else:
                    await send_record("b", [channel, "Unsupported live command"])
    except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
        pass
    finally:
        pending = tuple(subscriptions.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
