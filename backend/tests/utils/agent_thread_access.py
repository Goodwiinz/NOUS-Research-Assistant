"""Focused durable-thread access fixtures for agent transport tests."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4


def editable_thread(
    thread_id: str | UUID,
    *,
    conversation_id: str | UUID | None = None,
) -> Any:
    """Return the relationship shape consumed by the real agent resolver."""
    resolved_conversation_id = UUID(str(conversation_id or uuid4()))
    workspace = SimpleNamespace(can_user_edit=lambda _user_id: True)
    conversation = SimpleNamespace(
        id=resolved_conversation_id,
        workspace=workspace,
    )
    return SimpleNamespace(
        id=UUID(str(thread_id)),
        conversation_id=resolved_conversation_id,
        conversation=conversation,
    )


def editable_thread_getter() -> AsyncMock:
    """Mock the access funnel while preserving each requested durable ID."""

    async def get_thread(
        _db: object,
        thread_id: UUID,
        _user_id: object,
        *,
        include_messages: bool = False,
    ) -> Any:
        del include_messages
        return editable_thread(thread_id)

    return AsyncMock(side_effect=get_thread)
