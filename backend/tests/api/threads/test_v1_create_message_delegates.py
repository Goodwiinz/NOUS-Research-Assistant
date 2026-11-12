"""The nested workspace create-message handler no longer exists (audit C4).

It used to delegate to ``ChatService.create_message``; the route was removed
after its deprecation window (``docs/decisions/api-deprecation-window.md``).
Message creation over REST is ``POST /api/v2/messages`` only, covered by
``test_v2_create_message_delegates.py``.
"""

from __future__ import annotations

import pytest

from src.api.threads import threads as threads_mod
from src.api.threads.workspace_routes import messages as workspaces_mod

pytestmark = pytest.mark.unit


def test_nested_handlers_are_removed() -> None:
    assert not hasattr(workspaces_mod, "create_message")
    assert not hasattr(threads_mod, "create_message")


def test_canonical_handler_remains() -> None:
    assert callable(workspaces_mod.create_message_standalone)
