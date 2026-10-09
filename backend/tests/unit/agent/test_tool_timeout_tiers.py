"""Regression tests for per-tool timeout tier assignment.

Pins the fix for the dev-trace failure mode (traces 019f2a48-9083 /
019f2245-cf9d): ``search_arxiv`` ran under the default 30s tool timeout,
but its worst-case internal path in ``arxiv_service._make_request`` (3s
rate gate + 20s httpx timeout + 2s sleep + a second attempt) exceeds
30s, so every call died with ``TimeoutError`` at exactly 30s and the
agent re-issued the identical query until the tool-loop cap killed the
turn. The tool must sit in the slow tier, and must stay in the
no-outer-retry set so the 120s backstop is not amplified 2x by
``retry_transient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.services.agent._nodes_tools import (
    _NO_OUTER_RETRY_TOOLS,
    _SLOW_TOOL_TIMEOUT_SECONDS,
    _SLOW_TOOLS,
    TOOL_TIMEOUT_SECONDS,
)


def _resolve_timeout(tool_name: str) -> int:
    """Mirror of the tier selection in ``_execute_single_tool_call``."""
    return (
        _SLOW_TOOL_TIMEOUT_SECONDS if tool_name in _SLOW_TOOLS else TOOL_TIMEOUT_SECONDS
    )


@pytest.mark.unit
class TestToolTimeoutTiers:
    def test_search_arxiv_uses_slow_tier(self):
        # arxiv_service's internal worst case (~45-50s) exceeds the 30s
        # default; the 30s tier guarantees a TimeoutError on slow arXiv days.
        assert _resolve_timeout("search_arxiv") == _SLOW_TOOL_TIMEOUT_SECONDS

    def test_search_arxiv_keeps_single_outer_attempt(self):
        # arxiv_service retries internally; an outer retry on top of the
        # slow tier would amplify worst-case wall clock to ~2x the cap.
        assert "search_arxiv" in _NO_OUTER_RETRY_TOOLS

    def test_destructive_tools_are_never_retried_by_the_outer_wrapper(self):
        assert {
            "ingest_arxiv_papers",
            "create_project",
            "add_document_to_project",
            "create_project_note",
            "create_draft",
            "revise_draft",
            "execute_code",
            "forget_memory",
        } <= _NO_OUTER_RETRY_TOOLS

    def test_ingest_and_draft_tools_stay_slow(self):
        for tool in (
            "ingest_arxiv_papers",
            "create_draft",
            "revise_draft",
            "compare_documents",
        ):
            assert _resolve_timeout(tool) == _SLOW_TOOL_TIMEOUT_SECONDS

    def test_default_tier_unchanged_for_fast_tools(self):
        assert _resolve_timeout("search_documents") == TOOL_TIMEOUT_SECONDS
        assert TOOL_TIMEOUT_SECONDS == 30


def test_execute_code_cell_budget_fits_inside_its_outer_tier() -> None:
    """IN-2: execute_code must time out inside the sandbox (interrupt, keep the
    box), never through the outer wait_for (cancellation kills the box).
    Package install + cell share the budget, each may add one probe, and the
    manager's minimum cell timeout is 1 s."""
    from src.services.sandbox.e2b_sandbox_manager import (
        AGENT_CELL_HEADROOM_SECONDS,
        AGENT_CELL_TIMEOUT_SECONDS,
        POST_TIMEOUT_PROBE_SECONDS,
    )

    assert _resolve_timeout("execute_code") == _SLOW_TOOL_TIMEOUT_SECONDS
    assert (
        AGENT_CELL_TIMEOUT_SECONDS + 2 * POST_TIMEOUT_PROBE_SECONDS + 1
        < _SLOW_TOOL_TIMEOUT_SECONDS
    )
    # The budget is clamped to end this long before the outer limit. The
    # headroom covers one probe and the cell timeout's rounding up to whole
    # seconds, and still leaves a few seconds to record the result
    # (complete_operation) inside the outer limit. A call whose setup is
    # quick still gets the full budget.
    assert AGENT_CELL_HEADROOM_SECONDS - (POST_TIMEOUT_PROBE_SECONDS + 1) >= 3
    assert (
        AGENT_CELL_TIMEOUT_SECONDS + AGENT_CELL_HEADROOM_SECONDS
        <= _SLOW_TOOL_TIMEOUT_SECONDS
    )


def _execution(stdout: str = "", error: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        logs=SimpleNamespace(stdout=stdout, stderr=""), error=error, results=[]
    )


class _KernelBox:
    """Stateful fake E2B box (same model as tests/unit/services/test_sandbox.py):
    a cancelled cell leaves the namespace intact, like a kernel interrupt."""

    def __init__(self) -> None:
        self.ns: dict[str, Any] = {}
        self.killed = False
        self.sandbox_id = "kernel-box"

    async def run_code(self, code: str, **_kwargs: Any) -> SimpleNamespace:
        if self.killed:
            raise RuntimeError("sandbox was killed")
        first = code.splitlines()[0] if code else ""
        if first.startswith("#sleep "):
            await asyncio.sleep(float(first.split()[1]))
        if "pip" in code and "install" in code:
            return _execution()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(code, self.ns)
        except Exception as exc:
            return _execution(out.getvalue(), f"{type(exc).__name__}: {exc}")
        return _execution(out.getvalue())

    async def kill(self) -> None:
        self.killed = True


async def _train_then_follow_up(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slow_tier: float,
    cell_budget: int,
    train_code: str,
    setup_seconds: float = 0,
) -> tuple[dict[str, Any], dict[str, Any], list[bool]]:
    """Run load, train and describe cells through the tool node on scaled
    time, and return the train and describe executions and whether each box
    created was killed before cleanup. ``setup_seconds`` delays the train call
    inside the outer limit, as execute_tool's user lookup and operation claim
    do before the tool runs. The headroom is scaled too: the fake kernel
    answers the probe at once, so it covers the cell timeout's rounding up to
    whole seconds plus 1.2 s for the probe and the tool node's bookkeeping."""
    from src.services.agent import _nodes_tools, graph, tools_impl
    from src.services.sandbox import e2b_sandbox_manager as sandbox

    boxes: list[_KernelBox] = []

    async def create(**_kwargs: Any) -> _KernelBox:
        boxes.append(_KernelBox())
        return boxes[-1]

    monkeypatch.setenv("E2B_API_KEY", "fixture-key")
    monkeypatch.setattr(sandbox, "_e2b_available", True)
    monkeypatch.setattr(sandbox, "AsyncSandbox", SimpleNamespace(create=create))
    monkeypatch.setattr(sandbox, "_sandbox_manager", sandbox.SandboxManager())
    monkeypatch.setattr(sandbox, "AGENT_CELL_TIMEOUT_SECONDS", cell_budget)
    monkeypatch.setattr(sandbox, "AGENT_CELL_HEADROOM_SECONDS", 2.2)
    monkeypatch.setattr(_nodes_tools, "TOOL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(_nodes_tools, "_SLOW_TOOL_TIMEOUT_SECONDS", slow_tier)

    async def execute(**kwargs: Any) -> dict[str, Any]:
        # execute_tool's dispatch for execute_code, minus durable-operation rows.
        if kwargs["args"].get("description") == "train":
            await asyncio.sleep(setup_seconds)
        return cast(
            dict[str, Any],
            await tools_impl._tool_execute_code(
                kwargs["args"], thread_id=kwargs["thread_id"], current_user=MagicMock()
            ),
        )

    monkeypatch.setattr(graph, "_get_execute_tool", lambda: execute)
    config = {
        "configurable": {
            "user_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "thread_id": "thread-in2",
        }
    }
    turn = {"tool_operation_protocol_version": 1, "tool_operation_turn_id": "turn-in2"}

    async def cell(call_id: str, code: str) -> dict[str, Any]:
        result = await _nodes_tools._execute_single_tool(
            {
                "name": "execute_code",
                "id": call_id,
                "args": {"code": code, "description": call_id},
            },
            config,
            {},
            turn,
        )
        return cast(dict[str, Any], result["execution"])

    manager = sandbox.get_sandbox_manager()
    try:
        load = await cell("load", "df = [1, 2, 3]")
        assert load["status"] == "completed", load["result"]
        train = await cell("train", train_code)
        follow_up = await cell("describe", "print(len(df))")
        return train, follow_up, [box.killed for box in boxes]
    finally:
        await manager.cleanup_all()
        if manager._cleanup_task is not None:
            manager._cleanup_task.cancel()


@pytest.mark.unit
async def test_long_cell_keeps_conversation_sandbox_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IN-2 end to end through the tool node. Time is scaled: default tier
    0.5 s, slow tier 4 s, cell budget 1 s. A 3 s cell sits between the cell
    budget and the slow tier, as a 45 s training cell sits between 30 s and
    120 s. Before the fix the 0.5 s outer limit cancelled the call, #1864's
    cancellation path killed the box, and the next cell raised NameError.
    """
    train, follow_up, killed = await _train_then_follow_up(
        monkeypatch, slow_tier=4, cell_budget=1, train_code="#sleep 3\nmodel = sum(df)"
    )
    assert train["status"] == "failed"
    assert follow_up["status"] == "completed", follow_up["result"]
    assert follow_up["result"]["stdout"] == "3\n"
    assert killed == [False]


@pytest.mark.unit
async def test_slow_setup_still_lets_the_sandbox_time_out_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer limit starts before execute_tool resolves the user and claims
    the operation row. Scaled: slow tier 8 s, cell budget 6 s, and that setup
    takes 3 s, as a 30 s database stall would in production. A fresh 6 s
    budget would end at 9 s, so the outer limit would cancel the cell and
    kill the box. Clamped to end 2.2 s before the outer limit, the cell gets
    at most 3 s (rounded up from what is left) and the sandbox interrupts it
    itself, at least 1.2 s before the outer limit however slowly this runs.

    Mutation: drop the clamp in ``_tool_execute_code``, or the deadline scope
    around ``wait_for`` in ``_nodes_tools``; the train cell then gets 6 s, the
    outer limit cancels it and kills the box, and the next cell raises
    NameError.
    """
    cell_budget = 6
    train, follow_up, killed = await _train_then_follow_up(
        monkeypatch,
        slow_tier=8,
        cell_budget=cell_budget,
        train_code="#sleep 30\nmodel = sum(df)",
        setup_seconds=3,
    )
    assert train["status"] == "failed"
    stderr = train["result"].get("stderr", "")
    # The sandbox's own limit fired, and it was the clamped budget, not the
    # full one; the exact value depends on how long setup really took.
    timed_out = re.search(r"timed out after (\d+)s", stderr)
    assert timed_out is not None, train["result"]
    assert 1 <= int(timed_out.group(1)) < cell_budget
    assert "still available" in stderr
    assert follow_up["status"] == "completed", follow_up["result"]
    assert follow_up["result"]["stdout"] == "3\n"
    assert killed == [False]
