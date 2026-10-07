"""Harness runs stay bound to one Collection: a workspace grant never connects."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.schemas.integration_context import IntegrationContext

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, WORKSPACE, GRANT, DEVICE = (uuid4() for _ in range(6))


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _context(*, workspace: bool) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=None if workspace else PROJECT,
        workspace_id=WORKSPACE if workspace else None,
        grant_id=GRANT,
    )


def _socket() -> Any:
    return SimpleNamespace(
        url=SimpleNamespace(scheme="wss"),
        headers={"x-nous-integration-grant": "opaque"},
        accept=AsyncMock(),
        close=AsyncMock(),
        receive_text=AsyncMock(
            return_value=json.dumps({"poll": True, "deviceId": str(DEVICE)})
        ),
        send_json=AsyncMock(),
    )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.api import harness
    from src.core.websocket_auth import WebSocketAuthenticator

    monkeypatch.setattr(harness, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(
        WebSocketAuthenticator,
        "authenticate",
        AsyncMock(return_value={"sub": str(USER)}),
    )
    # Nothing past the guard may run for a refused context.
    for name in ("lease_commands", "lease_runs", "ingest_bridge_event"):
        monkeypatch.setattr(harness, name, AsyncMock(side_effect=AssertionError(name)))
    return harness


async def test_connect_refuses_a_workspace_grant_before_accepting(
    transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        transport,
        "resolve_integration_context",
        AsyncMock(return_value=_context(workspace=True)),
    )
    socket = _socket()
    await transport.connect(socket)
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(
        code=4403, reason="Bridge authorization denied"
    )


async def test_connect_rechecks_the_binding_on_every_frame(
    transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = AsyncMock(
        side_effect=[_context(workspace=False), _context(workspace=True)]
    )
    monkeypatch.setattr(transport, "resolve_integration_context", resolve)
    socket = _socket()
    await transport.connect(socket)
    socket.accept.assert_awaited_once()
    assert resolve.await_count == 2
    socket.send_json.assert_not_awaited()
    socket.close.assert_awaited_once_with(
        code=4403, reason="Bridge authorization denied"
    )


# Mutation: src/api/harness.py:116 remove the loopback exception.
# Command: pytest -c backend/pytest.ini --no-cov -q
# backend/tests/unit/api/test_harness_workspace_guard.py -k plain_ws_exception
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "203.0.113.8", None])
async def test_plain_ws_exception_is_loopback_only(
    transport: Any, monkeypatch: pytest.MonkeyPatch, host: str | None
) -> None:
    resolve = AsyncMock(return_value=_context(workspace=True))
    monkeypatch.setattr(transport, "resolve_integration_context", resolve)
    socket = _socket()
    socket.url.scheme = "ws"
    socket.client = SimpleNamespace(host=host) if host else None
    await transport.connect(socket)
    # Loopback reaches grant validation, which still refuses workspace grants.
    assert resolve.await_count == (1 if host in {"127.0.0.1", "::1"} else 0)
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(
        code=4403, reason="Bridge authorization denied"
    )
