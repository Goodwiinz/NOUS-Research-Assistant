"""R8-D3: agent read routes resolve threads through the workspace access funnel.

``GET /agent/threads``, ``/agent/threads/{id}/messages`` and
``/agent/graph/trace/{id}`` used a hand-rolled ``Workspace.owner_id == me``
join that only checked ``Thread.is_deleted``. Soft-deleting a workspace or
conversation (no cascade) left its agent threads listed and readable, while an
editor who created an agent thread in a shared workspace got 404 on reload.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.api.agent.execute import (
    get_graph_trace,
    get_thread_messages,
    list_agent_threads,
)
from src.models.conversation import Conversation
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole

pytestmark = pytest.mark.integration

_AGENT_MARKER = {"source": "agent"}


async def _agent_thread(db: Any, thread_factory: Any, owner: Any) -> Any:
    thread = await thread_factory(user=owner)
    thread.rag_document_scope = dict(_AGENT_MARKER)
    await db.commit()
    return thread


async def _parents(db: Any, thread: Any) -> tuple[Any, Any]:
    conversation = await db.scalar(
        select(Conversation).where(Conversation.id == thread.conversation_id)
    )
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == conversation.workspace_id)
    )
    return conversation, workspace


async def _listed_ids(db: Any, user: Any) -> set[str]:
    response = await list_agent_threads(current_user=user, db=db)
    return {t.id for t in response.threads}


async def _assert_unreadable(db: Any, user: Any, thread: Any) -> None:
    with pytest.raises(HTTPException) as messages_exc:
        await get_thread_messages(
            thread_id=thread.id, limit=None, before=None, current_user=user, db=db
        )
    assert messages_exc.value.status_code == 404
    with patch(
        "src.services.agent.visualization.get_execution_trace_mermaid",
        new=AsyncMock(return_value="sequenceDiagram"),
    ):
        with pytest.raises(HTTPException) as trace_exc:
            await get_graph_trace(thread_id=str(thread.id), current_user=user, db=db)
    assert trace_exc.value.status_code == 404


async def _assert_readable(db: Any, user: Any, thread: Any) -> None:
    messages = await get_thread_messages(
        thread_id=thread.id, limit=None, before=None, current_user=user, db=db
    )
    assert messages.total == 0
    with patch(
        "src.services.agent.visualization.get_execution_trace_mermaid",
        new=AsyncMock(return_value="sequenceDiagram"),
    ):
        trace = await get_graph_trace(
            thread_id=str(thread.id), current_user=user, db=db
        )
    assert trace["thread_id"] == str(thread.id)


async def test_soft_deleted_workspace_revokes_list_messages_and_trace(
    db_session: Any, thread_factory: Any, user_factory: Any
) -> None:
    owner = await user_factory()
    thread = await _agent_thread(db_session, thread_factory, owner)
    assert str(thread.id) in await _listed_ids(db_session, owner)  # live control
    await _assert_readable(db_session, owner, thread)

    _, workspace = await _parents(db_session, thread)
    workspace.is_deleted = True
    await db_session.commit()

    assert str(thread.id) not in await _listed_ids(db_session, owner)
    await _assert_unreadable(db_session, owner, thread)


async def test_soft_deleted_conversation_revokes_list_messages_and_trace(
    db_session: Any, thread_factory: Any, user_factory: Any
) -> None:
    owner = await user_factory()
    thread = await _agent_thread(db_session, thread_factory, owner)
    conversation, _ = await _parents(db_session, thread)
    conversation.is_deleted = True
    await db_session.commit()

    assert str(thread.id) not in await _listed_ids(db_session, owner)
    await _assert_unreadable(db_session, owner, thread)


async def test_editor_lists_and_reads_own_agent_thread_in_shared_workspace(
    db_session: Any, thread_factory: Any, user_factory: Any
) -> None:
    owner = await user_factory()
    editor = await user_factory()
    outsider = await user_factory()
    thread = await _agent_thread(db_session, thread_factory, owner)
    _, workspace = await _parents(db_session, thread)
    member: Any = WorkspaceMember(
        id=uuid4(),
        workspace_id=workspace.id,
        user_id=editor.id,
        role=WorkspaceRole.EDITOR,
    )
    db_session.add(member)
    thread.created_by_id = editor.id  # created via page_context.workspace_id
    await db_session.commit()

    assert str(thread.id) in await _listed_ids(db_session, editor)
    await _assert_readable(db_session, editor, thread)

    # Fail-closed controls: a stranger never sees it; a removed member loses it.
    assert str(thread.id) not in await _listed_ids(db_session, outsider)
    await _assert_unreadable(db_session, outsider, thread)
    member.is_deleted = True
    await db_session.commit()
    assert str(thread.id) not in await _listed_ids(db_session, editor)
    await _assert_unreadable(db_session, editor, thread)


async def test_list_keeps_only_agent_threads(
    db_session: Any, thread_factory: Any, user_factory: Any
) -> None:
    owner = await user_factory()
    agent = await _agent_thread(db_session, thread_factory, owner)
    plain = await thread_factory(user=owner)

    listed = await list_agent_threads(current_user=owner, db=db_session)

    ids = {t.id for t in listed.threads}
    assert str(agent.id) in ids
    assert str(plain.id) not in ids
    assert listed.total == len(listed.threads)
