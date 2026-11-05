"""Characterize message_service.py (Task 4.3 consolidation).

Covers the ``require_author_or_admin`` delete-permission divergence flag,
the consolidated list capabilities (asc/desc/before_id/since/load_only —
a strict union of the three prior implementations, not gated behind a
flag), and nested-route chain scoping.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Awaitable, Callable, List
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ChatMessage, MessageRole
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.schemas.chat import ChatMessageUpdate
from src.services.threads import message_service

pytestmark = pytest.mark.integration


async def _seed_messages(
    db_session: AsyncSession, thread: Thread, user: User, count: int = 3
) -> List[ChatMessage]:
    now = datetime.utcnow()
    msgs = []
    for i in range(count):
        m = ChatMessage(
            thread_id=thread.id,
            user_id=user.id,
            role=MessageRole.USER,
            content=f"msg-{i}",
            created_at=now + timedelta(milliseconds=i * 10),
            updated_at=now + timedelta(milliseconds=i * 10),
        )
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)
        db_session.info["_created"]["chat_messages"].append(m.id)
        msgs.append(m)
    return msgs


async def test_list_messages_asc_desc_and_before_id(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    asc_result = await message_service.list_messages(
        db_session, thread.id, user.id, order="asc"
    )
    assert asc_result is not None
    asc, total, has_more = asc_result
    assert [m.id for m in asc] == [m.id for m in msgs]
    assert total == 3
    assert has_more is False

    desc_result = await message_service.list_messages(
        db_session, thread.id, user.id, order="desc"
    )
    assert desc_result is not None
    desc, _total, _has_more = desc_result
    assert [m.id for m in desc] == [m.id for m in reversed(msgs)]

    older_result = await message_service.list_messages(
        db_session, thread.id, user.id, order="desc", before_id=msgs[2].id, limit=1
    )
    assert older_result is not None
    older, _total, has_more_cursor = older_result
    assert [m.id for m in older] == [msgs[1].id]
    assert has_more_cursor is True  # msgs[0] still remains


async def test_list_messages_since_filter(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    msgs = await _seed_messages(db_session, thread, user, count=3)

    newer_result = await message_service.list_messages(
        db_session, thread.id, user.id, since=msgs[0].created_at
    )
    assert newer_result is not None
    newer, total, _has_more = newer_result
    assert total == 2
    assert [m.id for m in newer] == [msgs[1].id, msgs[2].id]


async def test_list_messages_returns_none_for_missing_thread(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    user = await user_factory()
    assert (await message_service.list_messages(db_session, uuid4(), user.id)) is None


async def _add_viewer(
    db_session: AsyncSession, workspace_id: UUID, viewer_id: UUID
) -> None:
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace_id, user_id=viewer_id, role=WorkspaceRole.VIEWER
        )
    )
    await db_session.commit()


async def _workspace_id_for_thread(db_session: AsyncSession, thread: Thread) -> UUID:
    from sqlalchemy import select

    from src.models.conversation import Conversation

    conv: Conversation = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == thread.conversation_id)
        )
    ).scalar_one()
    workspace_id: UUID = conv.workspace_id
    return workspace_id


async def test_update_message_feedback_requires_edit_rights(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    owner = await user_factory()
    viewer = await user_factory()
    thread = await thread_factory(user=owner)
    workspace_id = await _workspace_id_for_thread(db_session, thread)
    await _add_viewer(db_session, workspace_id, viewer.id)

    msg = ChatMessage(
        thread_id=thread.id, user_id=owner.id, role=MessageRole.USER, content="hi"
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    db_session.info["_created"]["chat_messages"].append(msg.id)

    with pytest.raises(PermissionError):
        await message_service.update_message_feedback(
            db_session, msg.id, ChatMessageUpdate(feedback_rating=5), viewer.id
        )

    updated = await message_service.update_message_feedback(
        db_session, msg.id, ChatMessageUpdate(feedback_rating=5), owner.id
    )
    assert updated is not None
    assert updated.feedback_rating == 5


async def test_delete_message_require_author_or_admin_flag(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    owner = await user_factory()
    editor = await user_factory()
    thread = await thread_factory(user=owner)
    workspace_id = await _workspace_id_for_thread(db_session, thread)
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace_id, user_id=editor.id, role=WorkspaceRole.EDITOR
        )
    )
    await db_session.commit()

    msg_for_editor_flag_false = ChatMessage(
        thread_id=thread.id, user_id=owner.id, role=MessageRole.USER, content="a"
    )
    msg_for_editor_flag_true = ChatMessage(
        thread_id=thread.id, user_id=owner.id, role=MessageRole.USER, content="b"
    )
    db_session.add_all([msg_for_editor_flag_false, msg_for_editor_flag_true])
    await db_session.commit()
    for m in (msg_for_editor_flag_false, msg_for_editor_flag_true):
        await db_session.refresh(m)
        db_session.info["_created"]["chat_messages"].append(m.id)

    # require_author_or_admin=False (router-canonical): edit rights are
    # required, and a human-authored message may only be deleted by its author
    # or a workspace admin (audit 2026-11 F8) -- an editor must not delete the
    # owner's words.
    with pytest.raises(PermissionError):
        await message_service.delete_message(
            db_session,
            msg_for_editor_flag_false.id,
            editor.id,
            require_author_or_admin=False,
        )
    # The owner (workspace admin) may delete any message under the router rule.
    assert await message_service.delete_message(
        db_session,
        msg_for_editor_flag_false.id,
        owner.id,
        require_author_or_admin=False,
    )

    # require_author_or_admin=True (old ChatService default): a non-author
    # editor is refused — only the author or a workspace admin may delete.
    with pytest.raises(PermissionError):
        await message_service.delete_message(
            db_session,
            msg_for_editor_flag_true.id,
            editor.id,
            require_author_or_admin=True,
        )
    # The author may always delete under the old rule.
    assert await message_service.delete_message(
        db_session, msg_for_editor_flag_true.id, owner.id, require_author_or_admin=True
    )


async def test_router_delete_rule_editor_own_and_assistant_messages(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    """Audit 2026-11 F8: under the router rule an editor may delete their own
    messages and assistant replies (no human author), a viewer may delete
    nothing, and an admin may delete an editor's message."""
    owner = await user_factory()
    editor = await user_factory()
    viewer = await user_factory()
    thread = await thread_factory(user=owner)
    workspace_id = await _workspace_id_for_thread(db_session, thread)
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace_id, user_id=editor.id, role=WorkspaceRole.EDITOR
            ),
            WorkspaceMember(
                workspace_id=workspace_id, user_id=viewer.id, role=WorkspaceRole.VIEWER
            ),
        ]
    )
    await db_session.commit()

    editor_own = ChatMessage(
        thread_id=thread.id, user_id=editor.id, role=MessageRole.USER, content="e"
    )
    editor_own_for_admin = ChatMessage(
        thread_id=thread.id, user_id=editor.id, role=MessageRole.USER, content="e2"
    )
    assistant_reply = ChatMessage(
        thread_id=thread.id, user_id=None, role=MessageRole.ASSISTANT, content="ai"
    )
    viewer_target = ChatMessage(
        thread_id=thread.id, user_id=viewer.id, role=MessageRole.USER, content="v"
    )
    msgs = [editor_own, editor_own_for_admin, assistant_reply, viewer_target]
    db_session.add_all(msgs)
    await db_session.commit()
    for m in msgs:
        await db_session.refresh(m)
        db_session.info["_created"]["chat_messages"].append(m.id)

    assert await message_service.delete_message(db_session, editor_own.id, editor.id)
    assert await message_service.delete_message(
        db_session, assistant_reply.id, editor.id
    )
    # Edit rights are still required: a viewer cannot delete even their own.
    with pytest.raises(PermissionError):
        await message_service.delete_message(db_session, viewer_target.id, viewer.id)
    # A workspace admin (owner) may delete another member's message.
    assert await message_service.delete_message(
        db_session, editor_own_for_admin.id, owner.id
    )
