"""Tests for the workflow engine, step executor, and verification service."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.observability import ResearchObservability
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)
from src.services.research_engine.step_executor import StepExecutor, StepResult
from src.services.research_engine.verification import (
    QualityMark,
    run_source_grounding_check,
)

# ---------------------------------------------------------------------------
# TestStepResult
# ---------------------------------------------------------------------------


class TestStepResult:
    """Tests for StepResult dataclass."""

    def test_creation(self):
        result = StepResult(output={"summary": "hello"})
        assert result.output == {"summary": "hello"}
        assert result.sources_used == []
        assert result.quality_marks == []
        assert result.token_count == 0
        assert result.inputs_hash is None
        assert result.outputs_hash is None
        assert result.full_prompt is None

    def test_quality_marks(self):
        qm = QualityMark(check_type="source_grounding", passed=True, details="ok")
        result = StepResult(output={"x": 1}, quality_marks=[qm])
        assert len(result.quality_marks) == 1
        assert result.quality_marks[0].passed is True
        assert result.quality_marks[0].check_type == "source_grounding"


# ---------------------------------------------------------------------------
# TestVerificationService
# ---------------------------------------------------------------------------


class TestVerificationService:
    """Tests for run_source_grounding_check."""

    def test_source_grounding_pass_numbers(self):
        claim = "The accuracy was 95.5% in the experiment"
        source = "Results showed an accuracy of 95.5% across all trials"
        mark = run_source_grounding_check(claim, source)
        assert mark.check_type == "source_grounding"
        assert mark.passed is True

    def test_source_grounding_fail_numbers(self):
        claim = "The accuracy was 99.9% in the experiment"
        source = "Results showed an accuracy of 72.3% across all trials"
        mark = run_source_grounding_check(claim, source)
        assert mark.check_type == "source_grounding"
        assert mark.passed is False

    def test_source_grounding_no_numbers_pass(self):
        claim = "The model performed well on the benchmark dataset"
        source = "The model performed exceptionally well on the benchmark dataset used in evaluation"
        mark = run_source_grounding_check(claim, source)
        assert mark.passed is True

    def test_source_grounding_no_numbers_fail(self):
        claim = "quantum computing revolutionizes cryptography entirely"
        source = "The weather today is sunny and warm in California"
        mark = run_source_grounding_check(claim, source)
        assert mark.passed is False


# ---------------------------------------------------------------------------
# TestStepExecutor
# ---------------------------------------------------------------------------


class TestStepExecutor:
    """Tests for StepExecutor."""

    @pytest.mark.asyncio
    async def test_execute_search_step(self):
        mock_connector = AsyncMock(spec=SourceConnector)
        mock_connector.search.return_value = [
            SourceDocument(
                connector_type="arxiv",
                title="Test Paper",
                abstract="Abstract text",
                external_id="123",
            )
        ]

        executor = StepExecutor(
            connectors={"arxiv": mock_connector},
            providers={},
        )

        step_def = {
            "type": "search",
            "params": {"sources": ["arxiv"], "query_template": "{query}"},
        }
        context = {"query": "machine learning"}
        result = await executor.execute(step_def, context)

        assert len(result.sources_used) == 1
        assert result.sources_used[0].title == "Test Paper"
        assert "sources" in result.output
        mock_connector.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_synthesize_step(self):
        mock_provider = AsyncMock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Synthesized output text",
            model_id="test-model",
            input_tokens=100,
            output_tokens=50,
        )

        executor = StepExecutor(
            connectors={},
            providers={"test-model": mock_provider},
        )

        step_def = {
            "type": "synthesize",
            "params": {
                "model_id": "test-model",
                "system_prompt_template": "Synthesize: {query}",
                "temperature": 0.3,
                "seed": 42,
            },
        }
        context = {"query": "summarize findings"}
        result = await executor.execute(step_def, context)

        assert "content" in result.output
        assert result.output["content"] == "Synthesized output text"
        assert result.token_count == 150
        assert result.inputs_hash is not None
        assert result.outputs_hash is not None
        mock_provider.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_rejects_connector_fanout_before_external_calls(self):
        connectors = {
            name: AsyncMock(spec=SourceConnector) for name in ("a", "b", "c", "d", "e")
        }
        executor = StepExecutor(connectors=connectors, providers={})

        with pytest.raises(ValueError, match="connector"):
            await executor.execute(
                {"type": "search", "params": {"sources": list(connectors)}},
                {},
            )

        for connector in connectors.values():
            connector.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_rejects_oversized_result_request_before_external_calls(self):
        connector = AsyncMock(spec=SourceConnector)
        executor = StepExecutor(connectors={"arxiv": connector}, providers={})

        with pytest.raises(ValueError, match="results"):
            await executor.execute(
                {"type": "search", "params": {"sources": ["arxiv"], "max_results": 51}},
                {},
            )

        connector.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_rejects_oversized_rendered_input_before_external_calls(self):
        provider = AsyncMock(spec=LLMProvider)
        executor = StepExecutor(connectors={}, providers={"test-model": provider})

        with pytest.raises(ValueError, match="prompt"):
            await executor.execute(
                {"type": "synthesize", "params": {"model_id": "test-model"}},
                {"payload": "x" * 20_000},
            )

        provider.complete.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestWorkflowEngine
# ---------------------------------------------------------------------------


class TestWorkflowEngine:
    """Tests for WorkflowEngine."""

    @pytest.mark.asyncio
    async def test_production_engine_records_safe_run_stage_provider_dedupe_and_verify_metrics(
        self,
    ) -> None:
        observer = ResearchObservability()
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.side_effect = [
            StepResult(
                output={
                    "contract_version": 1,
                    "stage_type": "search",
                    "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                    "source_records": [],
                    "coverage": {
                        "providers": {
                            "openalex": {"status": "ok", "returned": 3, "limit": 5}
                        },
                        "deduplication": {"before": 3, "after": 2},
                        "exhaustive": False,
                    },
                    "selected_sources": ["openalex"],
                }
            ),
            StepResult(
                output={
                    "contract_version": 1,
                    "stage_type": "verify",
                    "usage": {"model_calls": 1, "total_tokens": 1, "batches": []},
                    "verification": {"passed": True, "claims": []},
                    "processing_coverage": {},
                }
            ),
        ]
        engine = WorkflowEngine(step_executor=mock_executor, observer=observer)

        events = [
            event
            async for event in engine.run(
                {"steps": [{"type": "search"}, {"type": "verify"}]}, uuid4()
            )
        ]
        snapshot = observer.snapshot()

        assert events[-1]["event"] == "run_complete"
        assert snapshot["counters"]["runs"]["started"] == 1
        assert snapshot["counters"]["runs"]["completed"] == 1
        assert snapshot["counters"]["provider_outcomes"]["ok"] == 1
        assert snapshot["counters"]["deduplication"]["removed"] == 1
        assert snapshot["counters"]["verification"]["passed"] == 1
        assert len(snapshot["histograms"]["stage_duration_seconds"]["search"]) == 1

    @pytest.mark.asyncio
    async def test_executor_exception_content_never_reaches_engine_events_or_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.side_effect = RuntimeError("PRIVATE_RESEARCH_QUESTION")
        observer = ResearchObservability()
        engine = WorkflowEngine(step_executor=mock_executor, observer=observer)

        with caplog.at_level("INFO"):
            events = [
                event
                async for event in engine.run(
                    {"steps": [{"id": "extract", "type": "extract"}]}, uuid4()
                )
            ]

        serialized = (
            json.dumps(events)
            + "\n"
            + "\n".join(record.getMessage() for record in caplog.records)
        )
        assert "PRIVATE_RESEARCH_QUESTION" not in serialized
        assert events[-1] == {
            "event": "run_failed",
            "run_id": events[-1]["run_id"],
            "error": "Research step execution failed",
            "error_category": "unexpected_step_error",
            "step_index": 0,
            "step_id": "extract",
        }
        assert (
            observer.snapshot()["counters"]["validation_errors"][
                "unexpected_step_error"
            ]
            == 1
        )

    @pytest.mark.asyncio
    async def test_malformed_telemetry_value_cannot_fail_run_or_leak_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output={
                "coverage": {
                    "providers": {
                        "openalex": {
                            "status": "ok",
                            "returned": "PRIVATE_RESEARCH_QUESTION",
                        }
                    }
                }
            }
        )
        engine = WorkflowEngine(step_executor=mock_executor)

        with caplog.at_level("INFO"):
            events = [
                event
                async for event in engine.run(
                    {"steps": [{"id": "search", "type": "search"}]}, uuid4()
                )
            ]

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert events[-1]["event"] == "run_complete"
        assert "PRIVATE_RESEARCH_QUESTION" not in log_text

    @pytest.mark.asyncio
    async def test_resume_does_not_double_count_run_started(self) -> None:
        observer = ResearchObservability()
        engine = WorkflowEngine(
            step_executor=AsyncMock(spec=StepExecutor), observer=observer
        )

        events = [
            event
            async for event in engine.run(
                {"steps": []},
                uuid4(),
                record_run_started=False,
            )
        ]

        assert events[-1]["event"] == "run_complete"
        counters = observer.snapshot()["counters"]["runs"]
        assert counters.get("started", 0) == 0
        assert counters["completed"] == 1

    @pytest.mark.asyncio
    async def test_run_blueprint(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output={"result": "done"},
            quality_marks=[QualityMark(check_type="source_grounding", passed=True)],
        )

        engine = WorkflowEngine(step_executor=mock_executor)

        blueprint = {
            "steps": [
                {"id": "step_1", "type": "search", "params": {}},
                {"id": "step_2", "type": "synthesize", "params": {}},
            ]
        }
        run_id = uuid4()

        events = []
        async for event in engine.run(blueprint, run_id):
            events.append(event)

        event_types = [e["event"] for e in events]
        assert "run_start" in event_types
        assert "step_start" in event_types
        assert "step_complete" in event_types
        assert "run_complete" in event_types

    @pytest.mark.asyncio
    async def test_run_pauses_on_quality_failure(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output={"result": "bad"},
            quality_marks=[
                QualityMark(
                    check_type="source_grounding", passed=False, details="mismatch"
                )
            ],
        )

        engine = WorkflowEngine(
            step_executor=mock_executor, pause_on_quality_failure=True
        )

        blueprint = {
            "steps": [
                {"id": "step_1", "type": "search", "params": {}},
                {"id": "step_2", "type": "synthesize", "params": {}},
            ]
        }
        run_id = uuid4()

        events = []
        async for event in engine.run(blueprint, run_id):
            events.append(event)

        event_types = [e["event"] for e in events]
        assert "run_paused" in event_types
        # Should NOT reach run_complete since it paused after step 1
        assert "run_complete" not in event_types
        # Only the first step should have been executed
        assert mock_executor.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_run_stops_before_next_step_when_token_budget_is_exhausted(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.side_effect = [
            StepResult(output={"first": "done"}, token_count=10),
            StepResult(output={"second": "should not run"}, token_count=1),
        ]
        engine = WorkflowEngine(step_executor=mock_executor, max_total_tokens=10)

        events = []
        async for event in engine.run(
            {"steps": [{"type": "search"}, {"type": "search"}]}, uuid4()
        ):
            events.append(event)

        assert events[-1]["event"] == "run_failed"
        assert "token budget" in events[-1]["error"]
        assert mock_executor.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_run_fails_when_a_step_exhausts_the_token_budget(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output={"result": "done"}, token_count=11
        )
        engine = WorkflowEngine(step_executor=mock_executor, max_total_tokens=10)

        events = []
        async for event in engine.run({"steps": [{"type": "search"}]}, uuid4()):
            events.append(event)

        assert events[-1]["event"] == "run_failed"
        assert "token budget" in events[-1]["error"]

    @pytest.mark.asyncio
    async def test_run_stops_before_first_step_when_wall_budget_is_exhausted(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        engine = WorkflowEngine(
            step_executor=mock_executor,
            max_wall_time_seconds=0,
            clock=lambda: 100.0,
        )

        events = []
        async for event in engine.run({"steps": [{"type": "search"}]}, uuid4()):
            events.append(event)

        assert events[-1]["event"] == "run_failed"
        assert "wall-time budget" in events[-1]["error"]
        mock_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_cancels_a_step_when_wall_budget_expires_during_execution(self):
        async def slow_execute(_step, _context, **_kwargs):
            await asyncio.sleep(0.2)
            return StepResult(output={"done": True})

        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.side_effect = slow_execute
        engine = WorkflowEngine(step_executor=mock_executor, max_wall_time_seconds=0.05)

        started = time.monotonic()
        events = []
        async for event in engine.run({"steps": [{"type": "search"}]}, uuid4()):
            events.append(event)

        assert time.monotonic() - started < 0.15
        assert events[-1]["event"] == "run_failed"
        assert "wall-time budget" in events[-1]["error"]

    @pytest.mark.asyncio
    async def test_run_resume_uses_persisted_token_budget_before_next_step(self):
        mock_executor = AsyncMock(spec=StepExecutor)
        engine = WorkflowEngine(step_executor=mock_executor, max_total_tokens=10)

        events = []
        async for event in engine.run(
            {"steps": [{"type": "search"}]},
            uuid4(),
            initial_total_tokens=10,
        ):
            events.append(event)

        assert events[-1]["event"] == "run_failed"
        mock_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_verification_pause_is_content_free_and_wins_quality_pause(
        self,
    ):
        output = {
            "contract_version": 1,
            "stage_type": "verify",
            "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
            "verification": {
                "passed": False,
                "deterministic_passed": True,
                "schema_passed": True,
                "semantic_status": "failed",
                "coverage_complete": True,
                "claims": [],
            },
            "processing_coverage": {},
        }
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output=output,
            quality_marks=[QualityMark("semantic_verification", False, "failed")],
        )
        observer = ResearchObservability()
        engine = WorkflowEngine(step_executor=mock_executor, observer=observer)

        events = [
            event
            async for event in engine.run(
                {"steps": [{"id": "verify", "type": "verify"}]}, uuid4()
            )
        ]
        paused = next(event for event in events if event["event"] == "run_paused")

        assert paused["pause_reason"] == "verification_failed"
        assert paused["step_index"] == 0
        assert paused["output_hash"] == canonical_stage_output_hash(output)
        assert "context" not in paused
        assert "quality_marks" not in paused
        assert observer.snapshot()["counters"].get("pauses", {}) == {}
        assert observer.snapshot()["counters"]["verification"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_incomplete_gated_stage_uses_review_descriptor_without_context(self):
        output = {
            "contract_version": 1,
            "stage_type": "screen",
            "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
            "screening": [
                {
                    "source_id": "source-a",
                    "part_id": "p0001",
                    "included": True,
                    "reason": "Matches",
                }
            ],
            "included_source_ids": ["source-a"],
            "processing_coverage": {"0": {"complete": False}},
        }
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.return_value = StepResult(
            output=output,
            quality_marks=[QualityMark("screening_coverage", False, "incomplete")],
        )
        observer = ResearchObservability()
        engine = WorkflowEngine(step_executor=mock_executor, observer=observer)

        events = [
            event
            async for event in engine.run(
                {
                    "steps": [
                        {
                            "id": "screen",
                            "type": "screen",
                            "parameters": {"review_gate": "screening"},
                        }
                    ]
                },
                uuid4(),
            )
        ]
        paused = next(event for event in events if event["event"] == "run_paused")

        assert paused["pause_reason"] == "review_required"
        assert paused["review_kind"] == "screening"
        assert paused["output_hash"] == canonical_stage_output_hash(output)
        assert "context" not in paused
        assert "quality_marks" not in paused
        assert observer.snapshot()["counters"].get("pauses", {}) == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stage_type", "output"),
        [
            (
                "screen",
                {
                    "contract_version": 1,
                    "stage_type": "screen",
                    "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                    "screening": [],
                    "included_source_ids": [],
                    "processing_coverage": {"0": {"complete": True}},
                },
            ),
            (
                "extract",
                {
                    "contract_version": 1,
                    "stage_type": "extract",
                    "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
                    "extractions": [
                        {
                            "source_id": "source-a",
                            "part_id": "p0001",
                            "claims": [],
                        }
                    ],
                    "processing_coverage": {"1": {"complete": True}},
                    "diagnostics": [],
                },
            ),
        ],
    )
    async def test_zero_evidence_stage_completes_without_later_calls_or_review(
        self, stage_type, output
    ):
        mock_executor = AsyncMock(spec=StepExecutor)
        mock_executor.execute.side_effect = [
            StepResult(output=output),
            StepResult(output={"must": "not execute"}),
        ]
        review_kind = "screening" if stage_type == "screen" else "extraction"
        engine = WorkflowEngine(step_executor=mock_executor)

        events = [
            event
            async for event in engine.run(
                {
                    "steps": [
                        {
                            "id": stage_type,
                            "type": stage_type,
                            "parameters": {"review_gate": review_kind},
                        },
                        {"id": "later-paid-call", "type": "synthesize"},
                    ]
                },
                uuid4(),
            )
        ]

        assert mock_executor.execute.await_count == 1
        assert not any(event["event"] == "run_paused" for event in events)
        assert events[-1]["event"] == "run_complete"
        assert events[-1]["final_status"] == "no_evidence"
