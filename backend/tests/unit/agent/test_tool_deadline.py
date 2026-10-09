"""IN-2: the tool node's outer deadline is visible only inside its scope.

``execute_code`` ends its sandbox budget before this deadline, so a value that
leaked past its scope (or a nested scope that did not restore the outer one)
would clamp an unrelated tool call's budget.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from src.services.agent.tool_deadline import (
    tool_call_deadline,
    tool_call_deadline_scope,
)

pytestmark = pytest.mark.unit


def test_no_deadline_outside_a_tool_call() -> None:
    assert tool_call_deadline() is None


async def test_scope_sets_the_deadline_and_resets_it_on_exit() -> None:
    loop = asyncio.get_running_loop()
    before = loop.time()
    with tool_call_deadline_scope(120):
        deadline = tool_call_deadline()
        assert deadline is not None
        assert before + 120 <= deadline <= loop.time() + 120
    assert tool_call_deadline() is None


async def test_scope_resets_the_deadline_when_the_call_raises() -> None:
    with pytest.raises(asyncio.TimeoutError):
        with tool_call_deadline_scope(1):
            raise asyncio.TimeoutError
    assert tool_call_deadline() is None


async def test_nested_scope_restores_the_outer_deadline() -> None:
    with tool_call_deadline_scope(120):
        outer = tool_call_deadline()
        with tool_call_deadline_scope(30):
            inner = tool_call_deadline()
            assert outer is not None and inner is not None
            assert inner < outer
        assert tool_call_deadline() == outer
    assert tool_call_deadline() is None


async def test_the_tool_task_under_wait_for_sees_the_deadline() -> None:
    """The tool node starts the tool under ``asyncio.wait_for``, which runs it
    in a new task; the task copies the context, so it reads the deadline."""

    async def tool() -> Optional[float]:
        return tool_call_deadline()

    with tool_call_deadline_scope(120):
        expected = tool_call_deadline()
        seen = await asyncio.wait_for(tool(), timeout=1)
    assert seen is not None and seen == expected
