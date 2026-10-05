"""Unit tests for StepExecutor deterministic behavior."""

import asyncio
import weakref
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers.base import LLMProvider, LLMResponse
from src.services.research_engine.step_executor import (
    ExecutionBudget,
    StepExecutionError,
    StepExecutor,
    StepResult,
)


class TestStepExecutor:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("step_type", "handler_name"),
        [
            ("search", "_execute_search"),
            ("screen", "_execute_screen"),
            ("extract", "_execute_extract"),
            ("synthesize", "_execute_synthesize"),
            ("export", "_execute_export"),
            ("verify", "_execute_verify"),
        ],
    )
    async def test_legacy_step_routes_to_named_handler(
        self, monkeypatch, step_type, handler_name
    ):
        executor = StepExecutor(connectors={}, providers={})
        step_def = {"type": step_type, "parameters": {}}
        context = {"query": "controlled"}
        expected = StepResult(output={"route": step_type})
        handler = AsyncMock(return_value=expected)
        monkeypatch.setattr(executor, handler_name, handler)

        result = await executor.execute(step_def, context)

        assert result is expected
        handler.assert_awaited_once_with(step_def, context)

    def test_executor_is_reclaimed_without_cyclic_gc(self):
        executor = StepExecutor(connectors={}, providers={})
        executor_ref = weakref.ref(executor)

        del executor

        assert executor_ref() is None

    @pytest.mark.asyncio
    async def test_export_step_does_not_require_llm_provider(self):
        executor = StepExecutor(connectors={}, providers={})

        result = await executor.execute(
            {"type": "export", "parameters": {"format": "json", "fields": ["query"]}},
            {"query": "rag", "ignored": "value"},
        )

        assert result.output["format"] == "json"
        assert result.output["exported"] == {"query": "rag"}

    @pytest.mark.asyncio
    async def test_verify_step_emits_quality_mark(self):
        executor = StepExecutor(connectors={}, providers={})

        result = await executor.execute(
            {"type": "verify", "parameters": {}},
            {
                "claim": "Accuracy improved by 20%",
                "source_text": "Results show accuracy improved by 20% in evaluation.",
            },
        )

        assert result.output["verified"] is True
        assert len(result.quality_marks) == 1
        assert result.quality_marks[0].check_type == "source_grounding"
        assert result.quality_marks[0].passed is True


@pytest.fixture
def legacy_provider():
    provider = AsyncMock(spec=LLMProvider)
    provider.complete.return_value = LLMResponse(
        content="controlled result",
        model_id="test-model",
        input_tokens=100,
        output_tokens=50,
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("step_type", ["screen", "extract", "synthesize"])
async def test_legacy_stage_denies_call_that_cannot_fit_budget(
    step_type, legacy_provider
):
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget(used_tokens=49_999)

    with pytest.raises(StepExecutionError):
        await executor.execute(
            {"type": step_type, "model_id": "test-model"},
            {"query": "controlled"},
            budget=budget,
        )

    legacy_provider.complete.assert_not_awaited()
    assert budget.used_tokens == 49_999
    assert budget.used_calls == 0
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_legacy_stage_reconciles_usage_and_preserves_result(legacy_provider):
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget(used_tokens=1000)

    result = await executor.execute(
        {"type": "screen", "model_id": "test-model"},
        {"query": "controlled"},
        budget=budget,
    )

    assert result.output == {"content": "controlled result"}
    assert result.token_count == 150
    assert result.inputs_hash and result.outputs_hash
    assert budget.used_tokens == 1150
    assert budget.used_calls == 1
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_legacy_stages_share_call_limit(legacy_provider):
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget(max_total_calls=1)
    await executor.execute(
        {"type": "screen", "model_id": "test-model"}, {}, budget=budget
    )
    budget.active_step_index = 1

    with pytest.raises(StepExecutionError):
        await executor.execute(
            {"type": "synthesize", "model_id": "test-model"},
            {},
            budget=budget,
        )

    legacy_provider.complete.assert_awaited_once()
    assert budget.used_tokens == 150
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_legacy_failed_call_releases_tokens_but_counts_attempt(
    legacy_provider,
):
    legacy_provider.complete.side_effect = RuntimeError("provider unavailable")
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget()

    with pytest.raises(StepExecutionError) as exc:
        await executor.execute(
            {"type": "extract", "model_id": "test-model"}, {}, budget=budget
        )

    assert exc.value.consumed_tokens == 0
    assert exc.value.model_calls == 1
    assert budget.used_calls == 1
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("step_type", ["screen", "extract", "synthesize"])
async def test_legacy_provider_call_propagates_task_cancellation(
    step_type, legacy_provider
):
    started = asyncio.Event()

    async def complete(_request):
        started.set()
        await asyncio.Event().wait()

    legacy_provider.complete.side_effect = complete
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget()
    task = asyncio.create_task(
        executor.execute(
            {"type": step_type, "model_id": "test-model"}, {}, budget=budget
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert budget.used_calls == 1
    assert budget.used_tokens == 0
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("step_type", ["screen", "extract", "synthesize"])
async def test_legacy_workflow_reports_wall_time_expiry(step_type, legacy_provider):
    async def complete(_request):
        await asyncio.Event().wait()

    legacy_provider.complete.side_effect = complete
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    engine = WorkflowEngine(executor, max_wall_time_seconds=0.05)
    events = [
        event
        async for event in engine.run(
            {"steps": [{"type": step_type, "model_id": "test-model"}]}, uuid4()
        )
    ]

    assert events[-1]["event"] == "run_failed"
    assert "wall-time budget" in events[-1]["error"]
    assert all(event["event"] != "step_complete" for event in events)
    legacy_provider.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_provider_overspend_is_accounted_on_error(legacy_provider):
    legacy_provider.complete.return_value = LLMResponse(
        content="oversized usage",
        model_id="test-model",
        input_tokens=3000,
        output_tokens=50,
    )
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    budget = ExecutionBudget(max_total_tokens=3000)

    with pytest.raises(StepExecutionError) as exc:
        await executor.execute(
            {"type": "extract", "model_id": "test-model"}, {}, budget=budget
        )

    assert exc.value.consumed_tokens == 3050
    assert exc.value.model_calls == 1
    assert budget.used_tokens == 3050
    assert budget.reserved_tokens == 0


@pytest.mark.asyncio
async def test_resumed_legacy_workflow_rejects_paid_call_before_spending(
    legacy_provider,
):
    executor = StepExecutor(connectors={}, providers={"test-model": legacy_provider})
    engine = WorkflowEngine(executor)
    events = [
        event
        async for event in engine.run(
            {"steps": [{"type": "synthesize", "model_id": "test-model"}]},
            uuid4(),
            initial_context={"query": "controlled"},
            initial_total_tokens=49_999,
        )
    ]

    assert events[-1]["event"] == "run_failed"
    assert all(event["event"] != "step_complete" for event in events)
    legacy_provider.complete.assert_not_awaited()
