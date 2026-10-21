"""Preserve agent-stream disconnects across middleware receive wrappers."""

import asyncio
import contextlib

from starlette.types import ASGIApp, Message, Receive, Scope, Send

AGENT_DISCONNECT_EVENT = "agent_disconnect_event"


class AgentDisconnectSignalMiddleware:
    """Expose a shared event no matter which middleware consumes disconnect."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path.startswith("/api/v1/agent/stream") or path == "/assistant/v2/chat"
        ):
            await self.app(scope, receive, send)
            return

        disconnected = asyncio.Event()
        scope.setdefault("state", {})[AGENT_DISCONNECT_EVENT] = disconnected
        messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)

        async def pump_receive() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                await messages.put(message)
                if message["type"] == "http.disconnect":
                    return

        pump = asyncio.create_task(pump_receive())
        try:
            await self.app(scope, messages.get, send)
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
