"""Audit I11: WebSocket inbound frames had no size or rate cap.

Every frame up to the server's 16 MiB ``ws_max_size`` was ``json.loads``'d and
a ping-flood got one PONG send per frame. ``handle_client_message`` (the one
place every v2 inbound frame passes) now closes oversized frames with 1009
before parsing, and closes a connection that exceeds the per-connection
fixed-window message rate with one ``rate_limited`` error + 1008.

Mutation check: deleting the size guard in ``handle_client_message`` fails
``test_oversized_frame_closes_1009_without_parsing``; deleting the rate guard
fails ``test_message_over_window_cap_sends_one_error_and_closes_1008``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status

from src.services.websocket import websocket_manager as wm
from src.services.websocket.websocket_manager import (
    ConnectionInfo,
    EnhancedConnectionManager,
)

pytestmark = pytest.mark.unit

CID = "conn-1"
PING = json.dumps({"type": "ping"})


def _mgr() -> tuple[EnhancedConnectionManager, AsyncMock]:
    mgr = EnhancedConnectionManager()
    mgr._log_connection_event = AsyncMock()  # type: ignore[method-assign]
    mgr._record_message_metrics = AsyncMock()  # type: ignore[method-assign]
    ws = AsyncMock()
    now = datetime.now(timezone.utc)
    mgr.active_connections[CID] = ConnectionInfo(
        user_id="u1",
        organization_id="org1",
        connection_id=CID,
        websocket=ws,
        connected_at=now,
        last_heartbeat=now,
        subscribed_channels=set(),
    )
    mgr.user_connections["u1"].add(CID)
    mgr.organization_connections["org1"].add(CID)
    return mgr, ws


def _sent(ws: AsyncMock) -> list[dict[str, Any]]:
    return [c.args[0] for c in ws.send_json.await_args_list]


@pytest.mark.asyncio
async def test_oversized_frame_closes_1009_without_parsing() -> None:
    mgr, ws = _mgr()
    big = "x" * (wm.WS_MAX_INBOUND_MESSAGE_BYTES + 1)

    with patch.object(wm.json, "loads") as loads:
        await mgr.handle_client_message(CID, big)

    loads.assert_not_called()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs["code"] == status.WS_1009_MESSAGE_TOO_BIG
    assert CID not in mgr.active_connections


@pytest.mark.asyncio
async def test_multibyte_frame_is_measured_in_bytes() -> None:
    mgr, ws = _mgr()
    # Under the cap in characters, over it in UTF-8 bytes (3 bytes/char).
    big = "€" * (wm.WS_MAX_INBOUND_MESSAGE_BYTES // 2)

    await mgr.handle_client_message(CID, big)

    assert ws.close.await_args.kwargs["code"] == status.WS_1009_MESSAGE_TOO_BIG


@pytest.mark.asyncio
async def test_frame_at_cap_is_processed() -> None:
    mgr, ws = _mgr()
    head, tail = '{"type": "ping", "pad": "', '"}'
    pad = "x" * (wm.WS_MAX_INBOUND_MESSAGE_BYTES - len(head) - len(tail))
    padded = head + pad + tail
    assert len(padded.encode()) == wm.WS_MAX_INBOUND_MESSAGE_BYTES

    await mgr.handle_client_message(CID, padded)

    ws.close.assert_not_awaited()
    assert _sent(ws)[0]["type"] == "pong"


@pytest.mark.asyncio
async def test_messages_up_to_window_cap_are_all_answered() -> None:
    mgr, ws = _mgr()

    for _ in range(wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW):
        await mgr.handle_client_message(CID, PING)

    ws.close.assert_not_awaited()
    sent = _sent(ws)
    assert len(sent) == wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW
    assert all(m["type"] == "pong" for m in sent)


@pytest.mark.asyncio
async def test_message_over_window_cap_sends_one_error_and_closes_1008() -> None:
    mgr, ws = _mgr()

    for _ in range(wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW + 5):
        await mgr.handle_client_message(CID, PING)

    errors = [m for m in _sent(ws) if m["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["data"]["error"] == "rate_limited"
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs["code"] == status.WS_1008_POLICY_VIOLATION
    assert CID not in mgr.active_connections
    # Nothing after the cap was answered: N pongs + 1 error.
    assert len(_sent(ws)) == wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW + 1


@pytest.mark.asyncio
async def test_window_resets_after_window_seconds() -> None:
    mgr, ws = _mgr()
    info = mgr.active_connections[CID]

    for _ in range(wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW):
        await mgr.handle_client_message(CID, PING)
    # Simulate the window elapsing (no global clock patching: asyncio uses
    # time.monotonic too).
    info.inbound_window_start -= wm.WS_INBOUND_WINDOW_SECONDS

    for _ in range(wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW):
        await mgr.handle_client_message(CID, PING)

    ws.close.assert_not_awaited()
    assert len(_sent(ws)) == 2 * wm.WS_MAX_INBOUND_MESSAGES_PER_WINDOW
