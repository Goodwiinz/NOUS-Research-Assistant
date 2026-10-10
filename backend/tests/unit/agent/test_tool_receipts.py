"""The old unscoped receipts remain historical data, not replay authority."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.services.agent._nodes_tools import SIDE_EFFECT_TOOLS, _execute_single_tool
from src.services.agent.tool_registry import ToolEffectMode, ToolPolicyTag
from src.services.agent.tools import TOOL_REGISTRY

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _run(
    *,
    executor: AsyncMock,
    operation_context: dict[str, Any] | None,
    tool_name: str = "create_project",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_id = str(uuid.uuid4())
    organization_id = str(uuid.uuid4())
    call = {
        "name": tool_name,
        "args": arguments or {"name": "A scoped project"},
        "id": "provider-call-1",
    }
    config = {
        "configurable": {
            "user_id": user_id,
            "organization_id": organization_id,
            "thread_id": "thread-1",
        }
    }
    with patch("src.services.agent.graph._get_execute_tool", return_value=executor):
        return await _execute_single_tool(call, config, {}, operation_context)


async def test_mutation_inventory_is_complete_and_registry_owned() -> None:
    expected = {
        "create_project",
        "create_project_note",
        "add_document_to_project",
        "create_draft",
        "revise_draft",
        "ingest_arxiv_papers",
        "execute_code",
        "forget_memory",
    }
    actual = {
        descriptor.name
        for descriptor in TOOL_REGISTRY.descriptors
        if descriptor.effect_mode is not ToolEffectMode.READ_ONLY
    }

    assert actual == expected
    assert SIDE_EFFECT_TOOLS == expected
    for descriptor in TOOL_REGISTRY.descriptors:
        if descriptor.effect_mode is ToolEffectMode.READ_ONLY:
            continue
        assert ToolPolicyTag.DESTRUCTIVE in descriptor.policy_tags
        assert ToolPolicyTag.NO_OUTER_RETRY in descriptor.policy_tags
        assert ToolPolicyTag.CONTEXT_FREE not in descriptor.policy_tags


async def test_unanchored_mutation_fails_closed_without_using_legacy_receipts() -> None:
    executor = AsyncMock(return_value={"status": "success"})

    result = await _run(executor=executor, operation_context=None)

    executor.assert_not_awaited()
    assert result["execution"]["status"] == "failed"
    payload = json.loads(result["message"].content)
    assert payload["error_category"] == "legacy_operation_result_unavailable"
    assert payload["restart_with_new_turn"] is True
    assert result["execution"]["retry_exhausted"] is True


async def test_anchored_mutation_passes_scoped_identity_and_effective_arguments() -> (
    None
):
    executor = AsyncMock(
        return_value={"status": "success", "project_id": str(uuid.uuid4())}
    )

    result = await _run(
        executor=executor,
        operation_context={
            "tool_operation_protocol_version": 1,
            "tool_operation_turn_id": "human-message-1",
        },
    )

    executor.assert_awaited_once()
    operation_key = executor.await_args.kwargs["operation_key"]
    assert operation_key.thread_id == "thread-1"
    assert operation_key.turn_id == "human-message-1"
    assert operation_key.tool_call_id == "provider-call-1"
    assert operation_key.tool_name == "create_project"
    assert len(operation_key.args_hash) == 64
    assert result["execution"]["status"] == "completed"


async def test_read_tool_does_not_require_operation_anchor_or_legacy_receipt() -> None:
    executor = AsyncMock(return_value={"results": []})

    result = await _run(
        executor=executor,
        operation_context=None,
        tool_name="search_arxiv",
        arguments={"query": "tenant scoped tools"},
    )

    executor.assert_awaited_once()
    assert "operation_key" not in executor.await_args.kwargs
    assert result["execution"]["status"] == "completed"
