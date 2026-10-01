"""Audit I11: the ``client_info`` query param was ``json.loads``'d with no
length or shape check and stored on the connection for its lifetime.

``_parse_client_info`` bounds it (raw length, dict-only, scalar values,
truncated strings, key count). The endpoint test also pins that the
server-stamped ``role`` is applied AFTER the client dict, so a client cannot
claim ``role=admin``, and that the receive loop exits once the manager has
dropped the connection (e.g. after an inbound-cap close).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from src.api.realtime import websocket_v2 as v2
from src.api.realtime.websocket_v2 import _parse_client_info

pytestmark = pytest.mark.unit


def test_oversized_client_info_is_dropped() -> None:
    raw = json.dumps({"k": "x" * v2.CLIENT_INFO_MAX_CHARS})
    assert len(raw) > v2.CLIENT_INFO_MAX_CHARS
    assert _parse_client_info(raw) == {}


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"str"', "42", "null"])
def test_invalid_or_non_dict_client_info_is_empty(raw: str) -> None:
    assert _parse_client_info(raw) == {}


def test_nested_values_dropped_and_scalars_kept() -> None:
    raw = json.dumps(
        {"browser": "ff", "v": 3, "f": 1.5, "b": True, "n": None, "o": {}, "l": [1]}
    )
    assert _parse_client_info(raw) == {
        "browser": "ff",
        "v": 3,
        "f": 1.5,
        "b": True,
        "n": None,
    }


def test_long_strings_truncated() -> None:
    raw = json.dumps({"ua": "y" * 1000})
    assert _parse_client_info(raw)["ua"] == "y" * v2.CLIENT_INFO_MAX_VALUE_CHARS


def test_key_count_capped() -> None:
    raw = json.dumps({f"k{i}": i for i in range(100)})
    assert len(_parse_client_info(raw)) == v2.CLIENT_INFO_MAX_KEYS


def _harness(connect_result: str | None) -> tuple[MagicMock, AsyncMock]:
    manager = MagicMock()
    manager.connect_authenticated = AsyncMock(return_value=connect_result)
    manager.subscribe_to_channel = AsyncMock()
    manager.send_message_to_connection = AsyncMock()
    manager.disconnect = AsyncMock()
    manager.active_connections = {}
    return manager, AsyncMock()


async def _run_endpoint(manager: MagicMock, ws: AsyncMock, client_info: str) -> None:
    with (
        patch.object(v2, "connection_manager", manager),
        patch.object(v2, "status_update_service", AsyncMock()),
        patch.object(
            v2.WebSocketAuthenticator,
            "authenticate",
            AsyncMock(return_value={"sub": "u1"}),
        ),
        patch.object(
            v2.WebSocketAuthenticator, "get_subprotocol_response", return_value=None
        ),
        patch.object(v2, "_resolve_ws_organization_id", AsyncMock(return_value="o1")),
        patch.object(v2, "_resolve_ws_role", AsyncMock(return_value="user")),
    ):
        await v2.websocket_connect_v2_secure(
            ws, channels="", frequency="normal", client_info=client_info
        )


@pytest.mark.asyncio
async def test_server_role_stamp_wins_over_client_supplied_role() -> None:
    manager, ws = _harness(connect_result=None)

    await _run_endpoint(manager, ws, json.dumps({"role": "admin", "browser": "ff"}))

    stored = manager.connect_authenticated.await_args.kwargs["client_info"]
    assert stored["role"] == "user"
    assert stored["browser"] == "ff"


@pytest.mark.asyncio
async def test_receive_loop_exits_once_manager_dropped_connection() -> None:
    manager, ws = _harness(connect_result="cid")
    manager.active_connections = {"cid": object()}

    async def drop(connection_id: str, raw: str) -> None:
        manager.active_connections.pop(connection_id, None)

    manager.handle_client_message = AsyncMock(side_effect=drop)
    # A second receive means the loop kept reading a socket the manager had
    # already closed (in prod: RuntimeError logged per iteration, forever).
    ws.receive_text = AsyncMock(side_effect=["x", WebSocketDisconnect()])

    await _run_endpoint(manager, ws, "")

    assert ws.receive_text.await_count == 1
    manager.disconnect.assert_awaited_once()
