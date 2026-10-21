import asyncio
from typing import Any

import pytest

from src.middleware.disconnect_signal import (
    AGENT_DISCONNECT_EVENT,
    AgentDisconnectSignalMiddleware,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/assistant/v2/chat", "/api/v1/agent/stream"])
async def test_shared_disconnect_event_covers_both_chat_transports(path: str) -> None:
    received: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        signal = scope["state"][AGENT_DISCONNECT_EVENT]
        assert not signal.is_set()
        assert (await receive())["type"] == "http.disconnect"
        received.append(signal.is_set())

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    await AgentDisconnectSignalMiddleware(app)(
        {"type": "http", "path": path}, receive, send
    )
    assert received == [True]
