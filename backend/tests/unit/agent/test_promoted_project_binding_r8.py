"""R8-A6: a project promoted from message text survives into the next turn.

Each turn's input re-seeds ``current_project_id`` / ``page_context`` from the
request, so ``rag_node``'s in-state promotion alone was lost on turn 2. The
fix persists the verified id into the thread binding through the request
path's own owner-gated writer, which ``_resolve_and_bind_project`` (path 2)
then resolves on every later turn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import HumanMessage

pytestmark = pytest.mark.unit

THREAD_ID = uuid4()
USER_ID = uuid4()
PROJECT_ID = str(uuid4())


def _result(first: Any = None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = None
    return result


def _turn_one(persistence: str = "durable") -> tuple[dict, dict]:
    state = {
        "messages": [
            HumanMessage(
                content=f"Use https://app.example/projects/{PROJECT_ID} and "
                "list the methods sections of its documents."
            )
        ],
        "page_context": {"type": "chat"},
        "current_project_id": "",
        "attachment_ids": [],
        "thread_persistence": persistence,
        "use_rag": True,
    }

    async def no_hits(query: str, user_id: str) -> list:
        return []

    config = {
        "configurable": {
            "thread_id": str(THREAD_ID),
            "user_id": str(USER_ID),
            "organization_id": str(uuid4()),
            "search_fn": no_hits,
        }
    }
    return state, config


async def _run_turn_one(
    *, persistence: str = "durable", owner_row: Any = (PROJECT_ID, "Thesis")
) -> tuple[dict, SimpleNamespace, AsyncMock, MagicMock]:
    from src.services.agent._nodes_rag import rag_node

    thread = SimpleNamespace(id=THREAD_ID, source_project_id=None)
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(first=owner_row))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    sessions = MagicMock()

    @asynccontextmanager
    async def fake_tool_session() -> AsyncIterator[Any]:
        sessions()
        yield db

    async def fake_attach(
        session: Any, thread_obj: Any, project_id: UUID, **_: Any
    ) -> None:
        thread_obj.source_project_id = project_id

    attach = AsyncMock(side_effect=fake_attach)
    state, config = _turn_one(persistence)
    with (
        patch(
            "src.services.agent._nodes_rag._verify_extracted_project_id",
            new=AsyncMock(return_value=PROJECT_ID),
        ),
        patch("src.services.agent.tool_session.tool_session", new=fake_tool_session),
        patch(
            "src.services.agent.tool_session.resolve_tool_user",
            new=AsyncMock(return_value=SimpleNamespace(id=USER_ID)),
        ),
        patch(
            "src.services.threads.workspace_access.get_thread",
            new=AsyncMock(return_value=thread),
        ),
        patch(
            "src.services.research.project_thread_service.attach_thread_to_project",
            new=attach,
        ),
    ):
        update = await rag_node(state, config)
    return update, thread, attach, sessions


async def test_verified_promotion_carries_into_next_turn() -> None:
    from src.services.agent.agent_execution_service import _resolve_and_bind_project
    from src.services.agent.runtime_snapshot import (
        empty_runtime_snapshot,
        runtime_state_fields,
    )

    update, thread, attach, _ = await _run_turn_one()

    assert update["current_project_id"] == PROJECT_ID
    attach.assert_awaited_once()
    call = attach.await_args
    assert call is not None
    assert call.args[1] is thread
    assert call.args[2] == UUID(PROJECT_ID)

    # Turn 2: the client re-sends no project. The request path resolves the
    # thread binding (path 2) into page_context, which both builders then
    # project into current_project_id.
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(first=(PROJECT_ID, "Thesis")))
    page_context: dict = {"type": "chat"}
    await _resolve_and_bind_project(
        db, cast(Any, SimpleNamespace(id=USER_ID)), thread, page_context
    )
    turn_two = runtime_state_fields(
        empty_runtime_snapshot(), page_context.get("project_id")
    )
    assert turn_two["current_project_id"] == PROJECT_ID


async def test_binding_keeps_the_request_path_owner_gate() -> None:
    """A promotion the owner-only request gate rejects is never persisted."""
    _, thread, attach, _ = await _run_turn_one(owner_row=None)

    attach.assert_not_awaited()
    assert thread.source_project_id is None


async def test_ephemeral_turn_never_opens_a_binding_session() -> None:
    update, _, attach, sessions = await _run_turn_one(persistence="ephemeral")

    assert update["current_project_id"] == PROJECT_ID
    sessions.assert_not_called()
    attach.assert_not_awaited()
