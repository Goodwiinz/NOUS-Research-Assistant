"""Agent history must respect deletion at every level of the ownership chain.

No app server, credentials, model calls, or external database are used.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.agent.execute import (
    ThreadMessagesResponse,
    get_thread_messages,
    list_agent_threads,
)
from src.models.chat_message import ChatMessage, MessageRole
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.threads import workspace_access

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def history() -> AsyncIterator[SimpleNamespace]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (
            Workspace,
            WorkspaceMember,
            Conversation,
            Thread,
            ChatMessage,
            Citation,
        ):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = SimpleNamespace(id=uuid4(), organization_id=uuid4())
        workspace = Workspace(
            id=uuid4(),
            name="Synthetic audit workspace",
            owner_id=user.id,
            organization_id=user.organization_id,
        )
        conversation = Conversation(
            id=uuid4(),
            workspace_id=workspace.id,
            title="Synthetic audit",
            created_by_id=user.id,
        )
        thread = Thread(
            id=uuid4(),
            conversation_id=conversation.id,
            title="Synthetic audit",
            created_by_id=user.id,
            message_count=1,
        )
        message = ChatMessage(
            id=uuid4(),
            thread_id=thread.id,
            user_id=user.id,
            role=MessageRole.USER,
            content="Synthetic deleted chat content",
        )
        db.add_all([workspace, conversation, thread, message])
        await db.commit()
        yield SimpleNamespace(
            db=db,
            user=user,
            workspace=workspace,
            conversation=conversation,
            thread=thread,
            message=message,
        )
    await engine.dispose()


async def read_history(
    history: SimpleNamespace, limit: int | None
) -> ThreadMessagesResponse:
    result = await get_thread_messages(
        history.thread.id,
        limit=limit,
        before=None,
        current_user=history.user,
        db=history.db,
    )
    assert isinstance(result, ThreadMessagesResponse)
    return result


@pytest.mark.parametrize("limit", [None, 50], ids=["full", "paged"])
async def test_control_active_history_is_readable(
    history: SimpleNamespace, limit: int | None
) -> None:
    result = await read_history(history, limit)
    assert [message.content for message in result.messages] == [history.message.content]
    assert result.total == 1


@pytest.mark.parametrize("limit", [None, 50], ids=["full", "paged"])
@pytest.mark.parametrize("parent", ["workspace", "conversation", "thread"])
async def test_deleted_parent_revokes_agent_history(
    history: SimpleNamespace, limit: int | None, parent: str
) -> None:
    # These deletes flag the parent without cascading to descendants.
    getattr(history, parent).is_deleted = True
    await history.db.commit()
    assert (
        await workspace_access.get_thread(
            history.db, history.thread.id, history.user.id, include_messages=False
        )
        is None
    )
    with pytest.raises(HTTPException) as raised:
        await read_history(history, limit)
    assert raised.value.status_code == 404


@pytest.mark.parametrize("limit", [None, 50], ids=["full", "paged"])
async def test_deleted_message_is_absent_from_agent_history(
    history: SimpleNamespace, limit: int | None
) -> None:
    history.message.is_deleted = True
    await history.db.commit()
    result = await read_history(history, limit)
    assert result.messages == []
    assert result.total == 0


@pytest.mark.parametrize("limit", [None, 50], ids=["full", "paged"])
async def test_foreign_user_cannot_read_history(
    history: SimpleNamespace, limit: int | None
) -> None:
    history.user = SimpleNamespace(
        id=uuid4(), organization_id=history.user.organization_id
    )
    with pytest.raises(HTTPException) as raised:
        await read_history(history, limit)
    assert raised.value.status_code == 404


async def test_deleted_rows_do_not_affect_page_count_or_has_more(
    history: SimpleNamespace,
) -> None:
    start = datetime(2026, 9, 1)
    history.message.created_at = start
    for minute, deleted in [(1, False), (2, True), (3, True)]:
        history.db.add(
            ChatMessage(
                id=uuid4(),
                thread_id=history.thread.id,
                user_id=history.user.id,
                role=MessageRole.USER,
                content=f"message-{minute}",
                created_at=start + timedelta(minutes=minute),
                is_deleted=deleted,
            )
        )
    await history.db.commit()
    first = await get_thread_messages(
        history.thread.id,
        limit=1,
        before=None,
        current_user=history.user,
        db=history.db,
    )
    assert [message.content for message in first.messages] == ["message-1"]
    assert first.total == 2
    assert first.has_more is True
    older = await get_thread_messages(
        history.thread.id,
        limit=1,
        before=start + timedelta(minutes=1),
        current_user=history.user,
        db=history.db,
    )
    assert [message.content for message in older.messages] == [history.message.content]
    assert older.total == 1
    assert older.has_more is False


async def test_thread_list_filters_deleted_ancestors_before_limit_and_count() -> None:
    # JSONB containment is PostgreSQL-specific; inspect the actual route query
    # while the sibling history tests exercise deletion using real ORM reads.
    db = AsyncMock()
    rows = MagicMock()
    rows.all.return_value = []
    db.execute.return_value = rows
    user = User(id=uuid4())
    result = await list_agent_threads(current_user=user, db=db)
    query = db.execute.await_args.args[0]
    sql = str(query.compile(dialect=postgresql.dialect()))
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    for table in ("workspaces", "conversations", "threads"):
        assert f"{table}.is_deleted = false" in where
    assert "workspaces.owner_id =" in where
    assert "count(*) OVER ()" in sql
    assert result.total == 0
