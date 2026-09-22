"""REST message create honours client_message_id (audit B8-I2).

`chat_messages` carries a partial-unique `(thread_id, client_message_id)`
index for user rows and the agent path already upserts against it. The REST
create ignored the key entirely, so the frontend's blanket POST retry after a
5xx/timeout-after-commit duplicated the row and re-bumped `message_count` /
`token_count`.

The dedupe is an ON CONFLICT DO NOTHING against a PostgreSQL partial index,
which sqlite cannot compile, so this asserts on the statements the service
issues (the pattern `test_persistence_conflict_predicates.py` uses).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql import asyncpg

from src.models.chat_message import ChatMessage
from src.schemas.chat import ChatMessageCreate, MessageRole
from src.services.threads.chat_service import ChatService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _service(*, insert_returns: Optional[UUID]) -> tuple[ChatService, MagicMock]:
    db = MagicMock()
    insert_result = MagicMock()
    insert_result.scalar_one_or_none.return_value = insert_returns
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = ChatMessage(id=uuid4())
    db.execute = AsyncMock(side_effect=[insert_result, select_result, MagicMock()])
    db.get = AsyncMock(return_value=ChatMessage(id=uuid4()))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return ChatService(db), db


def _thread() -> SimpleNamespace:
    workspace = SimpleNamespace(can_user_edit=lambda _uid: True)
    conversation = SimpleNamespace(workspace=workspace, update_activity=lambda: None)
    return SimpleNamespace(conversation=conversation)


def _payload(cmid: Optional[UUID]) -> ChatMessageCreate:
    return ChatMessageCreate(
        thread_id=uuid4(),
        role=MessageRole.USER,
        content="hello",
        client_message_id=cmid,
    )


async def _create(
    service: ChatService, data: ChatMessageCreate
) -> Optional[ChatMessage]:
    with patch.object(service, "get_thread", AsyncMock(return_value=_thread())):
        return await service.create_message(data, uuid4())


async def test_first_write_upserts_against_the_user_partial_index() -> None:
    service, db = _service(insert_returns=uuid4())

    message = await _create(service, _payload(uuid4()))

    assert message is not None
    sql = str(db.execute.await_args_list[0].args[0].compile(dialect=asyncpg.dialect()))
    target = sql.split("ON CONFLICT", 1)[1].split("DO NOTHING", 1)[0]
    assert "role = 'user'" in target
    assert "$" not in target
    # The counter update is the last statement on the insert path.
    assert "UPDATE threads" in str(db.execute.await_args_list[-1].args[0])


async def test_duplicate_returns_the_existing_row_without_bumping_counters() -> None:
    service, db = _service(insert_returns=None)  # ON CONFLICT deduped

    message = await _create(service, _payload(uuid4()))

    assert message is not None
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert not any("UPDATE threads" in s for s in statements)
    db.commit.assert_not_awaited()


async def test_without_a_client_message_id_the_orm_path_is_unchanged() -> None:
    service, db = _service(insert_returns=None)

    message = await _create(service, _payload(None))

    assert message is not None
    db.add.assert_called_once()
