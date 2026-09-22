from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.chat_message import ChatMessage, MessageRole
from src.models.conversation import Conversation
from src.models.thread import Thread, ThreadStatus
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.threads import thread_service, workspace_access

pytestmark = pytest.mark.integration


async def test_workspace_listing_is_global_stable_and_uses_lean_access(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await user_factory()
    workspace = Workspace(
        id=uuid4(),
        name="workspace",
        owner_id=owner.id,
        organization_id=owner.organization_id,
    )
    db_session.add(workspace)
    await db_session.flush()
    db_session.info["_created"]["workspaces"].append(workspace.id)

    conversations = [
        Conversation(
            id=uuid4(),
            workspace_id=workspace.id,
            title=title,
            created_by_id=owner.id,
        )
        for title in ("first", "second")
    ]
    db_session.add_all(conversations)
    await db_session.flush()
    db_session.info["_created"]["conversations"].extend(
        conversation.id for conversation in conversations
    )

    now = datetime.now(UTC)
    tied_ids = sorted((uuid4(), uuid4()), key=str)
    rows = [
        Thread(
            id=tied_ids[1],
            conversation_id=conversations[0].id,
            title="later-id",
            status=ThreadStatus.ACTIVE,
            last_message_at=now,
            created_by_id=owner.id,
        ),
        Thread(
            id=tied_ids[0],
            conversation_id=conversations[1].id,
            title="earlier-id",
            status=ThreadStatus.ACTIVE,
            last_message_at=now,
            created_by_id=owner.id,
        ),
        Thread(
            id=uuid4(),
            conversation_id=conversations[1].id,
            title="older-resolved",
            status=ThreadStatus.RESOLVED,
            last_message_at=now - timedelta(minutes=1),
            created_by_id=owner.id,
        ),
    ]
    db_session.add_all(rows)
    preview_message = ChatMessage(
        id=uuid4(),
        thread_id=tied_ids[0],
        user_id=owner.id,
        role=MessageRole.USER,
        content="p" * 300,
    )
    db_session.add(preview_message)
    await db_session.commit()
    db_session.info["_created"]["threads"].extend(row.id for row in rows)
    db_session.info["_created"]["chat_messages"].append(preview_message.id)
    workspace_id = cast(UUID, workspace.id)
    owner_id = cast(UUID, owner.id)

    access_calls: list[dict[str, object]] = []
    real_get_workspace = workspace_access.get_workspace

    async def recording_get_workspace(*args: Any, **kwargs: Any) -> Any:
        access_calls.append(kwargs)
        return await real_get_workspace(*args, **kwargs)

    monkeypatch.setattr(workspace_access, "get_workspace", recording_get_workspace)

    first_page = await thread_service.list_workspace_threads(
        db_session, workspace_id, owner_id, limit=2, offset=0
    )
    assert first_page is not None
    threads, total, previews = first_page
    assert [thread.id for thread in threads] == tied_ids
    assert [thread.conversation_id for thread in threads] == [
        conversations[1].id,
        conversations[0].id,
    ]
    assert total == 3
    assert previews == {tied_ids[0]: "p" * 240, tied_ids[1]: None}
    assert access_calls == [{"load_conversations": False, "load_collections": False}]

    resolved_page = await thread_service.list_workspace_threads(
        db_session,
        workspace_id,
        owner_id,
        status_filter=ThreadStatus.RESOLVED,
        limit=20,
        offset=0,
    )
    assert resolved_page is not None
    resolved, resolved_total, _ = resolved_page
    assert [thread.title for thread in resolved] == ["older-resolved"]
    assert resolved_total == 1

    second_page = await thread_service.list_workspace_threads(
        db_session, workspace_id, owner_id, limit=2, offset=2
    )
    assert second_page is not None
    page_threads, page_total, _ = second_page
    assert [thread.title for thread in page_threads] == ["older-resolved"]
    assert page_total == 3


async def test_workspace_listing_excludes_threads_under_deleted_conversations(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    owner = await user_factory()
    workspace = Workspace(
        id=uuid4(),
        name="workspace",
        owner_id=owner.id,
        organization_id=owner.organization_id,
    )
    conversation = Conversation(
        id=uuid4(),
        workspace_id=workspace.id,
        title="deleted",
        created_by_id=owner.id,
        is_deleted=True,
    )
    thread = Thread(
        id=uuid4(),
        conversation_id=conversation.id,
        title="hidden",
        created_by_id=owner.id,
    )
    live_conversation = Conversation(
        id=uuid4(),
        workspace_id=workspace.id,
        title="live",
        created_by_id=owner.id,
    )
    deleted_thread = Thread(
        id=uuid4(),
        conversation_id=live_conversation.id,
        title="also hidden",
        created_by_id=owner.id,
        is_deleted=True,
    )
    db_session.add_all(
        [workspace, conversation, thread, live_conversation, deleted_thread]
    )
    await db_session.commit()
    db_session.info["_created"]["workspaces"].append(workspace.id)
    db_session.info["_created"]["conversations"].extend(
        [conversation.id, live_conversation.id]
    )
    db_session.info["_created"]["threads"].extend([thread.id, deleted_thread.id])
    workspace_id = cast(UUID, workspace.id)
    owner_id = cast(UUID, owner.id)

    result = await thread_service.list_workspace_threads(
        db_session, workspace_id, owner_id
    )
    assert result is not None
    assert result == ([], 0, {})

    setattr(workspace, "is_deleted", True)
    await db_session.commit()
    assert (
        await thread_service.list_workspace_threads(db_session, workspace_id, owner_id)
        is None
    )


async def test_workspace_listing_allows_viewers_and_public_readers_but_not_outsiders(
    db_session: AsyncSession,
    user_factory: Callable[..., Awaitable[User]],
) -> None:
    owner = await user_factory()
    viewer = await user_factory()
    outsider = await user_factory()
    workspace = Workspace(
        id=uuid4(),
        name="shared workspace",
        owner_id=owner.id,
        organization_id=owner.organization_id,
    )
    workspace.members.append(
        WorkspaceMember(user_id=viewer.id, role=WorkspaceRole.VIEWER)
    )
    db_session.add(workspace)
    await db_session.commit()
    db_session.info["_created"]["workspaces"].append(workspace.id)
    workspace_id = cast(UUID, workspace.id)
    viewer_id = cast(UUID, viewer.id)
    outsider_id = cast(UUID, outsider.id)

    assert await thread_service.list_workspace_threads(
        db_session, workspace_id, viewer_id
    ) == ([], 0, {})
    assert (
        await thread_service.list_workspace_threads(
            db_session, workspace_id, outsider_id
        )
        is None
    )

    setattr(workspace, "is_public", True)
    await db_session.commit()
    assert await thread_service.list_workspace_threads(
        db_session, workspace_id, outsider_id
    ) == ([], 0, {})
