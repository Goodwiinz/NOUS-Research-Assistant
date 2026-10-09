"""The agent tool node's outer deadline for the running tool call (IN-2).

``_nodes_tools`` bounds every tool call with ``asyncio.wait_for``. That limit
starts before ``execute_tool`` resolves the acting user and claims the durable
operation row, so a tool that must time out on its own before the outer limit
cancels it (``execute_code`` interrupts its cell and keeps the sandbox) reads
the deadline here rather than starting a fresh budget when it is dispatched.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_TOOL_CALL_DEADLINE: ContextVar[Optional[float]] = ContextVar(
    "tool_call_deadline", default=None
)


@contextmanager
def tool_call_deadline_scope(timeout: float) -> Iterator[None]:
    """Record ``timeout`` seconds from now, on the running loop's clock, as the
    deadline of the tool call started inside this block."""
    token = _TOOL_CALL_DEADLINE.set(asyncio.get_running_loop().time() + timeout)
    try:
        yield
    finally:
        _TOOL_CALL_DEADLINE.reset(token)


def tool_call_deadline() -> Optional[float]:
    """The running tool call's deadline on the loop clock, or None outside one."""
    return _TOOL_CALL_DEADLINE.get()
