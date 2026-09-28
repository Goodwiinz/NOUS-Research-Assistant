"""Credential-gated current-model evaluation for Daily Research Brief quality."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from src.api.research_engine.runs import _build_providers
from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
    merge_stage_output,
)
from src.services.research_engine.step_executor import StepExecutor

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "services"
    / "research_engine"
    / "blueprints"
    / "templates"
    / "daily_research_brief.yaml"
)


def _live_anthropic_key() -> str | None:
    """Return only a credential that can authorize the live provider eval.

    The repository-wide pytest collection imports the integration conftest,
    which installs a deterministic provider fixture in ``os.environ``.  That
    value is intentionally not a live credential and must never turn this
    networked eval into an attempted candidate run.
    """

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key or key == "test-anthropic-key":
        return None
    return key


def _live_eval_prerequisites() -> tuple[str, float | None]:
    """Validate release-eval prerequisites before any provider call."""

    if _live_anthropic_key() is None:
        pytest.skip("BLOCKED: ANTHROPIC_API_KEY is absent; live eval was not run")

    mode = os.getenv("DAILY_BRIEF_EVAL_MODE", "candidate")
    frozen_value = os.getenv("DAILY_BRIEF_CITATION_THRESHOLD")
    if mode == "baseline":
        return mode, None
    if frozen_value is None:
        pytest.skip(
            "BLOCKED: DAILY_BRIEF_CITATION_THRESHOLD is absent; "
            "candidate live eval was not run"
        )
    try:
        citation_threshold = float(frozen_value)
    except ValueError:
        pytest.fail("DAILY_BRIEF_CITATION_THRESHOLD must be a number from 0 to 1")
    if not 0.0 <= citation_threshold <= 1.0:
        pytest.fail("DAILY_BRIEF_CITATION_THRESHOLD must be between 0 and 1")
    return mode, citation_threshold


class _EvalEvidenceConnector(SourceConnector):
    """Controlled evidence keeps retrieval fixed while the configured model is live."""

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del query, kwargs
        return [
            SourceDocument(
                connector_type="openalex",
                external_id="W-eval-abstract",
                title="Controlled randomized treatment study",
                authors=["Ada Evidence"],
                abstract=(
                    "In a randomized study of 42 adults, the treatment reduced "
                    "the measured score by 4.0 points compared with control."
                ),
                url="https://example.test/eval/abstract",
                metadata={"doi": "10.1234/eval-abstract"},
            ),
            SourceDocument(
                connector_type="openalex",
                external_id="W-eval-full-text",
                title="Controlled replication study",
                authors=["Grace Grounding"],
                full_text=(
                    "Methods: 18 participants were assigned to treatment. "
                    "Findings: the measured score decreased by 2.0 points. "
                    "Limitation: the sample was small."
                ),
                url="https://example.test/eval/full-text",
                metadata={"doi": "10.1234/eval-full-text"},
            ),
            SourceDocument(
                connector_type="openalex",
                external_id="W-eval-metadata",
                title="Registry record without evidentiary text",
                authors=["Meta Data"],
                url="https://example.test/eval/metadata",
                metadata={"doi": "10.1234/eval-metadata"},
            ),
        ][:max_results]


async def _run_current_model() -> tuple[dict[str, Any], dict[str, Any]]:
    template = cast(
        dict[str, Any], yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    )
    providers = _build_providers(template["steps"])
    required_model = "claude-sonnet-4-6"
    if required_model not in providers:
        pytest.skip(
            "BLOCKED: configured model claude-sonnet-4-6 has no ANTHROPIC_API_KEY"
        )

    context: dict[str, Any] = {
        "contract_version": 1,
        "research_question": (
            "What does the controlled evidence say about treatment effects?"
        ),
        "inclusion_criteria": ["Reports a measured treatment effect"],
        "exclusion_criteria": ["Has no evidentiary text"],
        "providers": ["openalex"],
        "limit_per_provider": 3,
        "notes": "Current configured-model evidence-quality evaluation",
        "scope_confirmation": {"confirmed": True},
        "provider_manifest": [{"id": "openalex", "enabled": True}],
        "artifact_provenance": {
            "run_id": str(uuid.UUID(int=101)),
            "blueprint_id": str(uuid.UUID(int=102)),
            "blueprint_version": 1,
            "template_source": "daily_research_brief",
            "template_contract_version": 1,
            "stage_hashes": {},
            "models": [{"model_id": required_model}],
            "review_history": [],
            "scope_confirmation": {"confirmed": True},
            "provider_manifest": [{"id": "openalex", "enabled": True}],
            "limitations": ["Controlled external evidence fixture"],
        },
    }
    executor = StepExecutor(
        providers=providers,
        connectors={"openalex": _EvalEvidenceConnector()},
    )
    outputs: list[dict[str, Any]] = []
    for index, step in enumerate(template["steps"]):
        result = await executor.execute(step, context)
        outputs.append(result.output)
        context = merge_stage_output(context, result.output, index)
        context["artifact_provenance"]["stage_hashes"][str(index)] = (
            canonical_stage_output_hash(result.output)
        )
    return context, {"template": template, "outputs": outputs}


def _quality_metrics(
    context: dict[str, Any], run: dict[str, Any]
) -> dict[str, float | int]:
    records = context["source_records"]
    expected_labels = {
        "Controlled randomized treatment study": "abstract",
        "Controlled replication study": "full_text",
        "Registry record without evidentiary text": "metadata_only",
    }
    correct_labels = sum(
        record["evidence_level"] == expected_labels[record["title"]]
        for record in records
    )
    evidence = {
        item["evidence_id"]: item
        for extraction in context["extractions"]
        for item in extraction["evidence"]
    }
    checks = {item["claim_id"]: item for item in context["verification"]["claims"]}
    claims = context["synthesis"]["claims"]
    citations = [
        (claim, reference)
        for claim in claims
        for reference in claim.get("evidence", [])
    ]
    correct_citations = sum(
        reference["evidence_id"] in evidence
        and checks.get(claim["claim_id"], {}).get("status") == "supported"
        for claim, reference in citations
    )
    unsupported_claims = sum(
        checks.get(claim["claim_id"], {}).get("status") != "supported"
        for claim in claims
    )
    configured_gates = [
        step["parameters"]["review_gate"]
        for step in run["template"]["steps"]
        if step.get("parameters", {}).get("review_gate")
    ]
    export_output = run["outputs"][-1]
    stage_types = [output.get("stage_type") for output in run["outputs"]]

    screening_output = run["outputs"][1]
    screening_coverage = screening_output.get("processing_coverage", {})
    screening_parts = {
        (item.get("source_id"), item.get("part_id"))
        for coverage in screening_coverage.values()
        if isinstance(coverage, dict)
        for item in coverage.get("source_parts", [])
        if isinstance(item, dict)
    }
    screening_decisions = {
        (item.get("source_id"), item.get("part_id"))
        for item in screening_output.get("screening", [])
        if isinstance(item, dict)
    }

    extraction_output = run["outputs"][2]
    extraction_coverage = extraction_output.get("processing_coverage", {})
    extraction_parts = {
        (item.get("source_id"), item.get("part_id"))
        for coverage in extraction_coverage.values()
        if isinstance(coverage, dict)
        for item in coverage.get("source_parts", [])
        if isinstance(item, dict)
    }
    extraction_records = {
        (item.get("source_id"), item.get("part_id"))
        for item in extraction_output.get("extractions", [])
        if isinstance(item, dict)
    }
    source_text_by_id = {
        record["source_id"]: record.get("full_text") or record.get("abstract") or ""
        for record in records
    }
    grounded_quotes = all(
        isinstance(item.get("quote"), str)
        and item["quote"] in source_text_by_id.get(extraction["source_id"], "")
        for extraction in extraction_output.get("extractions", [])
        for item in extraction.get("evidence", [])
    )
    verification_claims = {
        item.get("claim_id"): item
        for item in run["outputs"][4].get("verification", {}).get("claims", [])
        if isinstance(item, dict)
    }
    claim_ids = {claim.get("claim_id") for claim in claims}
    supported_exact_claim_set = set(verification_claims) == claim_ids and all(
        item.get("status") == "supported" for item in verification_claims.values()
    )
    # This is intentionally a live-output readiness check. Persistence, hash-bound
    # decisions, reload, replay, and resume behavior are exercised by the separate
    # PostgreSQL and browser lifecycle suites; this eval must not claim those from
    # template strings alone.
    live_gate_output_readiness = all(
        (
            configured_gates == ["screening", "extraction", "final"],
            stage_types
            == ["search", "screen", "extract", "synthesize", "verify", "export"],
            bool(screening_parts),
            screening_decisions == screening_parts,
            len(screening_output.get("screening", [])) == len(screening_parts),
            bool(extraction_parts),
            extraction_records == extraction_parts,
            len(extraction_output.get("extractions", [])) == len(extraction_parts),
            grounded_quotes,
            supported_exact_claim_set,
            export_output.get("verification_output_hash")
            == canonical_stage_output_hash(run["outputs"][4]),
            export_output.get("report_hash")
            == canonical_json_sha256(export_output.get("exported")),
        )
    )
    return {
        "citation_correctness": (
            correct_citations / len(citations) if citations else 0.0
        ),
        "unsupported_material_claims": unsupported_claims,
        "live_gate_output_readiness": 1.0 if live_gate_output_readiness else 0.0,
        "coverage_label_correctness": correct_labels / len(expected_labels),
        "export_hash_reconstruction": (
            1.0
            if export_output["report_hash"]
            == canonical_json_sha256(export_output["exported"])
            else 0.0
        ),
        "claim_count": len(claims),
        "citation_count": len(citations),
    }


async def test_current_configured_model_meets_frozen_quality_gates() -> None:
    """Run live model calls only; deterministic substitutes cannot satisfy this gate."""

    mode, citation_threshold = _live_eval_prerequisites()

    context, run = await _run_current_model()
    metrics = _quality_metrics(context, run)
    print("DAILY_BRIEF_MODEL_EVAL=" + json.dumps(metrics, sort_keys=True))

    assert metrics["claim_count"] > 0
    assert metrics["citation_count"] > 0
    assert metrics["unsupported_material_claims"] == 0
    assert metrics["live_gate_output_readiness"] == 1.0
    assert metrics["coverage_label_correctness"] == 1.0
    assert metrics["export_hash_reconstruction"] == 1.0
    if mode == "baseline":
        print("DAILY_BRIEF_CITATION_BASELINE=" + str(metrics["citation_correctness"]))
    else:
        assert citation_threshold is not None
        assert metrics["citation_correctness"] >= citation_threshold


async def test_integration_fixture_key_cannot_unlock_live_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full test collection's provider fixture is not release evidence."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    assert _live_anthropic_key() is None


@pytest.mark.parametrize(
    ("key", "mode", "threshold", "blocked", "expected_calls"),
    [
        (None, "candidate", "0.9", True, 0),
        ("test-anthropic-key", "candidate", "0.9", True, 0),
        ("sk-ant-api03-release-shaped-test", "candidate", None, True, 0),
        ("sk-ant-api03-release-shaped-test", "baseline", None, False, 1),
        ("sk-ant-api03-release-shaped-test", "candidate", "0.9", False, 1),
    ],
    ids=[
        "missing-key-blocked",
        "fixture-key-blocked",
        "candidate-threshold-missing-blocked",
        "baseline-without-threshold-proceeds",
        "candidate-with-threshold-proceeds",
    ],
)
async def test_live_eval_prerequisite_matrix_stops_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
    mode: str,
    threshold: str | None,
    blocked: bool,
    expected_calls: int,
) -> None:
    """Only complete live-eval configurations may cross the provider boundary."""

    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("DAILY_BRIEF_EVAL_MODE", mode)
    if threshold is None:
        monkeypatch.delenv("DAILY_BRIEF_CITATION_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("DAILY_BRIEF_CITATION_THRESHOLD", threshold)

    calls = 0

    async def fake_run_current_model() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal calls
        calls += 1
        return {}, {}

    monkeypatch.setattr(
        sys.modules[__name__], "_run_current_model", fake_run_current_model
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_quality_metrics",
        lambda _context, _run: {
            "claim_count": 1,
            "citation_count": 1,
            "unsupported_material_claims": 0,
            "live_gate_output_readiness": 1.0,
            "coverage_label_correctness": 1.0,
            "export_hash_reconstruction": 1.0,
            "citation_correctness": 1.0,
        },
    )

    if blocked:
        with pytest.raises(pytest.skip.Exception):
            await test_current_configured_model_meets_frozen_quality_gates()
    else:
        await test_current_configured_model_meets_frozen_quality_gates()
    assert calls == expected_calls


@pytest.mark.parametrize("threshold", ["not-a-number", "-0.01", "1.01"])
async def test_invalid_supplied_threshold_fails_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
    threshold: str,
) -> None:
    """A supplied candidate threshold must remain a valid frozen gate."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-release-shaped-test")
    monkeypatch.setenv("DAILY_BRIEF_EVAL_MODE", "candidate")
    monkeypatch.setenv("DAILY_BRIEF_CITATION_THRESHOLD", threshold)
    calls = 0

    async def fail_if_called() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal calls
        calls += 1
        return {}, {}

    monkeypatch.setattr(sys.modules[__name__], "_run_current_model", fail_if_called)

    with pytest.raises(pytest.fail.Exception):
        await test_current_configured_model_meets_frozen_quality_gates()
    assert calls == 0
