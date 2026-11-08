"""Characterize ChatService's CURRENT (pre-4.3) behavior for its existing
callers (backend/src/api/threads/threads.py — the live ``/api/v2/threads``
router — and ``stream_service.py``), locking in the eight measured
divergences from the router-inline ``workspace_routes`` endpoints *before*
Task 4.3 moved the underlying logic into shared resource services.

Per the Task 4.3 amendment (2026-07-16, A2): router semantics are canonical
for the endpoints the router serves, but ChatService's own callers must keep
their observed behavior. These tests pin exactly that — the ChatService
surface must behave identically after the consolidation as it did before it.
"""

from __future__ import annotations

from typing import Awaitable, Callable
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.chat import (
    ConversationCreate,
    ThreadCreate,
    WorkspaceCreate,
    WorkspaceUpdate,
)
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.integration


async def _make_workspace(db_session: AsyncSession, owner: User) -> Workspace:
    ws = Workspace(name="ws", owner_id=owner.id, organization_id=owner.organization_id)
    db_session.add(ws)
    await db_session.commit()
    await db_session.refresh(ws)
    db_session.info["_created"]["workspaces"].append(ws.id)
    return ws


async def test_create_workspace_enforces_org_match(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
    organization_factory: Callable[..., Awaitable[Organization]],
) -> None:
    """fix(workspaces): close pre-existing tenant gaps on the ChatService
    surface — ChatService.create_workspace previously trusted
    ``data.organization_id`` verbatim with no cross-org guard, unlike the
    router-inline create endpoint (which has always enforced it); the live
    ``conversations.py`` create endpoint this class backs inherited that gap.
    It now enforces the same guard: a cross-org create raises
    ``PermissionError`` (the route maps that to 403), replacing the old
    divergence-1 test that pinned the gap itself."""
    owner = await user_factory()
    foreign_org = await organization_factory()
    service = ChatService(db_session)

    with pytest.raises(PermissionError):
        await service.create_workspace(
            WorkspaceCreate(name="x", is_public=False, organization_id=foreign_org.id),
            owner.id,
        )


async def test_list_workspaces_filters_deleted_memberships(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """fix(workspaces): close pre-existing tenant gaps on the ChatService
    surface — ChatService.list_workspaces previously did not exclude a
    soft-deleted ``WorkspaceMember`` row, so a removed member still saw the
    workspace on the live ``conversations.py`` list endpoint this class
    backs, unlike the router-inline list endpoint (which has always filtered
    it out). It now filters deleted memberships too, replacing the old
    divergence-2 test that pinned the gap itself."""
    owner = await user_factory()
    service = ChatService(db_session)
    workspace = await service.create_workspace(
        WorkspaceCreate(name="x", is_public=False), owner.id
    )

    membership = next(m for m in workspace.members if str(m.user_id) == str(owner.id))
    membership.is_deleted = True
    await db_session.commit()

    workspaces, _total = await service.list_workspaces(owner.id)
    assert workspace.id not in [w.id for w in workspaces]


async def test_delete_workspace_never_stamps_deleted_at(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """Divergence 3: ChatService soft-deletes never set deleted_at (only
    is_deleted); the router-inline delete endpoints always have."""
    owner = await user_factory()
    service = ChatService(db_session)
    workspace = await service.create_workspace(
        WorkspaceCreate(name="x", is_public=False), owner.id
    )

    assert await service.delete_workspace(workspace.id, owner.id) is True
    assert workspace.is_deleted is True
    assert workspace.deleted_at is None


async def test_delete_conversation_never_stamps_deleted_at_and_requires_admin(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """Divergence 3 + a second permission divergence: ChatService requires
    ADMIN rights to delete a conversation; the router-inline delete
    endpoints require only edit rights."""
    owner = await user_factory()
    editor = await user_factory()
    ws = await _make_workspace(db_session, owner)
    db_session.add(
        WorkspaceMember(
            workspace_id=ws.id, user_id=editor.id, role=WorkspaceRole.EDITOR
        )
    )
    await db_session.commit()

    service = ChatService(db_session)
    conversation = await service.create_conversation(
        ConversationCreate(workspace_id=ws.id, title="c"), owner.id
    )
    assert conversation is not None

    # An editor (not admin) is refused by ChatService's own delete rule.
    assert await service.delete_conversation(conversation.id, editor.id) is False

    assert await service.delete_conversation(conversation.id, owner.id) is True
    assert conversation.is_deleted is True
    assert conversation.deleted_at is None


async def test_delete_thread_never_stamps_deleted_at(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    """Divergence 3, for threads."""
    user = await user_factory()
    thread = await thread_factory(user=user)
    service = ChatService(db_session)

    assert await service.delete_thread(thread.id, user.id) is True
    assert thread.is_deleted is True
    assert thread.deleted_at is None


async def test_list_conversations_orders_pinned_first(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """Divergence 4: ChatService orders pinned conversations to the top; the
    router-inline list endpoint orders by activity only."""
    owner = await user_factory()
    ws = await _make_workspace(db_session, owner)
    service = ChatService(db_session)

    older_pinned = await service.create_conversation(
        ConversationCreate(workspace_id=ws.id, title="pinned"), owner.id
    )
    assert older_pinned is not None
    await service.create_conversation(
        ConversationCreate(workspace_id=ws.id, title="fresh"), owner.id
    )
    older_pinned.is_pinned = True
    await db_session.commit()

    conversations, _total = await service.list_conversations(ws.id, owner.id)
    assert conversations[0].id == older_pinned.id


async def test_create_thread_only_flushes_caller_owns_the_commit(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """Divergence 5: ChatService.create_thread only flushes — a caller that
    never commits (or rolls back) can still discard it. The router-inline
    create endpoints commit immediately."""
    owner = await user_factory()
    ws = await _make_workspace(db_session, owner)
    service = ChatService(db_session)
    conversation = await service.create_conversation(
        ConversationCreate(workspace_id=ws.id, title="c"), owner.id
    )
    assert conversation is not None

    thread = await service.create_thread(
        ThreadCreate(conversation_id=conversation.id, title="t"), owner.id
    )
    assert thread is not None
    thread_id = thread.id

    await db_session.rollback()

    from sqlalchemy import select

    from src.models.thread import Thread

    row = (
        await db_session.execute(select(Thread).where(Thread.id == thread_id))
    ).scalar_one_or_none()
    assert row is None  # rolled back — never committed by create_thread itself


async def test_delete_message_requires_edit_rights_even_for_author(
    db_session: AsyncSession,
    thread_factory: Callable[..., Awaitable[Thread]],
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    """Divergence 6 (closed 2026-11-08): ChatService.delete_message used to
    let a message's AUTHOR delete it even as a read-only viewer. Both delete
    paths now require edit rights; an editor may still delete their own."""
    from src.models import ChatMessage, MessageRole
    from src.models.conversation import Conversation
    from src.models.workspace import Workspace as WorkspaceModel

    owner = await user_factory()
    author_viewer = await user_factory()
    thread = await thread_factory(user=owner)

    from sqlalchemy import select

    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == thread.conversation_id)
        )
    ).scalar_one()
    db_session.add(
        WorkspaceMember(
            workspace_id=conv.workspace_id,
            user_id=author_viewer.id,
            role=WorkspaceRole.VIEWER,
        )
    )
    await db_session.commit()

    msg = ChatMessage(
        thread_id=thread.id,
        user_id=author_viewer.id,
        role=MessageRole.USER,
        content="hi",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    db_session.info["_created"]["chat_messages"].append(msg.id)

    service = ChatService(db_session)
    # A viewer is read-only: authorship no longer lets them delete.
    assert await service.delete_message(msg.id, author_viewer.id) is False

    # Promoted to editor, the author may delete their own message.
    member = (
        await db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == conv.workspace_id,
                WorkspaceMember.user_id == author_viewer.id,
            )
        )
    ).scalar_one()
    member.role = WorkspaceRole.EDITOR
    await db_session.commit()
    assert await service.delete_message(msg.id, author_viewer.id) is True


async def test_update_workspace_and_delete_workspace_collapse_permission_and_not_found(
    db_session: AsyncSession, user_factory: Callable[..., Awaitable[User]]
) -> None:
    """ChatService's undifferentiated None/False result for not-found vs.
    insufficient-permission (the router distinguishes 404 vs 403; ChatService
    always returns None/False for both)."""
    owner = await user_factory()
    outsider = await user_factory()
    service = ChatService(db_session)
    workspace = await service.create_workspace(
        WorkspaceCreate(name="x", is_public=False), owner.id
    )

    assert (
        await service.update_workspace(uuid4(), WorkspaceUpdate(name="y"), owner.id)
        is None
    )
    assert (
        await service.update_workspace(
            workspace.id, WorkspaceUpdate(name="y"), outsider.id
        )
        is None
    )
    assert await service.delete_workspace(uuid4(), owner.id) is False
    assert await service.delete_workspace(workspace.id, outsider.id) is False
