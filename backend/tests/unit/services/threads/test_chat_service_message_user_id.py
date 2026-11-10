"""REST ``create_message`` records the author of user turns (audit B8-S10).

``ChatMessageCreate.role`` is the *schemas* MessageRole (a str enum) while the
service compared it with ``==`` against the *models* MessageRole (a plain
Enum). That comparison is always False, so user messages created without a
``client_message_id`` were stored with ``user_id = NULL``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from src.models.chat_message import ChatMessage
from src.models.conversation import Conversation
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.schemas.chat import ChatMessageCreate, MessageRole
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.unit


@pytest.fixture
async def db() -> AsyncGenerator[tuple[AsyncSession, uuid.UUID, uuid.UUID], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(ChatMessage.__table__.create)
        await conn.run_sync(Workspace.__table__.create)
        await conn.run_sync(Conversation.__table__.create)

    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        session.add(
            Workspace(
                id=workspace_id,
                name="workspace",
                owner_id=user_id,
                organization_id=uuid.uuid4(),
                members=[],
            )
        )
        session.add(
            Conversation(
                id=conversation_id,
                workspace_id=workspace_id,
                title="conversation",
                created_by_id=user_id,
            )
        )
        session.add(
            Thread(
                id=thread_id,
                conversation_id=conversation_id,
                created_by_id=user_id,
            )
        )
        await session.commit()
        yield session, user_id, thread_id
    await engine.dispose()


async def _create(
    session: AsyncSession, user_id: uuid.UUID, thread_id: uuid.UUID, role: MessageRole
) -> ChatMessage:
    service = ChatService(session)
    thread = (
        await session.execute(
            select(Thread)
            .options(
                selectinload(Thread.conversation).selectinload(Conversation.workspace)
            )
            .where(Thread.id == thread_id)
        )
    ).scalar_one()
    set_committed_value(thread.conversation.workspace, "members", [])

    with patch.object(service, "get_thread", new=AsyncMock(return_value=thread)):
        message = await service.create_message(
            ChatMessageCreate(
                thread_id=thread_id,
                role=role,
                content="hello",
                client_message_id=None,
            ),
            user_id,
        )
    assert message is not None
    return message


async def _stored_user_id(session: AsyncSession, message_id: Any) -> Any:
    return (
        await session.execute(
            select(ChatMessage.user_id).where(ChatMessage.id == message_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_user_message_without_client_id_stores_author(
    db: tuple[AsyncSession, uuid.UUID, uuid.UUID],
) -> None:
    session, user_id, thread_id = db

    message = await _create(session, user_id, thread_id, MessageRole.USER)

    assert message.user_id == user_id
    assert await _stored_user_id(session, message.id) == user_id


@pytest.mark.asyncio
async def test_assistant_message_has_no_author(
    db: tuple[AsyncSession, uuid.UUID, uuid.UUID],
) -> None:
    session, user_id, thread_id = db

    message = await _create(session, user_id, thread_id, MessageRole.ASSISTANT)

    assert message.user_id is None
    assert await _stored_user_id(session, message.id) is None
