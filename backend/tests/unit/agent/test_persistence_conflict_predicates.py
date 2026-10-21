"""Regression tests for partial-index inference in chat-message upserts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql import asyncpg

from src.services.agent.agent_execution_service import (
    _persist_assistant_message,
    _persist_user_message,
)


def _conflict_target_sql(statement: object) -> str:
    compiled = str(statement.compile(dialect=asyncpg.dialect()))
    return compiled.split("ON CONFLICT", 1)[1].split("DO NOTHING", 1)[0]


@pytest.mark.asyncio
async def test_user_conflict_predicate_uses_literal_role() -> None:
    result = MagicMock(rowcount=0)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    request = SimpleNamespace(
        thread_id=str(uuid4()),
        messages=[
            SimpleNamespace(
                role="user",
                content="hello",
                client_message_id=uuid4(),
            )
        ],
    )
    user = SimpleNamespace(id=uuid4())

    await _persist_user_message(db, user, request)

    target = _conflict_target_sql(db.execute.await_args.args[0])
    assert "role = 'user'" in target
    assert "$" not in target


@pytest.mark.asyncio
async def test_assistant_conflict_predicate_uses_literal_role() -> None:
    insert_result = MagicMock()
    insert_result.scalar_one_or_none.return_value = None
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[select_result, insert_result, select_result]),
        commit=AsyncMock(),
    )

    await _persist_assistant_message(
        db,
        thread_id=str(uuid4()),
        content="hello",
        model_name="gpt-5.6-luna",
        tool_executions_out=None,
        client_message_id=str(uuid4()),
    )

    target = _conflict_target_sql(db.execute.await_args_list[1].args[0])
    assert "role = 'assistant'" in target
    assert "$" not in target
