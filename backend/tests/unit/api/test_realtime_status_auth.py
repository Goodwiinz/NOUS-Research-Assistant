"""R4-M5 regression: websocket_v2 status/health endpoints must require auth.

``get_websocket_status`` / ``get_available_channels`` / ``websocket_health_check``
(websocket_v2.py) exposed connection counts, channel metadata, and health
internals to anyone, unauthenticated - the sibling endpoints in the same
router (``/connections/{user_id}``, ``/broadcast``, ``/test-connection``) all
gate on ``Depends(get_current_user)``, these four didn't. Verified statically
via the FastAPI dependency graph so no live server/DB is needed.

The legacy v1 ``/ws/status`` case (websocket.py) was removed along with the
v1 WebSocket route in the WS-removal-to-polling migration.
"""

import inspect
from typing import Any, Callable

import pytest

from src.core.dependencies import get_current_user

pytestmark = pytest.mark.unit


def _has_current_user_dependency(fn: Callable[..., Any]) -> bool:
    for param in inspect.signature(fn).parameters.values():
        default = param.default
        if default is inspect._empty:
            continue
        dependency = getattr(default, "dependency", None)
        if dependency is get_current_user:
            return True
    return False


def test_websocket_v2_status_requires_auth() -> None:
    """I6 tightened /status to platform operators; that gate itself depends
    on ``get_current_user``, so authentication is still enforced."""
    from src.api.realtime.websocket_v2 import get_websocket_status
    from src.core.dependencies import require_platform_operator

    param = inspect.signature(get_websocket_status).parameters["current_user"]
    assert param.default.dependency is require_platform_operator
    assert _has_current_user_dependency(require_platform_operator)


def test_websocket_v2_channels_requires_auth() -> None:
    from src.api.realtime.websocket_v2 import get_available_channels

    assert _has_current_user_dependency(get_available_channels)


def test_websocket_v2_health_requires_auth() -> None:
    from src.api.realtime.websocket_v2 import websocket_health_check

    assert _has_current_user_dependency(websocket_health_check)
