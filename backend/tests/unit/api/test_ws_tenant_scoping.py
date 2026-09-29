"""I6 regression: tenant scoping on WS v2 admin/inspection endpoints.

``GET /connections/{user_id}`` and ``POST /test-connection`` authorized on
role alone — an org-A admin could enumerate/message org-B users' connections.
``GET /status`` returned global infra stats (total connections, per-channel
subscribers, org/user counts, job throughput) to any authenticated user,
disclosing other tenants' activity volume.

Contract pinned here:
- self access unchanged (and does not hit the target-user lookup);
- admin access requires the target user to be in the caller's org
  (foreign-org target -> 403, nonexistent target user -> 404, same-org -> 200);
- /status requires admin (regular user -> 403, admin -> 200, shape unchanged).

Direct-call + monkeypatch pattern (see test_ws_broadcast_authz.py).
asyncio_mode=auto, so plain ``async def`` tests run natively.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

from src.api.realtime import websocket_v2 as ws
from src.models.user import User, UserRole

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
TARGET = "target-user"


def _user(role: UserRole, org_id: str | None = None) -> User:
    return User(id=uuid4(), role=role, organization_id=org_id)


def _conn(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        connected_at=datetime.now(timezone.utc),
        last_heartbeat=datetime.now(timezone.utc),
        subscribed_channels=set(),
        client_info={},
    )


def _patch_org_lookup(monkeypatch, org_id: str | None) -> AsyncMock:
    lookup = AsyncMock(return_value=org_id)
    monkeypatch.setattr(ws, "_get_user_organization_id", lookup, raising=False)
    return lookup


# --- GET /connections/{user_id} ----------------------------------------------


async def test_connections_foreign_org_admin_denied(monkeypatch):
    """Org-A admin cannot enumerate an org-B user's connections -> 403."""
    manager = SimpleNamespace(
        get_user_connections=Mock(return_value=[]),
        active_connections={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    _patch_org_lookup(monkeypatch, ORG_B)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    with pytest.raises(HTTPException) as ei:
        await ws.get_user_connections(user_id=TARGET, current_user=admin)

    assert ei.value.status_code == 403
    manager.get_user_connections.assert_not_called()


async def test_connections_same_org_admin_allowed(monkeypatch):
    """Admin inspecting a user in their OWN org -> 200."""
    manager = SimpleNamespace(
        get_user_connections=Mock(return_value=[]),
        active_connections={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    _patch_org_lookup(monkeypatch, ORG_A)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    result = await ws.get_user_connections(user_id=TARGET, current_user=admin)

    assert result["active_connections"] == 0
    manager.get_user_connections.assert_called_once_with(TARGET)


async def test_connections_self_allowed_without_lookup(monkeypatch):
    """Self access keeps working and never needs the target-user lookup."""
    manager = SimpleNamespace(
        get_user_connections=Mock(return_value=[]),
        active_connections={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    lookup = _patch_org_lookup(monkeypatch, ORG_A)

    user = _user(UserRole.USER, org_id=ORG_A)
    result = await ws.get_user_connections(user_id=str(user.id), current_user=user)

    assert result["active_connections"] == 0
    lookup.assert_not_awaited()


async def test_connections_nonexistent_target_user_404(monkeypatch):
    """Admin + unknown target user -> 404 (sibling-endpoint convention)."""
    manager = SimpleNamespace(
        get_user_connections=Mock(return_value=[]),
        active_connections={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    _patch_org_lookup(monkeypatch, None)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    with pytest.raises(HTTPException) as ei:
        await ws.get_user_connections(user_id="ghost-user", current_user=admin)

    assert ei.value.status_code == 404


async def test_connections_null_org_admin_denied(monkeypatch):
    """A null-org admin has no tenant to match against -> fail closed 403."""
    manager = SimpleNamespace(
        get_user_connections=Mock(return_value=[]),
        active_connections={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    _patch_org_lookup(monkeypatch, ORG_B)

    admin = _user(UserRole.ADMIN, org_id=None)
    with pytest.raises(HTTPException) as ei:
        await ws.get_user_connections(user_id=TARGET, current_user=admin)

    assert ei.value.status_code == 403


# --- POST /test-connection ----------------------------------------------------


def _patch_test_manager(monkeypatch, target_user_id: str):
    manager = SimpleNamespace(
        active_connections={"conn-1": _conn(target_user_id)},
        send_message_to_connection=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    return manager


async def test_test_connection_foreign_org_admin_denied(monkeypatch):
    """Org-A admin cannot message an org-B user's connection -> 403."""
    manager = _patch_test_manager(monkeypatch, TARGET)
    _patch_org_lookup(monkeypatch, ORG_B)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    with pytest.raises(HTTPException) as ei:
        await ws.test_websocket_connection(
            connection_id="conn-1", current_user=admin
        )

    assert ei.value.status_code == 403
    manager.send_message_to_connection.assert_not_awaited()


async def test_test_connection_same_org_admin_allowed(monkeypatch):
    """Admin messaging a connection owned by a same-org user -> success."""
    _patch_test_manager(monkeypatch, TARGET)
    _patch_org_lookup(monkeypatch, ORG_A)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    result = await ws.test_websocket_connection(
        connection_id="conn-1", current_user=admin
    )

    assert result["success"] is True


async def test_test_connection_self_allowed(monkeypatch):
    """Owner messaging their own connection keeps working, no lookup."""
    user = _user(UserRole.USER, org_id=ORG_A)
    manager = _patch_test_manager(monkeypatch, str(user.id))
    lookup = _patch_org_lookup(monkeypatch, ORG_A)

    result = await ws.test_websocket_connection(
        connection_id="conn-1", current_user=user
    )

    assert result["success"] is True
    lookup.assert_not_awaited()


async def test_test_connection_nonexistent_target_user_404(monkeypatch):
    """Connection alive in manager but target user gone from DB -> 404."""
    _patch_test_manager(monkeypatch, TARGET)
    _patch_org_lookup(monkeypatch, None)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    with pytest.raises(HTTPException) as ei:
        await ws.test_websocket_connection(
            connection_id="conn-1", current_user=admin
        )

    assert ei.value.status_code == 404


# --- GET /status ---------------------------------------------------------------


def _patch_status_manager(monkeypatch):
    manager = SimpleNamespace(
        get_connection_stats=Mock(
            return_value={
                "total_connections": 1,
                "max_connections": 100,
                "redis_enabled": False,
            }
        ),
        redis_client=None,
        channel_subscribers={},
    )
    monkeypatch.setattr(ws, "connection_manager", manager)
    return manager


def _patch_status_service(monkeypatch):
    system_status = SimpleNamespace(
        active_jobs=0,
        queued_jobs=0,
        completed_jobs_today=0,
        failed_jobs_today=0,
        average_processing_time_seconds=0.0,
        active_connections=1,
    )
    service = SimpleNamespace(get_system_status=AsyncMock(return_value=system_status))
    monkeypatch.setattr(ws, "status_update_service", service)
    return service


async def test_status_regular_user_denied(monkeypatch):
    """Global infra stats are not for regular users -> 403."""
    manager = _patch_status_manager(monkeypatch)
    _patch_status_service(monkeypatch)

    user = _user(UserRole.USER, org_id=ORG_A)
    with pytest.raises(HTTPException) as ei:
        await ws.get_websocket_status(current_user=user)

    assert ei.value.status_code == 403
    manager.get_connection_stats.assert_not_called()


async def test_status_admin_allowed_shape_unchanged(monkeypatch):
    """Admin still gets the full status payload (shape unchanged)."""
    _patch_status_manager(monkeypatch)
    _patch_status_service(monkeypatch)

    admin = _user(UserRole.ADMIN, org_id=ORG_A)
    result = await ws.get_websocket_status(current_user=admin)

    assert result["websocket_service"]["status"] == "healthy"
    assert result["connections"]["total_connections"] == 1
    assert "system_status" in result
    assert "channels" in result
    assert "performance" in result
