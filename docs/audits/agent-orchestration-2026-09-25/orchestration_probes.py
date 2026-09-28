"""Offline audit probes. No network, real tool execution, or database writes."""

# mypy: ignore-errors
# These probes intentionally use dynamic mocks and ad-hoc payloads; production
# modules remain covered by the normal type-checking gate.

import asyncio
import json
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent import _builders, _nodes_tools


def tc(name, args, ident):
    return {"name": name, "args": args, "id": ident}


async def general_breaker():
    counter = 0

    async def pre(state, config):
        return {"intent": "general", "error_count": 0, "tool_loop_count": 0}

    async def no_op(state, config):
        return {}

    async def llm(state, config):
        nonlocal counter
        counter += 1
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        tc(
                            "search_documents",
                            {"query": f"try-{counter}"},
                            f"call-{counter}",
                        )
                    ],
                )
            ]
        }

    executor = AsyncMock(return_value={"error": "Permission denied"})
    with (
        patch.object(_builders, "preprocessing_node", pre),
        patch.object(_builders, "llm_node", llm),
        patch.object(_builders, "make_planner_node", return_value=no_op),
        patch.object(_builders, "make_compactor_node", return_value=no_op),
        patch.object(_builders, "memory_save_node", no_op),
        patch("src.services.agent.graph._get_execute_tool", return_value=executor),
    ):
        result = (
            await _builders.build_agent_graph()
            .compile()
            .ainvoke(
                {
                    "messages": [
                        HumanMessage(content="First question"),
                        AIMessage(content="STALE PRIOR TURN ANSWER"),
                        HumanMessage(content="Find this turn's documents"),
                    ],
                    "tool_executions": [],
                    "retrieved_contexts": [],
                    "page_context": {},
                    "reflection_count": 0,
                    "_reflection_result": None,
                }
            )
        )
    # Same predicate as both queued execution/resume result extractors.
    answer = next(
        (
            m.content
            for m in reversed(result["messages"])
            if getattr(m, "type", None) == "ai" and m.content
        ),
        "",
    )
    assert executor.await_count == 3
    assert answer == "STALE PRIOR TURN ANSWER"
    assert result["messages"][-1].tool_calls
    return {
        "tool_attempts": executor.await_count,
        "model_turns": counter,
        "error_count": result["error_count"],
        "extracted_answer": answer,
        "unanswered_call": result["messages"][-1].tool_calls[0]["id"],
    }


async def stale_read_after_write():
    read = tc("list_project_documents", {"project_id": "project-1"}, "read-before")
    write = tc(
        "add_document_to_project",
        {"project_id": "project-1", "document_id": "doc-1"},
        "add",
    )
    again = tc("list_project_documents", {"project_id": "project-1"}, "read-after")
    before = {"documents": [], "total": 0}
    added = {"status": "success", "document_id": "doc-1", "project_id": "project-1"}
    state = {
        "messages": [
            HumanMessage(
                content="Check project contents, add the paper, and verify the new contents"
            ),
            AIMessage(content="", tool_calls=[read]),
            ToolMessage(content=json.dumps(before), tool_call_id=read["id"]),
            AIMessage(content="", tool_calls=[write]),
            ToolMessage(content=json.dumps(added), tool_call_id=write["id"]),
            AIMessage(content="", tool_calls=[again]),
        ],
        "tool_executions": [
            {
                "id": read["id"],
                "tool_name": read["name"],
                "args": read["args"],
                "status": "completed",
                "result": before,
            },
            {
                "id": write["id"],
                "tool_name": write["name"],
                "args": write["args"],
                "status": "completed",
                "result": added,
            },
        ],
        "page_context": {},
        "retrieved_contexts": [],
        "tool_loop_count": 2,
    }
    executor = AsyncMock()
    with patch.object(_nodes_tools, "_execute_single_tool", executor):
        result = await _nodes_tools.tool_node(state, {"configurable": {}})
    assert executor.await_count == 0
    assert result["tool_executions"][-1]["result"] == before
    return {
        "fresh_reads": executor.await_count,
        "returned_result": result["tool_executions"][-1]["result"],
        "next_node": _builders.route_after_tool_node(result),
    }


async def receipt_crash_and_replay():
    call = tc(
        "create_project_note",
        {"title": "Audit fixture", "content": "Fixture"},
        "note-call",
    )
    config = {
        "configurable": {"thread_id": "fixture-thread", "user_id": "fixture-user"}
    }
    executor = AsyncMock(
        return_value={"status": "success", "note_id": "committed-note"}
    )
    # Simulate process cancellation after the side effect commits but before
    # the independent receipt transaction can be recorded.
    with (
        patch("src.services.agent.graph._get_execute_tool", return_value=executor),
        patch.object(
            _nodes_tools, "_tool_receipt_exists", AsyncMock(return_value=False)
        ),
        patch.object(
            _nodes_tools,
            "_record_tool_receipt",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
    ):
        try:
            await _nodes_tools._execute_single_tool(call, config, {})
        except asyncio.CancelledError:
            pass
    with (
        patch("src.services.agent.graph._get_execute_tool", return_value=executor),
        patch.object(
            _nodes_tools, "_tool_receipt_exists", AsyncMock(return_value=False)
        ),
        patch.object(_nodes_tools, "_record_tool_receipt", AsyncMock()),
    ):
        await _nodes_tools._execute_single_tool(call, config, {})
    assert executor.await_count == 2
    with patch.object(
        _nodes_tools, "_tool_receipt_exists", AsyncMock(return_value=True)
    ):
        replay = await _nodes_tools._execute_single_tool(call, config, {})
    assert "note_id" not in replay["execution"]["result"]
    return {
        "same_call_side_effect_executions": executor.await_count,
        "successful_receipt_replay_result": replay["execution"]["result"],
    }


async def main():
    for name, probe in [
        ("general_error_breaker", general_breaker),
        ("stale_read_after_write", stale_read_after_write),
        ("receipt_crash_and_replay", receipt_crash_and_replay),
    ]:
        print(name + ": " + json.dumps(await probe(), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
