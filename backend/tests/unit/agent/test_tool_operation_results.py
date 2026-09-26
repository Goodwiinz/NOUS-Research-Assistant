"""Behavioral regressions for tool-operation replay and read freshness."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent.state import AgentState

pytestmark = [pytest.mark.unit]


def test_result_cap_preserves_bounded_identity_subset_and_is_idempotent() -> None:
    from src.services.agent.tools_impl import (
        _MAX_RESULT_IDENTITY_BYTES,
        _MAX_TOOL_RESULT_BYTES,
        _cap_tool_result,
    )

    ids = {name: str(uuid.uuid4()) for name in ("project", "document", "note", "draft")}
    rows = [
        {
            "id": str(uuid.uuid4()),
            "title": f"Paper {index}",
            "status": "indexed",
            "project_id": ids["project"],
        }
        for index in range(80)
    ]
    result: dict[str, Any] = {
        "status": "completed",
        "project_id": ids["project"],
        "document_id": ids["document"],
        "note_id": ids["note"],
        "draft_id": ids["draft"],
        "task_id": "task_generated_17",
        "message": "研究成果 📚 " * 8000,
        "documents": rows,
    }

    bounded = _cap_tool_result(result)
    encoded = json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8")
    envelope = bounded["_tool_result_bounds"]
    entries = envelope["identity_entries"]
    coverage = envelope["identity_coverage"]

    assert len(encoded) <= _MAX_TOOL_RESULT_BYTES
    assert (
        len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        <= _MAX_RESULT_IDENTITY_BYTES
    )
    assert {f"{name}_id": bounded[f"{name}_id"] for name in ids} == {
        f"{name}_id": value for name, value in ids.items()
    }
    assert bounded["task_id"] == "task_generated_17"
    assert bounded["status"] == "completed"
    assert len(entries) <= 32
    assert any(
        entry["path"] == "/documents/0" and entry.get("label") == "Paper 0"
        for entry in entries
    )
    # Associations survive together; the projection never zips separate arrays.
    assert any(
        entry.get("label") == "Paper 12"
        and entry.get("status") == "indexed"
        and entry.get("related", {}).get("project_id") == ids["project"]
        for entry in entries
    )
    assert coverage["incomplete"] is True
    assert coverage["omitted_entries"] > 0
    assert bounded["result_complete"] is False
    assert "do not repeat" in bounded["recovery_guidance"].lower()

    repeated = _cap_tool_result(bounded)
    assert repeated == bounded
    assert repeated["_tool_result_bounds"]["identity_entries"] == entries


def test_result_cap_retains_external_connector_namespace_for_duplicate_ids() -> None:
    from src.services.agent.tools_impl import _cap_tool_result

    bounded = _cap_tool_result(
        {
            "results": [
                {"id": "shared-accession-7", "title": "SEC", "source": "sec_edgar"},
                {"id": "shared-accession-7", "title": "UniProt", "source": "uniprot"},
            ],
            "connectors_searched": ["sec_edgar", "uniprot"],
            "message": "large ordinary result " * 3000,
        }
    )

    entries = bounded["_tool_result_bounds"]["identity_entries"]
    external = {
        (entry["kind"], entry["namespace"], entry["id"], entry["label"])
        for entry in entries
        if entry["kind"] == "external"
    }
    assert external == {
        ("external", "sec_edgar", "shared-accession-7", "SEC"),
        ("external", "uniprot", "shared-accession-7", "UniProt"),
    }
    assert _cap_tool_result(bounded) == bounded


def test_result_cap_retains_pending_and_error_classification_with_large_collections() -> (
    None
):
    from src.services.agent.tools_impl import _cap_tool_result

    result = {
        "status": "pending",
        "task_id": "arbitrary-task-id",
        "error": "remote task is pending",
        "error_category": "external_task_pending",
        "paper_ids": [f"2401.{index:05d}v1" for index in range(10005)],
        "output": "λ" * 25000,
    }

    bounded = _cap_tool_result(result)
    encoded = json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8")
    coverage = bounded["_tool_result_bounds"]["identity_coverage"]

    assert len(encoded) <= 32 * 1024
    assert bounded["task_id"] == "arbitrary-task-id"
    assert bounded["status"] == "pending"
    assert bounded["error_category"] == "external_task_pending"
    assert bounded["error"] == "remote task is pending"
    paper_entry = next(
        entry
        for entry in bounded["_tool_result_bounds"]["identity_entries"]
        if entry["kind"] == "paper"
    )
    assert paper_entry["id"] == "2401.00000v1"
    assert coverage["incomplete"] is True
    assert coverage["unseen_count_known"] is True
    assert coverage["observed_entries"] == 10006
    assert (
        coverage["retained_entries"] + coverage["omitted_entries"]
        == coverage["observed_entries"]
    )


def test_terminal_draft_recovery_preserves_failure_category_and_no_retry_guidance() -> (
    None
):
    from src.services.agent.tools_impl import _draft_status_result

    dispatched = {
        "task_id": "draft-task-non-uuid-17",
        "project_id": str(uuid.uuid4()),
        "project_name": "Rejected draft",
        "user_id": str(uuid.uuid4()),
        "status": "pending",
    }
    failed = _draft_status_result(
        dispatched,
        {
            "task_id": dispatched["task_id"],
            "status": "failed",
            "current_step": "Error: document source was rejected",
            "error_category": "draft_source_rejected",
            "error_type": "user_fixable",
        },
    )
    assert failed["status"] == "failed"
    assert failed["error"] == "document source was rejected"
    assert failed["error_category"] == "draft_source_rejected"
    assert failed["error_type"] == "user_fixable"
    assert failed["automatic_retry_allowed"] is False
    assert "do not repeat" in failed["retry_guidance"].lower()

    cancelled = _draft_status_result(
        dispatched,
        {
            "task_id": dispatched["task_id"],
            "status": "cancelled",
            "current_step": "Draft generation cancelled",
        },
    )
    assert cancelled["error_category"] == "draft_generation_cancelled"
    assert cancelled["error_type"] == "fatal"
    assert cancelled["automatic_retry_allowed"] is False


async def test_preprocessing_checkpoints_a_stable_tool_operation_turn_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_classify

    async def _empty(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(_nodes_classify, "rag_node", _empty)
    monkeypatch.setattr(_nodes_classify, "_classify_core", _empty)
    monkeypatch.setattr(_nodes_classify, "memory_retrieval_node", _empty)
    monkeypatch.setattr(_nodes_classify, "tag_trace_intent", lambda _intent: None)

    human = HumanMessage(content="Create a project", id="client-turn-17")
    state = cast(AgentState, {"messages": [human], "turn_index": 2})

    result = await _nodes_classify.preprocessing_node(state, {})

    assert result["tool_operation_protocol_version"] == 1
    assert result["tool_operation_turn_id"] == "client-turn-17"


async def test_preprocessing_assigns_and_returns_missing_turn_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_classify

    async def _empty(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(_nodes_classify, "rag_node", _empty)
    monkeypatch.setattr(_nodes_classify, "_classify_core", _empty)
    monkeypatch.setattr(_nodes_classify, "memory_retrieval_node", _empty)
    monkeypatch.setattr(_nodes_classify, "tag_trace_intent", lambda _intent: None)

    state = cast(
        AgentState,
        {"messages": [HumanMessage(content="Create a project")], "turn_index": 0},
    )

    result = await _nodes_classify.preprocessing_node(state, {})

    assert result["tool_operation_protocol_version"] == 1
    anchor = result["tool_operation_turn_id"]
    assert isinstance(anchor, str) and anchor
    returned_humans = [
        message for message in result["messages"] if isinstance(message, HumanMessage)
    ]
    assert returned_humans[-1].id == anchor


async def test_reads_invalidate_prior_cache_after_an_attempted_mutation() -> None:
    from src.services.agent.tool_dedupe import find_cached_tool_results

    messages = [
        HumanMessage(content="Add a paper", id="turn-1"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "list-before",
                    "name": "list_project_documents",
                    "args": {"project_id": "project-1"},
                }
            ],
        ),
        ToolMessage(content='{"documents":[]}', tool_call_id="list-before"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "attach-attempt",
                    "name": "add_document_to_project",
                    "args": {
                        "project_id": "project-1",
                        "document_id": "document-1",
                    },
                }
            ],
        ),
        ToolMessage(
            content='{"error":"link result uncertain"}',
            tool_call_id="attach-attempt",
            status="error",
        ),
    ]
    executions = [
        {
            "id": "list-before",
            "tool_name": "list_project_documents",
            "args": {"project_id": "project-1"},
            "status": "completed",
            "result": {"documents": []},
        },
        {
            "id": "attach-attempt",
            "tool_name": "add_document_to_project",
            "args": {
                "project_id": "project-1",
                "document_id": "document-1",
            },
            "status": "failed",
            "result": {"error": "link result uncertain"},
        },
    ]

    cached = find_cached_tool_results(
        [
            {
                "id": "list-after",
                "name": "list_project_documents",
                "args": {"project_id": "project-1"},
            }
        ],
        messages,
        executions,
    )

    assert cached == {}


async def test_mixed_batch_verification_read_runs_after_mutation_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools

    mutation_finished = asyncio.Event()
    order: list[str] = []

    async def _execute(tc: dict[str, Any], *_args: Any) -> dict[str, Any]:
        if tc["name"] == "add_document_to_project":
            order.append("mutation")
            await asyncio.sleep(0.01)
            mutation_finished.set()
            result: dict[str, Any] = {
                "status": "success",
                "document_id": "document-1",
            }
        else:
            order.append("read")
            result = {"documents": ["document-1"] if mutation_finished.is_set() else []}
        return {
            "message": ToolMessage(content=json.dumps(result), tool_call_id=tc["id"]),
            "execution": {
                "id": tc["id"],
                "tool_name": tc["name"],
                "args": tc["args"],
                "status": "completed",
                "result": result,
            },
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }

    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", _execute)
    state = cast(
        AgentState,
        {
            "messages": [
                HumanMessage(content="Add and verify", id="turn-2"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "attach-1",
                            "name": "add_document_to_project",
                            "args": {
                                "project_id": "project-1",
                                "document_id": "document-1",
                            },
                        },
                        {
                            "id": "list-1",
                            "name": "list_project_documents",
                            "args": {"project_id": "project-1"},
                        },
                    ],
                ),
            ],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
        },
    )

    result = await _nodes_tools.tool_node(state, {})

    assert order == ["mutation", "read"]
    assert json.loads(result["messages"][1].content) == {"documents": ["document-1"]}


async def test_filtered_mixed_batch_preserves_all_emitted_tool_result_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.agent import _nodes_tools

    executed: list[str] = []

    async def _execute(tc: dict[str, Any], *_args: Any) -> dict[str, Any]:
        executed.append(tc["id"])
        result = {"status": "success", "name": tc["name"]}
        return {
            "message": ToolMessage(
                content=json.dumps(result),
                tool_call_id=tc["id"],
                status="success",
            ),
            "execution": {
                "id": tc["id"],
                "tool_name": tc["name"],
                "args": tc["args"],
                "status": "completed",
                "result": result,
            },
            "error_increment": 0,
            "error_text": "",
            "error_info": {},
        }

    monkeypatch.setattr(_nodes_tools, "_execute_single_tool", _execute)
    calls = [
        {"id": "read-1", "name": "list_projects", "args": {}},
        {"id": "skipped", "name": "search_arxiv", "args": {"query": "x"}},
        {
            "id": "write",
            "name": "create_project_note",
            "args": {"title": "t", "content": "c"},
        },
        {
            "id": "read-2",
            "name": "list_project_documents",
            "args": {"project_id": "project-1"},
        },
    ]
    state = cast(
        AgentState,
        {
            "messages": [AIMessage(content="", tool_calls=calls)],
            "tool_executions": [],
            "error_count": 0,
            "last_error": "",
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "turn-1",
        },
    )

    result = await _nodes_tools.make_filtered_tool_node(
        {"list_projects", "create_project_note", "list_project_documents"}
    )(state, {})

    assert executed == []
    assert [message.tool_call_id for message in result["messages"]] == [
        "read-1",
        "skipped",
        "write",
        "read-2",
    ]
    assert all(message.status == "error" for message in result["messages"])
    assert result["capability_limitation"]["unavailable_tools"] == ["search_arxiv"]


async def test_injected_mutation_without_operation_scope_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.models.user import User
    from src.services.agent import tools_impl

    dispatch = AsyncMock()
    monkeypatch.setattr(tools_impl, "_dispatch_tool", dispatch)
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    result = await tools_impl.execute_tool(
        "create_project",
        {"name": "A project"},
        user_id=str(user_id),
        organization_id=str(organization_id),
        thread_id="thread-1",
        db=cast(Any, object()),
        current_user=cast(
            User,
            SimpleNamespace(id=user_id, organization_id=organization_id),
        ),
    )

    dispatch.assert_not_awaited()
    assert result["error_category"] == "operation_context_invalid"
    assert result["automatic_retry_allowed"] is False
