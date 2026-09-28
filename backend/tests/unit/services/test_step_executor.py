"""Unit tests for StepExecutor deterministic behavior."""

import weakref
from unittest.mock import AsyncMock

import pytest

from src.services.research_engine.step_executor import StepExecutor, StepResult


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
