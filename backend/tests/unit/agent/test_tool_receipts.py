"""Durable per-tool-call receipts against checkpoint replay (audit B8-I1).

``tool_node`` commits tool side effects before any checkpoint write, and the
per-turn ``state["tool_executions"]`` guard dies with the node. A side
effecting call whose ``tool_call_id`` already has a receipt must not run
again; a fresh one must run and leave a receipt behind.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.agent._nodes_tools import SIDE_EFFECT_TOOLS, _execute_single_tool

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSession:
    """Records every statement; answers the receipt lookup with *receipt*."""

    def __init__(self, receipt: Optional[str]) -> None:
        self.receipt = receipt
        self.statements: List[str] = []
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        self.statements.append(str(statement))
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.receipt
        return result

    async def commit(self) -> None:
        self.commits += 1


@asynccontextmanager
async def _session_ctx(session: _FakeSession) -> AsyncIterator[_FakeSession]:
    yield session


async def _run(
    tool_name: str, session: _FakeSession, executor: AsyncMock
) -> dict[str, Any]:
    with (
        patch("src.services.agent.graph._get_execute_tool", return_value=executor),
        patch(
            "src.services.agent.tool_session.tool_session",
            lambda: _session_ctx(session),
        ),
    ):
        return await _execute_single_tool(
            {"name": tool_name, "args": {"name": "x"}, "id": "call-1"},
            {"configurable": {"user_id": "u1", "thread_id": "t1"}},
            {},
        )


async def test_side_effect_tools_are_the_fixed_audited_set() -> None:
    assert SIDE_EFFECT_TOOLS == {
        "create_project",
        "create_project_note",
        "create_draft",
        "revise_draft",
        "ingest_arxiv_papers",
        "execute_code",
    }


async def test_replayed_call_is_skipped_without_executing() -> None:
    session = _FakeSession(receipt="call-1")
    executor = AsyncMock(return_value={"project_id": "p1"})

    result = await _run("create_project", session, executor)

    executor.assert_not_awaited()
    assert result["execution"]["status"] == "skipped"
    payload = json.loads(result["message"].content)
    assert payload == {
        "status": "skipped",
        "reason": "already_executed",
        "tool_call_id": "call-1",
    }
    # No "error" key: the classifier must not count a skipped replay as one.
    assert "error" not in payload
    assert result["error_increment"] == 0


async def test_fresh_call_executes_and_writes_a_receipt() -> None:
    session = _FakeSession(receipt=None)
    executor = AsyncMock(return_value={"project_id": "p1"})

    result = await _run("create_project", session, executor)

    executor.assert_awaited_once()
    assert result["execution"]["status"] == "completed"
    assert any("INSERT INTO agent_tool_receipts" in s for s in session.statements)
    assert session.commits == 1


async def test_failed_call_leaves_no_receipt() -> None:
    session = _FakeSession(receipt=None)
    executor = AsyncMock(return_value={"error": "boom"})

    await _run("create_project", session, executor)

    assert not any("INSERT INTO agent_tool_receipts" in s for s in session.statements)


async def test_read_only_tools_never_touch_the_receipt_table() -> None:
    session = _FakeSession(receipt=None)
    executor = AsyncMock(return_value={"results": []})

    result = await _run("search_arxiv", session, executor)

    executor.assert_awaited_once()
    assert result["execution"]["status"] == "completed"
    assert session.statements == []
