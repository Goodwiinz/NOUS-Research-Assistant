from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from src.services.threads import workspace_access

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _user():
    return SimpleNamespace(id=uuid4())


def _request(*, thread_id=None, workspace_id=None):
    return SimpleNamespace(
        thread_id=str(thread_id) if thread_id else None,
        page_context=SimpleNamespace(workspace_id=workspace_id),
        messages=[SimpleNamespace(role="user", content="hello")],
        model="",
    )


def _thread(*, editable: bool):
    workspace = Mock()
    workspace.can_user_edit.return_value = editable
    conversation = SimpleNamespace(id=uuid4(), workspace=workspace)
    return SimpleNamespace(
        id=uuid4(), conversation_id=conversation.id, conversation=conversation
    )


async def test_explicit_missing_thread_raises_without_creating(monkeypatch):
    from src.services.agent.agent_execution_service import (
        AgentThreadResolutionError,
        _resolve_thread,
    )

    db = AsyncMock()
    db.add = Mock()
    monkeypatch.setattr(workspace_access, "get_thread", AsyncMock(return_value=None))
    with pytest.raises(AgentThreadResolutionError, match="Thread not found"):
        await _resolve_thread(db, _user(), _request(thread_id=uuid4()))
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_explicit_view_only_thread_is_not_writable(monkeypatch):
    from src.services.agent.agent_execution_service import (
        AgentThreadResolutionError,
        _resolve_thread,
    )

    db = AsyncMock()
    db.add = Mock()
    monkeypatch.setattr(
        workspace_access, "get_thread", AsyncMock(return_value=_thread(editable=False))
    )
    with pytest.raises(AgentThreadResolutionError, match="Thread not found"):
        await _resolve_thread(db, _user(), _request(thread_id=uuid4()))
    db.add.assert_not_called()


async def test_explicit_editable_thread_resolves_through_access_funnel(monkeypatch):
    from src.services.agent.agent_execution_service import _resolve_thread

    db = AsyncMock()
    expected = _thread(editable=True)
    get_thread = AsyncMock(return_value=expected)
    monkeypatch.setattr(workspace_access, "get_thread", get_thread)
    user = _user()
    actual, conversation_id = await _resolve_thread(
        db, user, _request(thread_id=expected.id)
    )
    assert actual is expected
    assert conversation_id == str(expected.conversation_id)
    assert get_thread.await_args.kwargs == {"include_messages": False}


async def test_explicit_inaccessible_workspace_never_falls_back(monkeypatch):
    from src.services.agent.agent_execution_service import (
        AgentThreadResolutionError,
        _resolve_thread,
    )

    db = AsyncMock()
    db.add = Mock()
    get_workspace = AsyncMock(return_value=None)
    monkeypatch.setattr(workspace_access, "get_workspace", get_workspace)
    with pytest.raises(AgentThreadResolutionError, match="Workspace not found"):
        await _resolve_thread(db, _user(), _request(workspace_id=uuid4()))
    assert get_workspace.await_args.kwargs == {
        "load_conversations": False,
        "load_collections": False,
    }
    db.add.assert_not_called()


async def test_explicit_workspace_is_locked_before_oldest_conversation_lookup(
    monkeypatch,
):
    from src.services.agent.agent_execution_service import _resolve_thread

    user = _user()
    workspace = Mock(id=uuid4())
    workspace.can_user_edit.return_value = True
    monkeypatch.setattr(
        workspace_access, "get_workspace", AsyncMock(return_value=workspace)
    )
    conversation = SimpleNamespace(id=uuid4())
    locked_result = MagicMock(scalar_one_or_none=Mock(return_value=workspace))
    conversation_result = MagicMock(scalar_one_or_none=Mock(return_value=conversation))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[locked_result, conversation_result])
    db.add = Mock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    thread, conversation_id = await _resolve_thread(
        db, user, _request(workspace_id=workspace.id)
    )
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "FOR UPDATE" in statements[0]
    assert "conversations.created_at ASC" in statements[1]
    assert "conversations.id ASC" in statements[1]
    assert thread is not None
    assert conversation_id == str(conversation.id)
    assert db.add.call_count == 1


async def test_threadless_user_without_workspace_remains_ephemeral():
    from src.services.agent.agent_execution_service import _resolve_thread

    empty = MagicMock(scalar_one_or_none=Mock(return_value=None))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=empty)
    db.add = Mock()
    assert await _resolve_thread(db, _user(), _request()) == (None, "")
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_implicit_workspace_locks_oldest_owned_and_reuses_oldest_conversation():
    from src.services.agent.agent_execution_service import _resolve_thread

    workspace = SimpleNamespace(id=uuid4())
    conversation = SimpleNamespace(id=uuid4())
    workspace_result = MagicMock(scalar_one_or_none=Mock(return_value=workspace))
    conversation_result = MagicMock(scalar_one_or_none=Mock(return_value=conversation))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[workspace_result, conversation_result])
    db.add = Mock()

    thread, conversation_id = await _resolve_thread(db, _user(), _request())

    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "workspaces.owner_id" in statements[0]
    assert "workspaces.created_at ASC" in statements[0]
    assert "workspaces.id ASC" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "conversations.created_at ASC" in statements[1]
    assert "conversations.id ASC" in statements[1]
    assert conversation_id == str(conversation.id)
    assert thread is not None
    # Reusing the existing container adds only the new thread.
    assert db.add.call_count == 1


async def test_implicit_workspace_without_conversation_creates_one_container_and_thread():
    from src.models.conversation import Conversation
    from src.models.thread import Thread
    from src.services.agent.agent_execution_service import _resolve_thread

    workspace = SimpleNamespace(id=uuid4())
    workspace_result = MagicMock(scalar_one_or_none=Mock(return_value=workspace))
    no_conversation = MagicMock(scalar_one_or_none=Mock(return_value=None))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[workspace_result, no_conversation])
    db.add = Mock()

    thread, conversation_id = await _resolve_thread(db, _user(), _request())

    added = [call.args[0] for call in db.add.call_args_list]
    assert len([item for item in added if isinstance(item, Conversation)]) == 1
    assert len([item for item in added if isinstance(item, Thread)]) == 1
    assert thread is added[-1]
    assert conversation_id == str(added[0].id)
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()
