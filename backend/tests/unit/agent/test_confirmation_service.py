"""Approval receipts are bound to a saved action, not a waiting thread.

Mutation proof: see docs/engineering/agent-approvals.md for the removed guards
and exact regression commands. The PostgreSQL suite uses this real graph.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.services.agent.confirmation_service import (
    ApprovalExpired,
    PendingApproval,
    pending_approval,
    require_approval,
)

pytestmark = pytest.mark.unit


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        values={"user_id": "user"},
        config={"configurable": {"checkpoint_id": "saved", "checkpoint_ns": ""}},
        tasks=(
            SimpleNamespace(
                interrupts=(
                    SimpleNamespace(
                        id="interrupt",
                        value={"tools": [{"name": "delete", "args": {"id": "a"}}]},
                    ),
                )
            ),
        ),
    )


def _approval(snapshot: Any, **changes: Any) -> PendingApproval:
    approval = pending_approval(
        snapshot,
        **{"thread_id": "thread", "run_id": "run", "user_id": "user", **changes},
    )

    assert approval is not None
    return approval


def test_receipt_is_stable_across_checkpoint_reloads() -> None:
    snapshot = _snapshot()
    first = _approval(snapshot)
    replay = _approval(deepcopy(snapshot))
    assert first == replay
    assert len(first.approval_id) == 64
    assert first.confirmation()["approval_id"] == first.approval_id
    assert first.resume(False) == {"interrupt": {"confirmed": False}}


@pytest.mark.parametrize("change", ["run", "thread", "checkpoint", "interrupt", "args"])
def test_any_change_to_the_authorized_action_rotates_receipt(change: str) -> None:
    snapshot = _snapshot()
    first = _approval(snapshot)
    context = {}
    if change in {"run", "thread"}:
        context[f"{change}_id"] = "replacement"
    elif change == "checkpoint":
        snapshot.config["configurable"]["checkpoint_id"] = "replacement"
    elif change == "interrupt":
        snapshot.tasks[0].interrupts[0].id = "replacement"
    else:
        snapshot.tasks[0].interrupts[0].value["tools"][0]["args"]["id"] = "b"
    assert _approval(snapshot, **context).approval_id != first.approval_id


@pytest.mark.parametrize("missing", ["owner", "checkpoint", "interrupt"])
def test_legacy_or_unowned_checkpoints_cannot_issue_receipts(missing: str) -> None:
    snapshot = _snapshot()
    if missing == "owner":
        snapshot.values = {"user_id": "someone-else"}
    elif missing == "checkpoint":
        snapshot.config = {}
    else:
        snapshot.tasks[0].interrupts[0].id = "placeholder-id"
    with pytest.raises(ApprovalExpired):
        _approval(snapshot)


class ApprovalState(TypedDict):
    user_id: str
    first: bool
    second: bool


def approval_graph(checkpointer: Any) -> Any:
    """Two real saved interrupts, including two gates within the same node."""

    def actions(state: ApprovalState) -> dict[str, bool]:
        first = interrupt({"tools": [{"name": "first", "args": {"id": "a"}}]})
        second = interrupt({"tools": [{"name": "second", "args": {"id": "b"}}]})
        return {"first": first["confirmed"], "second": second["confirmed"]}

    builder = StateGraph(ApprovalState)
    builder.add_node("actions", actions)
    builder.add_edge(START, "actions")
    builder.add_edge("actions", END)
    return builder.compile(checkpointer=checkpointer)


async def test_old_receipt_cannot_approve_next_interrupt_in_same_node() -> None:
    graph = approval_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke({"user_id": "user"}, config)
    first = _approval(await graph.aget_state(config))
    await graph.ainvoke(Command(resume=first.resume(True)), config)
    saved_second = await graph.aget_state(config)
    second = _approval(saved_second)
    # LangGraph reuses the task interrupt id for sequential calls inside one
    # node. The saved checkpoint AND action digest must therefore bind it.
    assert first.interrupt_id == second.interrupt_id
    assert first.approval_id != second.approval_id
    with pytest.raises(ApprovalExpired):
        require_approval(
            saved_second,
            approval_id=first.approval_id,
            thread_id="thread",
            run_id="run",
            user_id="user",
        )
    valid = require_approval(
        saved_second,
        approval_id=second.approval_id,
        thread_id="thread",
        run_id="run",
        user_id="user",
    )
    result = await graph.ainvoke(Command(resume=valid.resume(False)), config)
    assert result["first"] is True
    assert result["second"] is False
    with pytest.raises(ApprovalExpired):
        require_approval(
            await graph.aget_state(config),
            approval_id=second.approval_id,
            thread_id="thread",
            run_id="run",
            user_id="user",
        )
