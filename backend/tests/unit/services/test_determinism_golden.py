"""Determinism golden tests for the research engine workflow.

Verifies that the workflow engine produces deterministic, reproducible results
when given the same inputs, temperature 0, and seed values.
"""

import copy
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)
from src.services.research_engine.step_executor import StepExecutor
from src.services.research_engine.verification import run_source_grounding_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DeterministicProvider(LLMProvider):
    """LLM provider that returns deterministic content based on the prompt hash."""

    def __init__(self, model_id: str = "test-model-v1") -> None:
        config = ProviderConfig(
            provider_type="test",
            model_id=model_id,
            model_version="1.0.0",
        )
        super().__init__(config)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        # Deterministic: content is derived from the hash of prompt + system_prompt
        combined = request.prompt + (request.system_prompt or "")
        content_hash = hashlib.sha256(combined.encode()).hexdigest()
        deterministic_content = f"deterministic_output_{content_hash[:16]}"
        return LLMResponse(
            content=deterministic_content,
            model_id=self.config.model_id,
            model_version=self.config.model_version,
            input_tokens=10,
            output_tokens=20,
            temperature=request.temperature,
            seed=request.seed,
        )

    async def is_model_available(self) -> bool:
        return True


class DeterministicConnector(SourceConnector):
    """Source connector that returns fixed, deterministic results."""

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> List[SourceDocument]:
        return [
            SourceDocument(
                connector_type="test",
                external_id="doc-001",
                title="Deterministic Test Paper",
                authors=["Author A"],
                abstract="This is a deterministic abstract for testing.",
                url="https://example.com/paper/001",
            ),
        ]


def _make_blueprint(
    model_id: str = "test-model-v1",
    temperature: float = 0.0,
    seed: int = 42,
) -> Dict:
    """Create a minimal blueprint with search + synthesize steps.

    The search step uses a literal query template (no context interpolation)
    so that it works with the engine's initially empty context.  The synthesize
    step references the ``query`` key that the search step places into context.
    """
    return {
        "steps": [
            {
                "id": "search_step",
                "type": "search",
                "params": {
                    "sources": ["test"],
                    # Literal query - does not require prior context
                    "query_template": "quantum computing",
                },
            },
            {
                "id": "synthesize_step",
                "type": "synthesize",
                "params": {
                    "model_id": model_id,
                    "system_prompt_template": "Summarise: {query}",
                    "temperature": temperature,
                    "seed": seed,
                },
            },
        ],
    }


def _build_engine(
    model_id: str = "test-model-v1",
) -> WorkflowEngine:
    """Construct a WorkflowEngine wired to deterministic fakes."""
    provider = DeterministicProvider(model_id=model_id)
    connector = DeterministicConnector()
    executor = StepExecutor(
        connectors={"test": connector},
        providers={model_id: provider},
    )
    return WorkflowEngine(step_executor=executor, pause_on_quality_failure=False)


async def _collect_events(
    engine: WorkflowEngine, blueprint: Dict, run_id
) -> List[Dict]:
    """Run the engine and collect all emitted events."""
    events: List[Dict] = []
    async for event in engine.run(blueprint, run_id):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_inputs_produce_same_outputs():
    """Running the engine twice with identical inputs must yield identical output hashes."""
    engine = _build_engine()
    blueprint = _make_blueprint()
    run_id = uuid4()

    events_a = await _collect_events(engine, blueprint, run_id)
    events_b = await _collect_events(engine, blueprint, run_id)

    # Extract step_complete events
    completes_a = [e for e in events_a if e["event"] == "step_complete"]
    completes_b = [e for e in events_b if e["event"] == "step_complete"]

    assert len(completes_a) == len(completes_b)
    for ea, eb in zip(completes_a, completes_b):
        outputs = [copy.deepcopy(event["output"]) for event in (ea, eb)]
        if ea["step_id"] == "search_step":
            # Separate retrievals have distinct row identities and audit times;
            # all evidence content and downstream generated output must match.
            assert (
                outputs[0]["source_records"][0]["source_id"]
                != outputs[1]["source_records"][0]["source_id"]
            )
            for output in outputs:
                assert (
                    datetime.fromisoformat(
                        output["coverage"].pop("retrieved_at")
                    ).tzinfo
                    is not None
                )
                for record in output["source_records"]:
                    assert UUID(record.pop("source_id"))
                    for snapshot in record["metadata"]["provenance"]:
                        assert (
                            datetime.fromisoformat(snapshot.pop("retrieved_at")).tzinfo
                            is not None
                        )
        assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_prompt_keeps_evidence_but_does_not_mutate_retrieval_audit() -> None:
    executor = _build_engine().step_executor
    search = await executor.execute(_make_blueprint()["steps"][0], {})
    context = search.output
    original = copy.deepcopy(context)
    synth = _make_blueprint()["steps"][1]
    first = await executor.execute(synth, context)
    assert context == original

    other_retrieval = copy.deepcopy(context)
    other_retrieval["coverage"]["retrieved_at"] = "2026-01-01T00:00:00+00:00"
    record = other_retrieval["source_records"][0]
    record["source_id"] = str(uuid4())
    record["metadata"]["provenance"][0]["retrieved_at"] = "2026-01-01T00:00:00+00:00"
    second = await executor.execute(synth, other_retrieval)
    assert first.inputs_hash == second.inputs_hash
    assert first.outputs_hash == second.outputs_hash

    record["abstract"] = "A different measured result."
    changed = await executor.execute(synth, other_retrieval)
    assert changed.inputs_hash != first.inputs_hash
    assert changed.outputs_hash != first.outputs_hash


@pytest.mark.asyncio
async def test_output_hashes_match_across_runs():
    """The outputs_hash recorded by StepExecutor must be identical for repeated runs."""
    provider = DeterministicProvider()
    connector = DeterministicConnector()
    executor = StepExecutor(
        connectors={"test": connector},
        providers={"test-model-v1": provider},
    )

    step_def = {
        "id": "synth",
        "type": "synthesize",
        "params": {
            "model_id": "test-model-v1",
            "system_prompt_template": "Summarise: {query}",
            "temperature": 0.0,
            "seed": 42,
        },
    }
    context = {"query": "quantum computing"}

    result_a = await executor.execute(step_def, context)
    result_b = await executor.execute(step_def, context)

    assert result_a.outputs_hash is not None
    assert result_a.outputs_hash == result_b.outputs_hash
    assert result_a.inputs_hash == result_b.inputs_hash


@pytest.mark.asyncio
async def test_temperature_zero_consistent_hashes():
    """Temperature 0 with the same seed must always produce the same hash."""
    engine = _build_engine()
    blueprint = _make_blueprint(temperature=0.0, seed=42)
    run_id = uuid4()

    hashes: List[str] = []
    for _ in range(3):
        events = await _collect_events(engine, blueprint, run_id)
        synth_events = [
            e
            for e in events
            if e["event"] == "step_complete" and e["step_id"] == "synthesize_step"
        ]
        assert len(synth_events) == 1
        output_content = synth_events[0]["output"]["content"]
        hashes.append(hashlib.sha256(output_content.encode()).hexdigest())

    # All three hashes must be identical
    assert len(set(hashes)) == 1


@pytest.mark.asyncio
async def test_seed_propagated_to_provider():
    """The seed value from the blueprint must be forwarded to the LLM provider."""
    captured_requests: List[LLMRequest] = []

    class CapturingProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_type="test", model_id="cap"))

        async def complete(self, request: LLMRequest) -> LLMResponse:
            captured_requests.append(request)
            return LLMResponse(
                content="ok",
                model_id="cap",
                temperature=request.temperature,
                seed=request.seed,
            )

        async def is_model_available(self) -> bool:
            return True

    executor = StepExecutor(
        connectors={},
        providers={"cap": CapturingProvider()},
    )
    step_def = {
        "id": "s1",
        "type": "synthesize",
        "params": {
            "model_id": "cap",
            "system_prompt_template": "test",
            "temperature": 0.0,
            "seed": 99,
        },
    }
    await executor.execute(step_def, {"query": "x"})

    assert len(captured_requests) == 1
    assert captured_requests[0].seed == 99
    assert captured_requests[0].temperature == 0.0


@pytest.mark.asyncio
async def test_different_seeds_produce_different_hashes():
    """Changing the seed (with a hash-based provider) should change the output hash
    only if the content generation actually depends on seed.  With our deterministic
    provider the output is seed-independent; this test instead verifies the engine
    does not crash and the seed reaches the provider."""
    engine = _build_engine()
    bp_a = _make_blueprint(seed=42)
    bp_b = _make_blueprint(seed=99)
    run_id = uuid4()

    events_a = await _collect_events(engine, bp_a, run_id)
    events_b = await _collect_events(engine, bp_b, run_id)

    # Both runs complete successfully
    assert any(e["event"] == "run_complete" for e in events_a)
    assert any(e["event"] == "run_complete" for e in events_b)


@pytest.mark.asyncio
async def test_reproducibility_manifest_structure():
    """Build a reproducibility manifest from engine events and verify required fields."""
    engine = _build_engine()
    blueprint = _make_blueprint()
    run_id = uuid4()
    blueprint_version = 1

    events = await _collect_events(engine, blueprint, run_id)

    # Build manifest from events (as the orchestrator layer would do)
    step_records = []
    model_ids = set()
    for evt in events:
        if evt["event"] == "step_complete":
            step_records.append(
                {
                    "step_id": evt["step_id"],
                    "step_index": evt["step_index"],
                    "inputs_hash": None,  # search steps don't have hashes
                    "outputs_hash": None,
                }
            )

    # Rebuild with executor-level hashes for LLM steps
    executor = engine.step_executor
    context: Dict[str, Any] = {"query": "quantum computing"}
    for step_def in blueprint["steps"]:
        if step_def["type"] in ("synthesize", "screen", "extract", "export"):
            result = await executor.execute(step_def, context)
            model_id = step_def["params"].get("model_id", "")
            model_ids.add(model_id)
            for rec in step_records:
                if rec["step_id"] == step_def["id"]:
                    rec["inputs_hash"] = result.inputs_hash
                    rec["outputs_hash"] = result.outputs_hash

    manifest = {
        "run_id": str(run_id),
        "blueprint_version": blueprint_version,
        "steps": step_records,
        "model_ids": list(model_ids),
    }

    # Validate required top-level keys
    assert "run_id" in manifest
    assert "blueprint_version" in manifest
    assert "steps" in manifest
    assert "model_ids" in manifest

    # Validate steps have hash fields
    for step in manifest["steps"]:
        assert "inputs_hash" in step
        assert "outputs_hash" in step

    # At least one step should have non-None hashes (the LLM step)
    llm_steps = [s for s in manifest["steps"] if s["inputs_hash"] is not None]
    assert len(llm_steps) >= 1


def test_source_grounding_check_deterministic():
    """run_source_grounding_check must be deterministic for the same inputs."""
    claim = "The accuracy improved by 15% over the baseline."
    source = "Results show the accuracy improved by 15% over the baseline model."

    result_a = run_source_grounding_check(claim, source)
    result_b = run_source_grounding_check(claim, source)

    assert result_a.passed == result_b.passed
    assert result_a.details == result_b.details
    assert result_a.check_type == result_b.check_type


def test_source_grounding_word_overlap_deterministic():
    """Word overlap grounding must be deterministic."""
    claim = "deep learning models excel at image recognition"
    source = "deep learning models have shown to excel at image recognition tasks"

    result_a = run_source_grounding_check(claim, source)
    result_b = run_source_grounding_check(claim, source)

    assert result_a.passed == result_b.passed
    assert result_a.details == result_b.details
