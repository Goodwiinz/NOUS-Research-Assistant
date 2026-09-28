"""Measured performance gates for the controlled Daily Research Brief workflow."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import tracemalloc
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from src.schemas.research_engine import (
    MAX_PENDING_REVIEW_OUTPUT_BYTES,
    PendingReviewResponse,
    ReviewDescriptor,
    ReviewKind,
)
from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.contracts import (
    canonical_stage_output_hash,
    merge_stage_output,
    rehydrate_stage_outputs,
)
from src.services.research_engine.review_service import ResearchReviewService
from src.services.research_engine.step_executor import StepExecutor
from tests.integration.test_daily_research_brief_postgres import (
    _ABSTRACT,
    _ControlledConnector,
    _ControlledProvider,
    _daily_brief_lifecycle,
)

pytestmark = pytest.mark.asyncio

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "services"
    / "research_engine"
    / "blueprints"
    / "templates"
    / "daily_research_brief.yaml"
)
_RUNS = 31
_FANOUT_RUNS = 21
_MAX_STAGE_RUNS = 40
_SOAK_RUNS = 10

# Captured before freezing on 2026-09-28. Latency thresholds are the observed
# p95 plus 20%; byte ceilings use the same measured headroom and remain below
# the server-owned response bound. The PostgreSQL soak uses ten isolated full
# lifecycles, including three reloads and simultaneous resumes per lifecycle.
_MEASURED_BASELINE: dict[str, float | int] = {
    "six_stage_p50_ms": 24.769,
    "six_stage_p95_ms": 29.454,
    "export_p50_ms": 0.518,
    "export_p95_ms": 0.688,
    "export_payload_bytes": 15294,
    "fanout_1_p95_ms": 6.577,
    "fanout_2_p95_ms": 11.284,
    "fanout_4_p95_ms": 31.62,
    "max_search_p95_ms": 32.143,
    "max_screen_p95_ms": 64.343,
    "max_extract_p95_ms": 708.04,
    "max_stage_payload_bytes": 1152543,
    "max_stage_peak_bytes": 3624356,
    "cold_hydration_p50_ms": 2.849,
    "cold_hydration_p95_ms": 3.319,
    "soak_lifecycle_p50_ms": 922.647,
    "soak_lifecycle_p95_ms": 1723.942,
    "review_projection_p50_ms": 7.005,
    "review_projection_p95_ms": 7.735,
    "review_payload_bytes": 24094,
}
_FROZEN_THRESHOLDS: dict[str, float | int] = {
    "six_stage_p95_ms": 35.345,
    "export_p95_ms": 0.826,
    "export_payload_bytes": 18353,
    "fanout_1_p95_ms": 7.893,
    "fanout_2_p95_ms": 13.541,
    "fanout_4_p95_ms": 37.944,
    "max_search_p95_ms": 38.572,
    "max_screen_p95_ms": 77.212,
    "max_extract_p95_ms": 849.648,
    "max_stage_payload_bytes": 1383052,
    "max_stage_peak_bytes": 4349228,
    "cold_hydration_p95_ms": 3.983,
    "soak_lifecycle_p95_ms": 2068.731,
    "review_projection_p95_ms": 9.282,
    "review_payload_bytes": 28913,
}


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(len(ordered) * percentile + 0.999999) - 1)]


def _base_context(providers: list[str], limit_per_provider: int) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "research_question": "Does the controlled treatment reduce score?",
        "inclusion_criteria": ["Reports the measured score"],
        "exclusion_criteria": ["No outcome data"],
        "providers": providers,
        "limit_per_provider": limit_per_provider,
        "notes": "Controlled performance fixture",
        "scope_confirmation": {"confirmed": True},
        "provider_manifest": [
            {"id": provider_id, "enabled": True} for provider_id in providers
        ],
        "artifact_provenance": {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "blueprint_id": "00000000-0000-0000-0000-000000000002",
            "blueprint_version": 1,
            "template_source": "daily_research_brief",
            "template_contract_version": 1,
            "stage_hashes": {},
            "models": [{"model_id": "controlled-v1"}],
            "review_history": [],
            "scope_confirmation": {"confirmed": True},
            "provider_manifest": [
                {"id": provider_id, "enabled": True} for provider_id in providers
            ],
            "limitations": ["Controlled fixture"],
        },
    }


def _clear_recorded_requests(provider: _ControlledProvider) -> None:
    """Discard fixture request history after each measured stage."""

    provider.requests.clear()
    assert provider.requests == []


async def _run_six_stages() -> tuple[dict[str, Any], list[float]]:
    template = cast(
        dict[str, Any], yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    )
    provider = _ControlledProvider()
    executor = StepExecutor(
        providers={"claude-sonnet-4-6": provider},
        connectors={"openalex": _ControlledConnector()},
    )
    context = _base_context(["openalex"], 1)
    stage_ms: list[float] = []
    for index, step in enumerate(template["steps"]):
        started = time.perf_counter()
        result = await executor.execute(step, context)
        stage_ms.append((time.perf_counter() - started) * 1000)
        _clear_recorded_requests(provider)
        context = merge_stage_output(context, result.output, index)
        context["artifact_provenance"]["stage_hashes"][str(index)] = (
            canonical_stage_output_hash(result.output)
        )
    return context, stage_ms


class _MaxBoundConnector(SourceConnector):
    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del query, kwargs
        return [
            SourceDocument(
                connector_type=self.provider,
                external_id=f"{self.provider}-{index}",
                title=f"{self.provider} controlled candidate {index}",
                authors=["Controlled Author"],
                abstract=f"{_ABSTRACT} Candidate {index}. " + "x" * 512,
                url=f"https://example.test/{self.provider}/{index}",
                metadata={"doi": f"10.1234/{self.provider}-{index}"},
            )
            for index in range(50)
        ][:max_results]


async def _bounded_search(provider_count: int) -> dict[str, Any]:
    providers = ["openalex", "crossref", "pubmed", "semantic_scholar"][:provider_count]
    result = await StepExecutor(
        providers={},
        connectors={name: _MaxBoundConnector(name) for name in providers},
    ).execute(
        {
            "type": "search",
            "parameters": {
                "contract_version": 1,
                "sources": providers,
                "query_template": "{research_question}",
                "max_results_per_source": 50,
            },
        },
        {
            "contract_version": 1,
            "research_question": "controlled maximum-bound query",
        },
    )
    return result.output


async def _max_bound_screen_and_extract() -> tuple[dict[str, Any], list[float]]:
    providers = ["openalex", "crossref", "pubmed", "semantic_scholar"]
    template = cast(
        dict[str, Any], yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    )
    provider = _ControlledProvider()
    executor = StepExecutor(
        providers={"claude-sonnet-4-6": provider},
        connectors={name: _MaxBoundConnector(name) for name in providers},
    )
    context = _base_context(providers, 50)
    stage_ms: list[float] = []
    for index, step in enumerate(template["steps"][:3]):
        started = time.perf_counter()
        result = await executor.execute(step, context)
        stage_ms.append((time.perf_counter() - started) * 1000)
        _clear_recorded_requests(provider)
        context = merge_stage_output(context, result.output, index)
    return context, stage_ms


def _review_response() -> PendingReviewResponse:
    run_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    output = {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 4, "total_tokens": 800, "batches": []},
        "screening": [
            {
                "source_id": str(uuid.UUID(int=index + 10)),
                "part_id": "abstract",
                "included": True,
                "reason": "r" * 1000,
                "evidence_level": "abstract",
            }
            for index in range(200)
        ],
        "included_source_ids": [str(uuid.UUID(int=index + 10)) for index in range(200)],
        "processing_coverage": {},
    }
    projected = ResearchReviewService._pending_stage_output(output)
    return PendingReviewResponse(
        pending=True,
        descriptor=ReviewDescriptor(
            run_id=run_id,
            step_index=1,
            stage_type="screen",
            review_kind=ReviewKind.SCREENING,
            contract_version=1,
            output_hash=canonical_stage_output_hash(output),
        ),
        stage_output=projected,
    )


async def test_controlled_six_stage_latency_baseline() -> None:
    await _run_six_stages()  # warm caches before sampling
    totals: list[float] = []
    per_stage: list[list[float]] = [[] for _ in range(6)]
    for _ in range(_RUNS):
        started = time.perf_counter()
        context, stage_ms = await _run_six_stages()
        totals.append((time.perf_counter() - started) * 1000)
        for index, duration in enumerate(stage_ms):
            per_stage[index].append(duration)
        assert context["verification"]["passed"] is True
        assert context["format"] == "markdown"

    metrics = {
        "six_stage_p50_ms": round(statistics.median(totals), 3),
        "six_stage_p95_ms": round(_percentile(totals, 0.95), 3),
        "stage_p95_ms": [round(_percentile(values, 0.95), 3) for values in per_stage],
        "export_p50_ms": round(statistics.median(per_stage[5]), 3),
        "export_p95_ms": round(_percentile(per_stage[5], 0.95), 3),
        "export_payload_bytes": len(
            json.dumps(
                context["exported"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ),
    }
    if os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1":
        print("DAILY_BRIEF_PERF_SIX_STAGE=" + json.dumps(metrics, sort_keys=True))
    threshold = _FROZEN_THRESHOLDS.get("six_stage_p95_ms")
    if threshold is not None:
        assert metrics["six_stage_p95_ms"] <= threshold
    for metric in ("export_p95_ms", "export_payload_bytes"):
        threshold = _FROZEN_THRESHOLDS.get(metric)
        if threshold is not None:
            assert metrics[metric] <= threshold


async def test_max_stage_sample_policy_uses_nearest_rank_p95() -> None:
    ordered_samples = [float(value) for value in range(1, _MAX_STAGE_RUNS + 1)]

    # With 40 samples, nearest-rank p95 is rank 38. The two slowest samples do
    # not silently turn this gate into a maximum; a third slow sample does.
    assert _percentile(ordered_samples, 0.95) == 38.0
    assert _percentile(([0.0] * 38) + [1.0, 2.0], 0.95) == 0.0
    assert _percentile(([0.0] * 37) + [1.0, 2.0, 3.0], 0.95) == 1.0


async def test_provider_fanout_and_max_batch_latency_are_bounded() -> None:
    fanout_metrics: dict[str, Any] = {}
    for provider_count in (1, 2, 4):
        samples: list[float] = []
        output: dict[str, Any] = {}
        await _bounded_search(provider_count)
        for _ in range(_FANOUT_RUNS):
            started = time.perf_counter()
            output = await _bounded_search(provider_count)
            samples.append((time.perf_counter() - started) * 1000)
        candidate_count = provider_count * 50
        assert len(output["source_records"]) == candidate_count
        assert output["coverage"]["deduplication"] == {
            "before": candidate_count,
            "after": candidate_count,
        }
        fanout_metrics[str(provider_count)] = {
            "candidate_rows": candidate_count,
            "p50_ms": round(statistics.median(samples), 3),
            "p95_ms": round(_percentile(samples, 0.95), 3),
        }

    stage_samples: list[list[float]] = [[], [], []]
    max_context: dict[str, Any] = {}
    await _max_bound_screen_and_extract()
    for _ in range(_MAX_STAGE_RUNS):
        max_context, stage_ms = await _max_bound_screen_and_extract()
        for index, duration in enumerate(stage_ms):
            stage_samples[index].append(duration)
    metrics = {
        "provider_fanout": fanout_metrics,
        "max_search_p95_ms": round(_percentile(stage_samples[0], 0.95), 3),
        "max_screen_p95_ms": round(_percentile(stage_samples[1], 0.95), 3),
        "max_extract_p95_ms": round(_percentile(stage_samples[2], 0.95), 3),
        "screen_batches": max_context["stage_results"]["1"]["usage"]["model_calls"],
        "extract_batches": max_context["stage_results"]["2"]["usage"]["model_calls"],
    }
    assert len(max_context["source_records"]) == 200
    assert len(max_context["screening"]) == 200
    assert len(max_context["extractions"]) == 200
    if os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1":
        print("DAILY_BRIEF_PERF_FANOUT=" + json.dumps(metrics, sort_keys=True))

    for count, values in fanout_metrics.items():
        threshold = _FROZEN_THRESHOLDS.get(f"fanout_{count}_p95_ms")
        if threshold is not None:
            assert values["p95_ms"] <= threshold
    for metric in ("max_search_p95_ms", "max_screen_p95_ms", "max_extract_p95_ms"):
        threshold = _FROZEN_THRESHOLDS.get(metric)
        if threshold is not None:
            assert metrics[metric] <= threshold


async def test_max_bound_and_review_payload_are_bounded() -> None:
    tracemalloc.start()
    try:
        max_context, _ = await _max_bound_screen_and_extract()
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    output = cast(dict[str, Any], max_context["stage_results"]["0"])
    payload_bytes = len(
        json.dumps(max_context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    review_ms: list[float] = []
    review_payload_bytes = 0
    for _ in range(101):
        started = time.perf_counter()
        response = _review_response()
        review_ms.append((time.perf_counter() - started) * 1000)
        review_payload_bytes = len(response.model_dump_json().encode("utf-8"))
    metrics = {
        "candidate_rows": len(output["source_records"]),
        "max_stage_payload_bytes": payload_bytes,
        "max_stage_peak_bytes": peak_bytes,
        "review_projection_p50_ms": round(statistics.median(review_ms), 3),
        "review_projection_p95_ms": round(_percentile(review_ms, 0.95), 3),
        "review_payload_bytes": review_payload_bytes,
    }
    if os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1":
        print("DAILY_BRIEF_PERF_BOUNDS=" + json.dumps(metrics, sort_keys=True))

    assert metrics["candidate_rows"] == 200
    assert output["coverage"]["deduplication"] == {"before": 200, "after": 200}
    assert review_payload_bytes <= MAX_PENDING_REVIEW_OUTPUT_BYTES + 4096
    for metric in (
        "max_stage_payload_bytes",
        "max_stage_peak_bytes",
        "review_projection_p95_ms",
        "review_payload_bytes",
    ):
        threshold = _FROZEN_THRESHOLDS.get(metric)
        if threshold is not None:
            assert metrics[metric] <= threshold


async def test_cold_persisted_hydration_latency_is_bounded() -> None:
    context, _ = await _run_six_stages()
    persisted = [
        SimpleNamespace(step_index=int(index), output=output)
        for index, output in context["stage_results"].items()
    ]
    samples: list[float] = []
    hydrated: dict[str, Any] = {}
    for _ in range(101):
        started = time.perf_counter()
        hydrated = rehydrate_stage_outputs(persisted)
        samples.append((time.perf_counter() - started) * 1000)
    metrics = {
        "cold_hydration_p50_ms": round(statistics.median(samples), 3),
        "cold_hydration_p95_ms": round(_percentile(samples, 0.95), 3),
    }
    assert hydrated["report_hash"] == context["report_hash"]
    assert hydrated["verification_output_hash"] == context["verification_output_hash"]
    if os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1":
        print("DAILY_BRIEF_PERF_HYDRATION=" + json.dumps(metrics, sort_keys=True))
    threshold = _FROZEN_THRESHOLDS.get("cold_hydration_p95_ms")
    if threshold is not None:
        assert metrics["cold_hydration_p95_ms"] <= threshold


async def test_repeated_postgres_reconnect_lifecycle_soak() -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    samples: list[float] = []
    review_ids: set[str] = set()
    for _ in range(_SOAK_RUNS):
        started = time.perf_counter()
        async with _daily_brief_lifecycle(dsn) as persisted:
            samples.append((time.perf_counter() - started) * 1000)
            assert persisted.status == "completed"
            assert persisted.duplicate_stage_rows == 0
            assert len(persisted.stage_hashes) == 6
            assert len(persisted.review_ids) == 3
            assert not (review_ids & set(persisted.review_ids))
            review_ids.update(persisted.review_ids)
    metrics = {
        "iterations": _SOAK_RUNS,
        "completed": len(samples),
        "lifecycle_p50_ms": round(statistics.median(samples), 3),
        "lifecycle_p95_ms": round(_percentile(samples, 0.95), 3),
        "duplicate_stage_rows": 0,
        "unique_review_rows": len(review_ids),
    }
    if os.getenv("DAILY_BRIEF_PERF_CAPTURE") == "1":
        print("DAILY_BRIEF_PERF_SOAK=" + json.dumps(metrics, sort_keys=True))
    assert metrics["completed"] == _SOAK_RUNS
    assert metrics["unique_review_rows"] == _SOAK_RUNS * 3
    assert metrics["lifecycle_p95_ms"] <= _FROZEN_THRESHOLDS["soak_lifecycle_p95_ms"]


async def _run_script() -> None:
    await test_max_stage_sample_policy_uses_nearest_rank_p95()
    await test_controlled_six_stage_latency_baseline()
    await test_provider_fanout_and_max_batch_latency_are_bounded()
    await test_max_bound_and_review_payload_are_bounded()
    await test_cold_persisted_hydration_latency_is_bounded()
    await test_repeated_postgres_reconnect_lifecycle_soak()
    print(
        "DAILY_BRIEF_PERF_FROZEN="
        + json.dumps(
            {
                "baseline": _MEASURED_BASELINE,
                "thresholds": _FROZEN_THRESHOLDS,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_run_script())
