"""Accepted ancestry replaces sibling messages in real LangGraph checkpoints."""

from __future__ import annotations

from typing import Annotated, Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from src.models.chat_message import ChatMessage, MessageRole
from src.services.agent import branch_history


@pytest.fixture(autouse=True)
def no_attached_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module isolates checkpoint replacement; real file rows are exercised
    # by test_alphaxiv_files.py.
    monkeypatch.setattr(
        branch_history, "load_branch_file_contexts", AsyncMock(return_value={})
    )


class _CheckpointState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _graph() -> Any:
    graph = StateGraph(_CheckpointState)
    graph.add_node("noop", lambda _state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    return graph.compile(checkpointer=MemorySaver())


def _row(role: MessageRole, content: str) -> ChatMessage:
    return ChatMessage(id=uuid4(), role=role, content=content)


def _messages(rows: list[ChatMessage]) -> list[BaseMessage]:
    return [
        (
            HumanMessage(content=row.content, id=str(row.id))
            if row.role == MessageRole.USER
            else AIMessage(content=row.content, id=str(row.id))
        )
        for row in rows
    ]


@pytest.mark.parametrize("branch", ["root", "edit", "follow_up"])
async def test_accepted_branch_replaces_contaminated_checkpoint(
    monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    """A root/edit/follow-up must not retain superseded or sibling context.

    Mutation proof: remove the initial RemoveMessage in branch_history.py,
    then run this test's [root] case. It fails with seven retained sibling,
    summary and tool messages. The production helper was restored afterwards.
    """
    thread_id = uuid4()
    common = [
        _row(MessageRole.USER, "common question"),
        _row(MessageRole.ASSISTANT, "common answer"),
    ]
    sibling = [
        _row(MessageRole.USER, "other branch private question"),
        _row(MessageRole.ASSISTANT, "other branch private answer"),
    ]
    selected = [
        _row(MessageRole.USER, "selected branch question"),
        _row(MessageRole.ASSISTANT, "selected branch answer"),
    ]
    accepted = _row(MessageRole.USER, f"accepted {branch} prompt")
    rows = {
        "root": [accepted],
        "edit": [*common, accepted],
        "follow_up": [*common, *selected, accepted],
    }[branch]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(branch_history, "get_branch_path", lookup)
    db = cast(AsyncSession, object())
    graph = _graph()
    config = {"configurable": {"thread_id": str(thread_id)}}
    # Include old provider/tool messages, not just text turns. The reducer
    # must discard the complete old checkpoint before inserting this path.
    await graph.ainvoke(
        {
            "messages": [
                SystemMessage(content="sibling-only summary", id="old-summary"),
                *_messages(common + sibling),
                AIMessage(
                    content="",
                    id="old-tool-call",
                    tool_calls=[{"name": "search", "args": {}, "id": "old-call"}],
                ),
                ToolMessage(
                    content="sibling-only result",
                    tool_call_id="old-call",
                    id="old-tool-result",
                ),
            ]
        },
        config,
    )

    rebuilt = await branch_history.build_branch_input_messages(
        db,
        thread_id=str(thread_id),
        actor_id=uuid4(),
        organization_id=uuid4(),
        accepted_user_message_id=str(accepted.id),
    )
    lookup.assert_awaited_once_with(
        db, thread_id=thread_id, head_message_id=accepted.id
    )
    await graph.ainvoke({"messages": rebuilt}, config)
    persisted = (await graph.aget_state(config)).values["messages"]

    assert [message.content for message in persisted] == [row.content for row in rows]
    assert [message.id for message in persisted] == [str(row.id) for row in rows]
    assert all(isinstance(message, (HumanMessage, AIMessage)) for message in persisted)
    assert isinstance(persisted[-1], HumanMessage)


async def test_accepted_path_is_bounded_without_reintroducing_checkpoint_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(
            MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
            f"turn {index}",
        )
        for index in range(45)
    ]
    lookup = AsyncMock(return_value=rows)
    monkeypatch.setattr(branch_history, "get_branch_path", lookup)
    graph = _graph()
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph.ainvoke({"messages": _messages(rows[:5])}, config)

    rebuilt = await branch_history.build_branch_input_messages(
        cast(AsyncSession, object()),
        thread_id=config["configurable"]["thread_id"],
        actor_id=uuid4(),
        organization_id=uuid4(),
        accepted_user_message_id=str(rows[-1].id),
    )
    await graph.ainvoke({"messages": rebuilt}, config)
    persisted = (await graph.aget_state(config)).values["messages"]
    assert len(persisted) == 40
    assert [message.id for message in persisted] == [str(row.id) for row in rows[-40:]]
    assert persisted[-1].content == "turn 44"


@pytest.mark.parametrize(
    "invalid_path",
    ["empty", "wrong_tail", "assistant_tail", "system_ancestor", "tool_ancestor"],
)
async def test_invalid_ancestry_fails_closed(
    monkeypatch: pytest.MonkeyPatch, invalid_path: str
) -> None:
    accepted = _row(MessageRole.USER, "accepted prompt")
    rows = [accepted]
    expected = "The accepted message branch is unavailable"
    if invalid_path == "empty":
        rows = []
    elif invalid_path == "wrong_tail":
        rows = [_row(MessageRole.USER, "mutable selected head, not accepted prompt")]
    elif invalid_path == "assistant_tail":
        accepted.role = MessageRole.ASSISTANT
    else:
        role = (
            MessageRole.SYSTEM
            if invalid_path == "system_ancestor"
            else MessageRole.TOOL
        )
        rows.insert(0, _row(role, "must not enter model history"))
        expected = "The accepted message branch contains an invalid role"
    monkeypatch.setattr(branch_history, "get_branch_path", AsyncMock(return_value=rows))

    with pytest.raises(ValueError, match=expected):
        await branch_history.build_branch_input_messages(
            cast(AsyncSession, object()),
            thread_id=str(uuid4()),
            actor_id=uuid4(),
            organization_id=uuid4(),
            accepted_user_message_id=str(accepted.id),
        )


async def test_branch_retrieval_error_propagates_without_history_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("branch lookup unavailable")
    lookup = AsyncMock(side_effect=failure)
    monkeypatch.setattr(branch_history, "get_branch_path", lookup)

    with pytest.raises(RuntimeError) as caught:
        await branch_history.build_branch_input_messages(
            cast(AsyncSession, object()),
            thread_id=str(uuid4()),
            actor_id=uuid4(),
            organization_id=uuid4(),
            accepted_user_message_id=str(uuid4()),
        )
    assert caught.value is failure
    lookup.assert_awaited_once()


@pytest.mark.parametrize("invalid_identity", ["thread_id", "accepted_user_message_id"])
async def test_invalid_accepted_identity_fails_before_query(
    monkeypatch: pytest.MonkeyPatch, invalid_identity: str
) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(branch_history, "get_branch_path", lookup)
    identities = {
        "thread_id": str(UUID(int=1)),
        "accepted_user_message_id": str(UUID(int=2)),
    }
    identities[invalid_identity] = "not-a-uuid"

    with pytest.raises(ValueError):
        await branch_history.build_branch_input_messages(
            cast(AsyncSession, object()),
            actor_id=uuid4(),
            organization_id=uuid4(),
            **identities,
        )
    lookup.assert_not_awaited()
