"""Unit tests for the identical-failure circuit breaker in tool_dedupe.

Pins the fix for the retry storm seen in dev traces 019f2a48-9083 /
019f2245-cf9d: the model re-issued an identical failing tool call at
steps 3/6/9/12 until the loop cap killed the turn. Completed-only dedupe
deliberately lets a failed call retry once (genuine transients recover);
the breaker stops the third+ identical attempt within a turn.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent.tool_dedupe import (
    FAILED_RETRY_THRESHOLD,
    build_failure_capped_execution_entry,
    build_failure_capped_tool_message,
    find_repeated_failures,
)


def _turn(messages_tool_ids: list[str]) -> list:
    """Build a message list: one HumanMessage boundary + ToolMessages."""
    msgs: list = [HumanMessage(content="find papers")]
    msgs.append(AIMessage(content=""))
    for tid in messages_tool_ids:
        msgs.append(ToolMessage(content="x", tool_call_id=tid))
    return msgs


def _failed(exec_id: str, tool: str, args: dict) -> dict:
    return {
        "id": exec_id,
        "tool_name": tool,
        "args": args,
        "status": "failed",
        "error": "TimeoutError()",
    }


ARGS = {"query": "retrieval-augmented generation", "max_results": 5}


@pytest.mark.unit
class TestFindRepeatedFailures:
    def test_no_cap_below_threshold(self):
        # One failure -> retry allowed (transients recover).
        capped = find_repeated_failures(
            [{"id": "tc-2", "name": "search_arxiv", "args": ARGS}],
            _turn(["tc-1"]),
            [_failed("tc-1", "search_arxiv", ARGS)],
        )
        assert capped == {}

    def test_caps_third_identical_attempt(self):
        prior = [
            _failed("tc-1", "search_arxiv", ARGS),
            _failed("tc-2", "search_arxiv", ARGS),
        ]
        capped = find_repeated_failures(
            [{"id": "tc-3", "name": "search_arxiv", "args": ARGS}],
            _turn(["tc-1", "tc-2"]),
            prior,
        )
        assert set(capped) == {"tc-3"}
        # Cites the most recent failure.
        assert capped["tc-3"]["id"] == "tc-2"

    def test_different_args_not_capped(self):
        prior = [
            _failed("tc-1", "search_arxiv", ARGS),
            _failed("tc-2", "search_arxiv", ARGS),
        ]
        capped = find_repeated_failures(
            [{"id": "tc-3", "name": "search_arxiv", "args": {"query": "other"}}],
            _turn(["tc-1", "tc-2"]),
            prior,
        )
        assert capped == {}

    def test_cross_turn_failures_ignored(self):
        # Failures from a previous turn (tool ids not in the current-turn
        # slice) must not trip the breaker — the user asked again.
        prior = [
            _failed("old-1", "search_arxiv", ARGS),
            _failed("old-2", "search_arxiv", ARGS),
        ]
        capped = find_repeated_failures(
            [{"id": "tc-1", "name": "search_arxiv", "args": ARGS}],
            _turn([]),  # fresh turn, no in-turn tool messages
            prior,
        )
        assert capped == {}

    def test_completed_runs_do_not_count(self):
        prior = [
            _failed("tc-1", "search_arxiv", ARGS),
            {
                "id": "tc-2",
                "tool_name": "search_arxiv",
                "args": ARGS,
                "status": "completed",
                "result": {"papers": []},
            },
        ]
        capped = find_repeated_failures(
            [{"id": "tc-3", "name": "search_arxiv", "args": ARGS}],
            _turn(["tc-1", "tc-2"]),
            prior,
        )
        assert capped == {}

    def test_threshold_constant_is_two(self):
        # The breaker allows exactly one retry; changing this changes agent
        # behavior materially — bump deliberately, not accidentally.
        assert FAILED_RETRY_THRESHOLD == 2

    @pytest.mark.asyncio
    async def test_filtered_tool_node_caps_third_identical_failure(self, monkeypatch):
        from src.services.agent import _nodes_tools

        async def fail_if_called(*_args, **_kwargs):
            raise AssertionError("circuit-broken call must not execute")

        monkeypatch.setattr(_nodes_tools, "_execute_single_tool", fail_if_called)
        node = _nodes_tools.make_filtered_tool_node({"search_arxiv"})
        messages = _turn(["tc-1", "tc-2"])
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"id": "tc-3", "name": "search_arxiv", "args": ARGS}],
            )
        )

        result = await node(
            {
                "messages": messages,
                "tool_executions": [
                    _failed("tc-1", "search_arxiv", ARGS),
                    _failed("tc-2", "search_arxiv", ARGS),
                ],
                "error_count": 2,
                "last_error": "TimeoutError()",
                "page_context": {},
                "tool_loop_count": 2,
            },
            {"configurable": {}},
        )

        assert result["error_count"] == 3
        assert result["tool_executions"][-1]["error"].startswith("repeated_failure:")


@pytest.mark.unit
class TestCappedBuilders:
    def test_tool_message_instructs_no_identical_retry(self):
        tc = {"id": "tc-3", "name": "search_arxiv", "args": ARGS}
        msg = build_failure_capped_tool_message(
            "tc-3", tc, _failed("tc-2", "search_arxiv", ARGS), 2
        )
        payload = json.loads(msg.content)
        assert payload["error_category"] == "repeated_failure"
        assert "Do NOT retry" in payload["error"]
        assert "TimeoutError()" in payload["error"]
        assert msg.tool_call_id == "tc-3"

    def test_execution_entry_shape(self):
        tc = {"id": "tc-3", "name": "search_arxiv", "args": ARGS}
        entry = build_failure_capped_execution_entry(
            "tc-3", tc, _failed("tc-2", "search_arxiv", ARGS)
        )
        assert entry["status"] == "failed"
        assert entry["capped_from"] == "tc-2"
        assert entry["tool_name"] == "search_arxiv"
        assert entry["args"] == ARGS


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("specialist", [False, True])
async def test_exhausted_arxiv_retry_is_blocked_until_next_user_turn(
    specialist: bool,
) -> None:
    """Trace 47c93300: internal retries must not restart on the next model pass.

    Mutation check: remove the retry_exhausted branch in find_repeated_failures;
    this test fails at executor.await_count == 1. Run this file with pytest -q.
    """
    from src.services.agent import _nodes_tools
    from src.services.agent.error_recovery import tool_error_payload

    node = (
        _nodes_tools.make_filtered_tool_node({"search_arxiv"})
        if specialist
        else _nodes_tools.tool_node
    )
    executor = AsyncMock(
        return_value=tool_error_payload(
            "search_arxiv", RuntimeError("ArXiv rate limited (HTTP 429)")
        )
    )
    state = {
        "messages": [HumanMessage(content="Find analytical method validation papers")],
        "tool_executions": [],
        "page_context": {},
        "error_count": 0,
        "tool_loop_count": 0,
    }
    with patch("src.services.agent.graph._get_execute_tool", return_value=executor):
        for attempt in range(2):
            state["messages"].append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"search-{attempt}",
                            "name": "search_arxiv",
                            "args": ARGS,
                        }
                    ],
                )
            )
            update = await node(state, {"configurable": {}})
            state["messages"].extend(update.pop("messages"))
            state.update(update)

        assert executor.await_count == 1
        message = state["messages"][-1]
        assert message.status == "error"
        assert "exhausted its retry budget" in message.content
        assert "60 seconds" in message.content
        assert state["tool_executions"][-1]["capped_from"] == "search-0"

        state["messages"].extend(
            [
                HumanMessage(content="Try again now"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "search-new", "name": "search_arxiv", "args": ARGS}
                    ],
                ),
            ]
        )
        await node(state, {"configurable": {}})
        assert executor.await_count == 2
