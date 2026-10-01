"""R4-M9 regression: the admin gate on WS connection-inspection endpoints.

``User`` has no ``is_superuser`` attribute — the old checks raised
AttributeError, which the routes' blanket ``except Exception`` turned into a
500 for BOTH admins (should be allowed) and plain users (should be 403).
The canonical admin check is ``has_permission(UserRole.ADMIN)`` (what
multi_tenancy uses). Mocked connection manager, no real sockets.

I6 tightened the contract further: admin access is also tenant-scoped
(same-org only), so an admin inspecting a foreign user needs
``_get_user_organization_id`` to confirm the target shares their org —
see test_ws_tenant_scoping.py for the full matrix.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.models.user import User, UserRole

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ORG = "11111111-1111-1111-1111-111111111111"


def _user(role: UserRole) -> User:
    return User(id=uuid4(), role=role, organization_id=ORG)


async def test_admin_can_inspect_another_users_connections() -> None:
    from src.api.realtime.websocket_v2 import get_user_connections

    admin = _user(UserRole.ADMIN)
    with (
        patch("src.api.realtime.websocket_v2.connection_manager") as manager,
        patch(
            "src.api.realtime.websocket_v2._get_user_organization_id",
            new=AsyncMock(return_value=ORG),
        ),
    ):
        manager.get_user_connections.return_value = []
        result = await get_user_connections(user_id=str(uuid4()), current_user=admin)

    assert result["active_connections"] == 0


async def test_plain_user_gets_403_not_500() -> None:
    from src.api.realtime.websocket_v2 import get_user_connections

    user = _user(UserRole.USER)
    with pytest.raises(HTTPException) as exc_info:
        await get_user_connections(user_id=str(uuid4()), current_user=user)

    assert exc_info.value.status_code == 403
