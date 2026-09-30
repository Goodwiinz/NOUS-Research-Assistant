"""Tests for the research engine ExportService."""

import copy
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.schemas.research_engine import ExportFormat
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
    rehydrate_stage_outputs,
    validate_envelope,
)
from src.services.research_engine.export_service import (
    ExportArtifact,
    ExportService,
    ResearchExportError,
)
from src.services.research_engine.observability import ResearchObservability
from src.services.research_engine.step_executor import StepExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_step(run_id, index, step_type="synthesize"):
    """Create a mock ResearchStep instance."""
    step = MagicMock()
    step.id = uuid4()
    step.run_id = run_id
    step.step_index = index
    step.step_type = step_type
    step.mode = "deterministic"
    step.model_id = "test-model-v1"
    step.model_version = "2026-09-01"
    step.temperature = 0.0
    step.seed = 42
    step.inputs_hash = "abc123"
    step.outputs_hash = "def456"
    step.full_prompt = "permitted system prompt"
    step.output = {"content": "synthesised output"}
    step.quality_marks = [{"check_type": "source_grounding", "passed": True}]
    step.token_count = 150
    return step


def _make_source(run_id):
    """Create a mock ResearchSource instance."""
    source = MagicMock()
    source.id = uuid4()
    source.run_id = run_id
    source.connector_type = "arxiv"
    source.external_id = "2301.00001"
    source.title = "A Test Paper"
    source.authors = ["Author A", "Author B"]
    source.abstract = "Abstract text."
    source.url = "https://arxiv.org/abs/2301.00001"
    source.content_hash = "hash123"
    return source


def _make_blueprint():
    """Create a mock ResearchBlueprint instance."""
    bp = MagicMock()
    bp.id = uuid4()
    bp.name = "Test Blueprint"
    bp.version = 1
    bp.template_source = "custom"
    return bp


def _make_run(status="completed", manifest=None):
    """Create a mock ResearchRun with related objects."""
    run_id = uuid4()
    run = MagicMock()
    run.id = run_id
    run.status = status
    run.started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    run.completed_at = datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    run.total_tokens = 500
    run.reproducibility_manifest = manifest

    bp = _make_blueprint()
    run.blueprint = bp
    run.blueprint_id = bp.id
    run.blueprint_version = bp.version

    step = _make_step(run_id, 0)
    run.steps = [step]

    source = _make_source(run_id)
    run.sources = [source]

    return run, step, source


def _mock_db_for_run(run, evidence_list=None):
    """Create an AsyncMock db session that returns the given run and evidence."""
    db = AsyncMock()

    # First execute call returns the run
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run

    # Second execute call returns evidence
    ev_result = MagicMock()
    ev_result.scalars.return_value.all.return_value = evidence_list or []

    db.execute = AsyncMock(side_effect=[run_result, ev_result])
    return db


def _mock_db_for_manifest(run):
    """Create an AsyncMock db for export_manifest calls."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = run
    db.execute = AsyncMock(return_value=result)
    return db


# ---------------------------------------------------------------------------
# Tests: owner-scoped export artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owned_export_returns_stable_not_found_error() -> None:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    with pytest.raises(ResearchExportError) as raised:
        await ExportService().export(uuid4(), uuid4(), ExportFormat.JSON, db)

    assert raised.value.status_code == 404
    assert raised.value.detail() == {
        "code": "run_not_found",
        "message": "Run not found",
    }


@pytest.mark.asyncio
async def test_owned_export_rejects_non_completed_run() -> None:
    run, _, _ = _make_run(status="running")
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = run
    db.execute.return_value = result

    with pytest.raises(ResearchExportError) as raised:
        await ExportService().export(run.id, uuid4(), ExportFormat.MARKDOWN, db)

    assert raised.value.status_code == 409
    assert raised.value.code == "run_not_completed"


# ---------------------------------------------------------------------------
# Tests: export_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_json_returns_correct_structure():
    """export_json must return a report with run info, steps, sources, evidence."""
    run, step, source = _make_run(status="completed")
    db = _mock_db_for_run(run)

    service = ExportService()
    report = await service.export_json(run.id, db)

    # Top-level fields
    assert report["run_id"] == str(run.id)
    assert report["status"] == "completed"
    assert report["started_at"] is not None
    assert report["completed_at"] is not None
    assert report["total_tokens"] == 500

    # Blueprint section
    assert "blueprint" in report
    assert report["blueprint"]["name"] == "Test Blueprint"
    assert report["blueprint"]["version"] == 1

    # Steps section
    assert len(report["steps"]) == 1
    step_data = report["steps"][0]
    assert step_data["step_index"] == 0
    assert step_data["step_type"] == "synthesize"
    assert step_data["inputs_hash"] == "abc123"
    assert step_data["outputs_hash"] == "def456"
    assert step_data["full_prompt"] == "permitted system prompt"
    assert step_data["model_id"] == "test-model-v1"
    assert step_data["model_version"] == "2026-09-01"
    assert step_data["token_count"] == 150

    # Evidence nested under step
    assert step_data["evidence"] == []  # R5-L19: no evidence writers yet

    # Sources section
    assert len(report["sources"]) == 1
    assert report["sources"][0]["title"] == "A Test Paper"
    assert report["sources"][0]["connector_type"] == "arxiv"

    # Evidence count
    assert report["evidence_count"] == 0  # R5-L19
    assert report["evidence_status"] == "unavailable_legacy"
    assert "unverified" in report["markdown"]


@pytest.mark.asyncio
async def test_export_json_raises_for_non_completed_run():
    """export_json must raise ValueError for a run that is not completed."""
    run, _, _ = _make_run(status="running")
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = run
    db.execute = AsyncMock(return_value=result)

    service = ExportService()
    with pytest.raises(ValueError, match="not completed"):
        await service.export_json(run.id, db)


@pytest.mark.asyncio
async def test_export_json_raises_for_missing_run():
    """export_json must raise ValueError if the run does not exist."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    service = ExportService()
    with pytest.raises(ValueError, match="not found"):
        await service.export_json(uuid4(), db)


@pytest.mark.asyncio
async def test_export_json_with_no_evidence():
    """export_json must work correctly when there is no evidence."""
    run, step, source = _make_run(status="completed")
    db = _mock_db_for_run(run, evidence_list=[])

    service = ExportService()
    report = await service.export_json(run.id, db)

    assert report["evidence_count"] == 0
    assert report["steps"][0]["evidence"] == []


@pytest.mark.asyncio
async def test_export_json_projects_typed_evidence_and_failed_checks():
    """Versioned envelopes produce honest JSON and readable failed-check output."""
    run, search_step, source = _make_run(status="completed")
    source_id = "11111111-1111-4111-8111-111111111111"
    search_step.step_type = "search"
    search_step.output = {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "source_records": [
            {
                "source_id": source_id,
                "connector_type": "arxiv",
                "title": "A Test Paper",
                "abstract": "The reported score did not improve.",
                "evidence_level": "abstract",
                "url": "https://example.test/paper",
            }
        ],
        "coverage": {"partial": False, "exhaustive": False},
        "selected_sources": ["arxiv"],
    }
    extraction_step = _make_step(run.id, 1, step_type="extract")
    extraction_step.output = {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": {"model_calls": 1, "total_tokens": 12, "batches": []},
        "extractions": [
            {
                "source_id": source_id,
                "part_id": "p0001",
                "data": {"finding": "The reported score did not improve."},
                "evidence": [
                    {
                        "pointer": "/finding",
                        "quote": "The reported score did not improve.",
                        "page_reference": None,
                        "evidence_id": "e0001",
                    }
                ],
            }
        ],
        "processing_coverage": {"1": {"complete": True}},
    }
    extraction_step.full_prompt = "[system instructions and batch hashes]"
    verify_step = _make_step(run.id, 2, step_type="verify")
    verify_step.output = {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 1, "total_tokens": 8, "batches": []},
        "verification": {
            "passed": False,
            "deterministic_passed": True,
            "schema_passed": True,
            "semantic_status": "failed",
            "claims": [
                {
                    "claim_id": "c0001",
                    "status": "contradicted",
                    "reason": "The excerpt says the result did not improve.",
                    "evidence_ids": ["e0001"],
                }
            ],
            "coverage_complete": True,
            "continued_after_failure": True,
        },
        "processing_coverage": {},
    }
    run.steps = [search_step, extraction_step, verify_step]
    db = _mock_db_for_run(run)

    report = await ExportService().export_json(run.id, db)

    assert report["contract_version"] == 1
    assert report["evidence_count"] == 1
    assert report["steps"][1]["evidence"][0]["evidence_id"] == "e0001"
    assert report["steps"][1]["full_prompt"] == "[system instructions and batch hashes]"
    assert report["verification"]["passed"] is False
    assert "Verification failed or is incomplete" in report["markdown"]
    assert "Failed verification checks" in report["markdown"]
    assert "contradicted" in report["markdown"]
    assert "The excerpt says the result did not improve\\." in report["markdown"]


# ---------------------------------------------------------------------------
# Tests: export_manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_manifest_returns_stored_manifest():
    """export_manifest must return the reproducibility_manifest from the run."""
    manifest = {
        "run_id": str(uuid4()),
        "blueprint_version": 1,
        "steps": [
            {"step_id": "s1", "inputs_hash": "aaa", "outputs_hash": "bbb"},
        ],
        "model_ids": ["test-model-v1"],
    }
    run, _, _ = _make_run(status="completed", manifest=manifest)
    db = _mock_db_for_manifest(run)

    service = ExportService()
    result = await service.export_manifest(run.id, db)

    assert result == manifest
    assert "run_id" in result
    assert "blueprint_version" in result
    assert "steps" in result
    assert "model_ids" in result


@pytest.mark.asyncio
async def test_export_manifest_raises_for_missing_run():
    """export_manifest must raise ValueError if the run does not exist."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    service = ExportService()
    with pytest.raises(ValueError, match="not found"):
        await service.export_manifest(uuid4(), db)


@pytest.mark.asyncio
async def test_export_manifest_raises_for_no_manifest():
    """export_manifest must raise ValueError if the run has no manifest."""
    run, _, _ = _make_run(status="completed", manifest=None)
    db = _mock_db_for_manifest(run)

    service = ExportService()
    with pytest.raises(ValueError, match="no reproducibility manifest"):
        await service.export_manifest(run.id, db)


def _daily_brief_context(*, verified: bool = True) -> tuple[dict, dict]:
    source_id = "11111111-1111-4111-8111-111111111111"
    verification = {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 1, "total_tokens": 8, "batches": []},
        "verification": {
            "passed": verified,
            "deterministic_passed": True,
            "schema_passed": True,
            "semantic_status": "supported" if verified else "failed",
            "claims": [
                {
                    "claim_id": "c0001",
                    "status": "supported" if verified else "contradicted",
                    "reason": "Grounded" if verified else "The evidence conflicts.",
                    "evidence_ids": ["e0001"],
                }
            ],
            "coverage_complete": True,
            "continued_after_failure": not verified,
        },
        "processing_coverage": {},
    }
    context = {
        "contract_version": 1,
        "query": "private question text",
        "scope_confirmation": {
            "research_question": "private question text",
            "inclusion_criteria": ["included"],
            "exclusion_criteria": ["excluded"],
            "providers": ["openalex"],
            "limit_per_provider": 25,
            "confirmed": True,
            "configuration_hash": "a" * 64,
            "actor_id": str(uuid4()),
            "confirmed_at": "2026-09-27T10:00:00+00:00",
        },
        "provider_manifest": [
            {
                "id": "openalex",
                "label": "OpenAlex",
                "daily_brief_eligible": True,
                "available": True,
                "features": {
                    "full_text": False,
                    "date_filter": False,
                    "cursor": False,
                },
            }
        ],
        "source_records": [
            {
                "source_id": source_id,
                "connector_type": "openalex",
                "title": "=Formula title",
                "authors": ["+Author"],
                "publication_year": 2026,
                "doi": "10.1000/example",
                "url": "https://example.test/paper",
                "evidence_level": "abstract",
            }
        ],
        "coverage": {
            "partial": False,
            "exhaustive": False,
            "provider_results": [{"provider": "openalex", "returned_count": 1}],
        },
        "screening": [
            {
                "source_id": source_id,
                "part_id": "p0001",
                "included": True,
                "reason": "matches",
            }
        ],
        "included_source_ids": [source_id],
        "extractions": [
            {
                "source_id": source_id,
                "part_id": "p0001",
                "data": {"finding": "-bounded finding"},
                "evidence_level": "abstract",
                "evidence": [
                    {
                        "evidence_id": "e0001",
                        "pointer": "/finding",
                        "quote": "@quoted evidence",
                        "page_reference": None,
                    }
                ],
            }
        ],
        "synthesis": {
            "sections": [
                {
                    "heading": "Finding",
                    "claims": [
                        {
                            "claim_id": "c0001",
                            "claim_text": "A bounded claim.",
                            "evidence": [
                                {"evidence_id": "e0001", "relation": "supports"}
                            ],
                        }
                    ],
                }
            ]
        },
        "verification": verification["verification"],
        "approved_review_overlays": [
            {
                "review_id": str(uuid4()),
                "step_index": 2,
                "review_kind": "extraction",
                "output_hash": "b" * 64,
                "decision_payload": {
                    "items": [
                        {
                            "source_id": source_id,
                            "part_id": "p0001",
                            "decision": "accept",
                        }
                    ]
                },
            }
        ],
        "stage_results": {"4": verification},
        "artifact_provenance": {
            "run_id": str(uuid4()),
            "blueprint_id": str(uuid4()),
            "blueprint_version": 3,
            "template_source": "daily_research_brief",
            "template_contract_version": 1,
            "started_at": "2026-09-27T10:00:00+00:00",
            "completed_at": None,
            "models": [{"step_index": 3, "model_id": "model-v1"}],
            "stage_hashes": {"4": canonical_stage_output_hash(verification)},
            "limitations": ["Bounded provider search; not exhaustive."],
        },
    }
    return context, verification


def test_trusted_scope_rejects_malformed_or_drifted_confirmation() -> None:
    configuration = {
        "contract_version": 1,
        "research_question": "bounded question",
        "inclusion_criteria": ["included"],
        "exclusion_criteria": ["excluded"],
        "providers": ["openalex"],
        "limit_per_provider": 25,
        "notes": None,
    }
    scope = {
        **configuration,
        "confirmed": True,
        "confirmed_by": str(uuid4()),
        "confirmed_at": "2026-09-27T10:00:00+00:00",
        "configuration_hash": canonical_json_sha256(configuration),
    }
    manifest = {
        "scope_confirmation": scope,
        "parameters": copy.deepcopy(configuration),
    }

    assert ExportService._trusted_scope(manifest) is True

    scope["confirmed_by"] = "not-a-uuid"
    assert ExportService._trusted_scope(manifest) is False
    scope["confirmed_by"] = str(uuid4())

    scope["confirmed_at"] = "2026-09-27T10:00:00"
    assert ExportService._trusted_scope(manifest) is False
    scope["confirmed_at"] = "2026-09-27T10:00:00+00:00"

    scope["configuration_hash"] = "0" * 64
    assert ExportService._trusted_scope(manifest) is False
    scope["configuration_hash"] = canonical_json_sha256(configuration)

    manifest["parameters"]["notes"] = "changed after approval"
    assert ExportService._trusted_scope(manifest) is False


@pytest.mark.asyncio
async def test_contract_export_is_model_free_and_binds_exact_verification_and_report() -> (
    None
):
    """Dropping either hash would let final review authorize a different report."""
    context, verification = _daily_brief_context()

    result = await StepExecutor(connectors={}, providers={}).execute(
        {
            "type": "export",
            "parameters": {"contract_version": 1, "format": "json"},
        },
        context,
    )

    output = result.output
    assert output["usage"] == {"model_calls": 0, "total_tokens": 0, "batches": []}
    assert output["verification_output_hash"] == canonical_stage_output_hash(
        verification
    )
    assert output["report_hash"] == canonical_json_sha256(output["exported"])
    assert output["exported"]["final_status"] == "verified"
    assert output["content"] == json.dumps(
        output["exported"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def test_export_contract_owns_hash_bindings_and_rehydrates_them() -> None:
    envelope = {
        "contract_version": 1,
        "stage_type": "export",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "format": "json",
        "exported": {"final_status": "verified"},
        "verification_output_hash": "a" * 64,
        "report_hash": "b" * 64,
    }

    validate_envelope(envelope, "export")
    context = rehydrate_stage_outputs([SimpleNamespace(step_index=0, output=envelope)])

    assert context["verification_output_hash"] == "a" * 64
    assert context["report_hash"] == "b" * 64


def test_rehydrate_accepts_explicit_empty_legacy_output() -> None:
    context = rehydrate_stage_outputs([SimpleNamespace(step_index=0, output={})])

    assert context == {}


def test_export_contract_still_rejects_undeclared_top_level_fields() -> None:
    envelope = {
        "contract_version": 1,
        "stage_type": "export",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "format": "json",
        "exported": {},
        "verification_output_hash": "a" * 64,
        "report_hash": "b" * 64,
        "question": "must never be export-owned metadata",
    }

    with pytest.raises(ValueError, match="unowned fields"):
        validate_envelope(envelope, "export")


@pytest.mark.asyncio
async def test_contract_export_preserves_failed_checks_and_unverified_status() -> None:
    """A failed verification check must survive the deterministic export step."""
    context, _verification = _daily_brief_context(verified=False)

    result = await StepExecutor(connectors={}, providers={}).execute(
        {
            "type": "export",
            "parameters": {"contract_version": 1, "format": "markdown"},
        },
        context,
    )

    artifact = result.output["exported"]
    assert artifact["final_status"] == "unverified"
    assert artifact["verification"]["claims"][0]["status"] == "contradicted"
    assert "UNVERIFIED" in result.output["markdown"]
    assert "contradicted" in result.output["markdown"]
    assert "The evidence conflicts" in result.output["markdown"]


@pytest.mark.asyncio
async def test_export_service_json_has_complete_daily_brief_provenance() -> None:
    """Removing a provenance family makes the portable audit incomplete."""
    context, verification = _daily_brief_context()
    run, _step, _source = _make_run(
        status="completed",
        manifest={
            **context["artifact_provenance"],
            "scope_confirmation": context["scope_confirmation"],
            "provider_manifest": context["provider_manifest"],
            "review_history": context["approved_review_overlays"],
            "final_status": "unverified",
        },
    )
    run.blueprint.template_source = "daily_research_brief"
    run.steps = []
    for index, (stage_type, output) in enumerate(
        [
            (
                "search",
                {
                    "contract_version": 1,
                    "stage_type": "search",
                    "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                    "source_records": context["source_records"],
                    "coverage": context["coverage"],
                    "selected_sources": ["openalex"],
                    "query": context["query"],
                },
            ),
            (
                "screen",
                {
                    "contract_version": 1,
                    "stage_type": "screen",
                    "usage": {"model_calls": 1, "total_tokens": 2, "batches": []},
                    "screening": context["screening"],
                    "included_source_ids": context["included_source_ids"],
                    "processing_coverage": {},
                },
            ),
            (
                "extract",
                {
                    "contract_version": 1,
                    "stage_type": "extract",
                    "usage": {"model_calls": 1, "total_tokens": 2, "batches": []},
                    "extractions": context["extractions"],
                    "processing_coverage": {},
                },
            ),
            (
                "synthesize",
                {
                    "contract_version": 1,
                    "stage_type": "synthesize",
                    "usage": {"model_calls": 1, "total_tokens": 2, "batches": []},
                    "synthesis": context["synthesis"],
                    "processing_coverage": {},
                },
            ),
            ("verify", verification),
        ]
    ):
        step = _make_step(run.id, index, step_type=stage_type)
        step.output = output
        step.outputs_hash = (
            "f" * 64 if index == 3 else canonical_stage_output_hash(output)
        )
        run.steps.append(step)
    db = _mock_db_for_run(run)

    artifact = await ExportService().export(run.id, uuid4(), ExportFormat.JSON, db)
    payload = json.loads(artifact.content)

    assert artifact.media_type == "application/json"
    assert artifact.filename == f"daily-research-brief-{run.id}.json"
    assert "private question text" not in artifact.filename
    assert payload["artifact_version"] == 1
    assert payload["contract_version"] == 1
    for key in (
        "run",
        "blueprint",
        "template",
        "scope",
        "providers",
        "deduplication",
        "stages",
        "reviews",
        "claims",
        "evidence",
        "verification",
        "models",
        "timestamps",
        "limitations",
    ):
        assert key in payload
    assert payload["final_status"] == "unverified"
    assert "UNVERIFIED" in payload["warning"]
    assert payload["coverage"]["exhaustive"] is False
    assert payload["stages"][3]["output_hash"] == canonical_stage_output_hash(
        run.steps[3].output
    )


@pytest.mark.asyncio
async def test_csv_has_stable_audit_fields_bounds_cells_and_neutralizes_formulas() -> (
    None
):
    """Spreadsheet formulas and unbounded extraction cells are export hazards."""
    context, _verification = _daily_brief_context()
    context["extractions"][0]["data"] = "-" + ("x" * 20000)
    row = {
        "source": context["source_records"][0],
        "extraction": context["extractions"][0],
        "decision": "reject",
        "reason": "\tunsafe reason",
    }

    from src.services.research_engine.report_rendering import render_csv

    rendered = render_csv([row], final_status="unverified")
    parsed = list(csv.DictReader(io.StringIO(rendered)))

    assert list(parsed[0]) == [
        "record_type",
        "artifact_status",
        "warning",
        "source_id",
        "bibliography",
        "extracted",
        "evidence",
        "evidence_level",
        "decision",
        "reason",
        "review_id",
        "reviewer_id",
        "review_kind",
        "reviewed_at",
        "review_output_hash",
        "review_payload",
        "review_note",
        "report_hash",
        "verification_output_hash",
        "attestation_hash",
    ]
    assert parsed[0]["artifact_status"] == "unverified"
    assert "UNVERIFIED" in parsed[0]["warning"]
    assert parsed[0]["bibliography"].startswith("'")
    assert parsed[0]["extracted"].startswith("'")
    assert parsed[0]["reason"].startswith("'")
    assert max(len(value) for value in parsed[0].values()) <= 8192


@pytest.mark.asyncio
async def test_csv_keeps_accepted_and_rejected_extraction_audit_rows() -> None:
    """Human rejections remain auditable even though reports exclude them."""
    context, _verification = _daily_brief_context()
    accepted = context["extractions"][0]
    rejected = copy.deepcopy(accepted)
    rejected["part_id"] = "p0002"
    rejected["data"] = {"finding": "rejected finding"}
    rejected["evidence"][0]["evidence_id"] = "e0002"
    context["extractions"].append(rejected)
    reviews = [
        {
            "review_id": str(uuid4()),
            "step_index": 2,
            "review_kind": "extraction",
            "output_hash": "b" * 64,
            "decision": "approve",
            "decision_payload": {
                "items": [
                    {
                        "source_id": accepted["source_id"],
                        "part_id": "p0001",
                        "decision": "accept",
                    },
                    {
                        "source_id": rejected["source_id"],
                        "part_id": "p0002",
                        "decision": "reject",
                        "reason": "Outside the confirmed criteria.",
                    },
                ]
            },
        }
    ]
    run, _step, _source = _make_run(
        status="completed",
        manifest={
            **context["artifact_provenance"],
            "review_history": reviews,
            "final_status": "unverified",
        },
    )
    run.blueprint.template_source = "daily_research_brief"
    run.reviews = []
    search = _make_step(run.id, 0, step_type="search")
    search.output = {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "source_records": context["source_records"],
        "coverage": context["coverage"],
        "selected_sources": ["openalex"],
        "query": context["query"],
    }
    extract = _make_step(run.id, 1, step_type="extract")
    extract.output = {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": {"model_calls": 1, "total_tokens": 2, "batches": []},
        "extractions": context["extractions"],
        "processing_coverage": {},
    }
    run.steps = [search, extract]

    artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.CSV, _mock_db_for_run(run)
    )
    rows = list(csv.DictReader(io.StringIO(artifact.content.decode("utf-8"))))

    assert [(row["decision"], row["reason"]) for row in rows] == [
        ("accept", ""),
        ("reject", "Outside the confirmed criteria."),
    ]


@pytest.mark.asyncio
async def test_legacy_completed_export_is_readable_but_never_approved_daily_brief() -> (
    None
):
    run, _step, _source = _make_run(status="completed")

    json_artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
    )
    markdown_artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.MARKDOWN, _mock_db_for_run(run)
    )
    csv_artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.CSV, _mock_db_for_run(run)
    )

    assert json.loads(json_artifact.content)["final_status"] == "unverified"
    assert b"UNVERIFIED" in markdown_artifact.content
    assert b"UNVERIFIED" in csv_artifact.content


@pytest.mark.asyncio
async def test_no_evidence_has_audit_exports_but_no_markdown_brief() -> None:
    run, _step, _source = _make_run(
        status="completed", manifest={"final_status": "no_evidence"}
    )
    run.blueprint.template_source = "daily_research_brief"

    json_artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
    )
    csv_artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.CSV, _mock_db_for_run(run)
    )

    assert json.loads(json_artifact.content)["final_status"] == "no_evidence"
    assert b"no_evidence" in csv_artifact.content
    with pytest.raises(ResearchExportError) as error:
        await ExportService().export(
            run.id,
            uuid4(),
            ExportFormat.MARKDOWN,
            _mock_db_for_run(run),
        )
    assert error.value.code == "brief_not_available_no_evidence"


def _trusted_daily_export_run(
    *, markdown: str = "# EXACT APPROVED ARTIFACT\n"
) -> MagicMock:
    """Build a completed Daily Brief with durable terminal trust evidence."""
    context, verification = _daily_brief_context()
    scope = copy.deepcopy(context["scope_confirmation"])
    scope.pop("actor_id", None)
    scope["confirmed_by"] = str(uuid4())
    configuration = {
        "contract_version": 1,
        **{
            key: scope.get(key)
            for key in (
                "research_question",
                "inclusion_criteria",
                "exclusion_criteria",
                "providers",
                "limit_per_provider",
                "notes",
            )
        },
    }
    scope["configuration_hash"] = canonical_json_sha256(configuration)
    report = {
        "artifact_version": 1,
        "contract_version": 1,
        "final_status": "verified",
        "verification": verification["verification"],
        "limitations": ["Bounded provider search; results are not exhaustive."],
    }
    export_output = {
        "contract_version": 1,
        "stage_type": "export",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "format": "markdown",
        "exported": report,
        "markdown": markdown,
        "content": markdown,
        "media_type": "text/markdown; charset=utf-8",
        "verification_output_hash": canonical_stage_output_hash(verification),
        "report_hash": canonical_json_sha256(report),
    }
    run, _step, _source = _make_run(
        status="completed",
        manifest={
            "final_status": "verified",
            "scope_confirmation": scope,
            "provider_manifest": context["provider_manifest"],
        },
    )
    run.blueprint.template_source = "daily_research_brief"
    run.blueprint.steps = [
        {"id": "verify", "type": "verify", "parameters": {"model_id": "verify-v1"}},
        {"id": "export", "type": "export", "parameters": {"review_gate": "final"}},
    ]
    verify_step = _make_step(run.id, 0, step_type="verify")
    verify_step.output = verification
    verify_step.outputs_hash = canonical_stage_output_hash(verification)
    export_step = _make_step(run.id, 1, step_type="export")
    export_step.output = export_output
    export_step.outputs_hash = canonical_stage_output_hash(export_output)
    run.steps = [verify_step, export_step]
    review_id = uuid4()
    reviewer_id = uuid4()
    reviewed_at = datetime(2026, 9, 27, 10, 10, tzinfo=timezone.utc)
    review = SimpleNamespace(
        id=review_id,
        reviewer_id=reviewer_id,
        step_index=1,
        stage_type="export",
        review_kind="final",
        output_hash=export_step.outputs_hash,
        decision="approve",
        decision_payload={},
        note=None,
        created_at=reviewed_at,
    )
    run.reviews = [review]
    unsigned_attestation = {
        "schema_version": 1,
        "review_id": str(review_id),
        "reviewer_id": str(reviewer_id),
        "reviewed_at": reviewed_at.isoformat(),
        "decision": "approve",
        "review_kind": "final",
        "step_index": 1,
        "output_hash": export_step.outputs_hash,
        "report_hash": export_output["report_hash"],
        "verification_output_hash": export_output["verification_output_hash"],
    }
    run.reproducibility_manifest["final_approval_attestation"] = {
        **unsigned_attestation,
        "attestation_hash": canonical_json_sha256(unsigned_attestation),
    }
    return cast(MagicMock, run)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# EXACT APPROVED ARTIFACT\n", b"# EXACT APPROVED ARTIFACT\n"),
        ("", b""),
    ],
)
async def test_markdown_download_uses_exact_persisted_approved_artifact_bytes(
    markdown: str, expected: bytes
) -> None:
    run = _trusted_daily_export_run(markdown=markdown)
    observer = ResearchObservability()

    artifact = await ExportService(observer=observer).export(
        run.id, uuid4(), ExportFormat.MARKDOWN, _mock_db_for_run(run)
    )

    assert artifact.content[: len(expected)] == expected
    assert b"DAILY_RESEARCH_BRIEF_POST_APPROVAL_AUDIT_V1" in artifact.content
    assert b'"reviewer_id"' in artifact.content
    assert b'"final_approval_attestation"' in artifact.content
    rendered = artifact.content.decode("utf-8")
    reviewed_prefix, audit_section = rendered.split(
        "\n<!-- DAILY_RESEARCH_BRIEF_POST_APPROVAL_AUDIT_V1 -->", 1
    )
    audit = json.loads(audit_section.split("```json\n", 1)[1].split("\n```", 1)[0])
    export_output = run.steps[-1].output
    attestation = audit["final_approval_attestation"]
    assert reviewed_prefix.encode("utf-8") == expected
    assert audit["reviewed_artifact"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert audit["reviewed_artifact"]["report_hash"] == canonical_json_sha256(
        export_output["exported"]
    )
    assert attestation["report_hash"] == export_output["report_hash"]
    assert (
        attestation["verification_output_hash"]
        == export_output["verification_output_hash"]
    )
    assert attestation["output_hash"] == canonical_stage_output_hash(export_output)
    mutated_output = copy.deepcopy(export_output)
    mutated_output["content"] += "tampered"
    assert canonical_stage_output_hash(mutated_output) != attestation["output_hash"]
    assert observer.snapshot()["counters"]["exports"]["markdown:success:none"] == 1


@pytest.mark.asyncio
async def test_export_reconstruction_gap_returns_stable_content_free_error() -> None:
    run, _step, _source = _make_run(status="completed")
    first = _make_step(run.id, 0, step_type="search")
    first.output = {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "source_records": [],
        "coverage": {"exhaustive": False},
        "selected_sources": ["openalex"],
    }
    third = _make_step(run.id, 2, step_type="verify")
    third.output = {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "verification": {"passed": False, "claims": []},
        "processing_coverage": {},
    }
    run.steps = [first, third]
    observer = ResearchObservability()

    with pytest.raises(ResearchExportError) as raised:
        await ExportService(observer=observer).export(
            run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
        )

    assert raised.value.status_code == 500
    assert raised.value.code == "export_reconstruction_failed"
    assert "PRIVATE" not in raised.value.message
    assert run.status == "completed"
    metrics = observer.snapshot()["counters"]
    assert metrics["rehydration_errors"]["invalid_history"] == 1
    assert metrics["exports"]["json:failed:reconstruction"] == 1


@pytest.mark.asyncio
async def test_legacy_json_export_refuses_noncontiguous_persisted_history() -> None:
    run, _step, _source = _make_run(status="completed")
    first = _make_step(run.id, 0, step_type="search")
    first.output = {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "source_records": [],
        "coverage": {"exhaustive": False},
        "selected_sources": ["openalex"],
    }
    third = _make_step(run.id, 2, step_type="verify")
    third.output = {
        "contract_version": 1,
        "stage_type": "verify",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "verification": {"passed": False, "claims": []},
        "processing_coverage": {},
    }
    run.steps = [first, third]
    observer = ResearchObservability()

    with pytest.raises(ResearchExportError) as raised:
        await ExportService(observer=observer).export_json(
            run.id, _mock_db_for_run(run)
        )

    assert raised.value.code == "export_reconstruction_failed"
    assert raised.value.status_code == 500
    assert run.status == "completed"
    metrics = observer.snapshot()["counters"]
    assert metrics["rehydration_errors"]["invalid_history"] == 1
    assert metrics["exports"]["json:failed:reconstruction"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_output", [None, [], "persisted corruption"])
@pytest.mark.parametrize("exporter", ["v1", "legacy"])
async def test_exporters_reject_malformed_contiguous_persisted_output(
    malformed_output: object,
    exporter: str,
) -> None:
    run, step, _source = _make_run(status="completed")
    step.output = malformed_output
    run.steps = [step]
    observer = ResearchObservability()
    service = ExportService(observer=observer)

    with pytest.raises(ResearchExportError) as raised:
        if exporter == "v1":
            await service.export(
                run.id,
                uuid4(),
                ExportFormat.JSON,
                _mock_db_for_run(run),
            )
        else:
            await service.export_json(run.id, _mock_db_for_run(run))

    assert raised.value.status_code == 500
    assert raised.value.code == "export_reconstruction_failed"
    assert run.status == "completed"
    metrics = observer.snapshot()["counters"]
    assert metrics["rehydration_errors"]["invalid_history"] == 1
    assert metrics["exports"]["json:failed:reconstruction"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exporter", ["v1", "legacy"])
@pytest.mark.parametrize(
    ("persisted_output", "accepted"),
    [
        pytest.param({}, True, id="explicit-empty-legacy-output"),
        pytest.param(
            {"contract_version": 1},
            False,
            id="missing-stage-type",
        ),
        pytest.param(
            {"stage_type": "search"},
            False,
            id="missing-contract-version",
        ),
        pytest.param(
            {"contract_version": 1, "stage_type": "unknown"},
            False,
            id="unknown-stage-type",
        ),
        pytest.param(
            {"contract_version": 1, "stage_type": 7},
            False,
            id="non-string-stage-type",
        ),
        pytest.param(
            {"contract_version": 1, "stage_type": []},
            False,
            id="unhashable-stage-type",
        ),
        pytest.param(
            {
                "contract_version": 1,
                "stage_type": "search",
                "usage": {"model_calls": "one", "total_tokens": 0, "batches": []},
                "source_records": [],
                "coverage": {"exhaustive": False},
                "selected_sources": [],
            },
            False,
            id="malformed-envelope-fields",
        ),
    ],
)
async def test_exporters_validate_canonical_looking_persisted_outputs(
    persisted_output: dict[str, object],
    accepted: bool,
    exporter: str,
) -> None:
    run, step, _source = _make_run(status="completed")
    step.output = copy.deepcopy(persisted_output)
    run.steps = [step]
    observer = ResearchObservability()
    service = ExportService(observer=observer)

    async def export() -> object:
        if exporter == "v1":
            return await service.export(
                run.id,
                uuid4(),
                ExportFormat.JSON,
                _mock_db_for_run(run),
            )
        return await service.export_json(run.id, _mock_db_for_run(run))

    if accepted:
        assert await export()
        assert run.status == "completed"
        return

    with pytest.raises(ResearchExportError) as raised:
        await export()

    assert raised.value.status_code == 500
    assert raised.value.code == "export_reconstruction_failed"
    assert run.status == "completed"
    metrics = observer.snapshot()["counters"]
    assert metrics["rehydration_errors"]["invalid_history"] == 1
    assert metrics["exports"]["json:failed:reconstruction"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["terminal_status", "scope", "final_review"])
async def test_verified_daily_export_requires_all_durable_trust_evidence(
    missing: str,
) -> None:
    run = _trusted_daily_export_run()
    if missing == "terminal_status":
        run.reproducibility_manifest.pop("final_status")
    elif missing == "scope":
        run.reproducibility_manifest.pop("scope_confirmation")
    else:
        run.reviews = []

    if missing == "terminal_status":
        artifact = await ExportService().export(
            run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
        )
        payload = json.loads(artifact.content)
        assert payload["final_status"] == "unverified"
    else:
        with pytest.raises(ResearchExportError) as raised:
            await ExportService().export(
                run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
            )
        assert raised.value.code == "verified_artifact_attestation_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "json_format",
        "missing_markdown",
        "non_string_markdown",
        "missing_content",
        "non_string_content",
        "mismatched_content",
    ],
)
async def test_verified_markdown_requires_exact_canonical_persisted_bytes(
    corruption: str,
) -> None:
    run = _trusted_daily_export_run(markdown="# REVIEWED MARKDOWN\n")
    export_step = run.steps[-1]
    output = export_step.output
    if corruption == "json_format":
        output["format"] = "json"
    elif corruption == "missing_markdown":
        output.pop("markdown")
    elif corruption == "non_string_markdown":
        output["markdown"] = {"not": "bytes"}
    elif corruption == "missing_content":
        output.pop("content")
    elif corruption == "non_string_content":
        output["content"] = ["not", "bytes"]
    else:
        output["content"] = "# DIFFERENT MARKDOWN\n"
    export_step.outputs_hash = canonical_stage_output_hash(output)
    run.reviews[0].output_hash = export_step.outputs_hash

    with pytest.raises(ResearchExportError) as raised:
        await ExportService().export(
            run.id,
            uuid4(),
            ExportFormat.MARKDOWN,
            _mock_db_for_run(run),
        )
    assert raised.value.code == "verified_artifact_attestation_invalid"


@pytest.mark.asyncio
async def test_verified_json_wraps_immutable_report_with_complete_approval_audit() -> (
    None
):
    run = _trusted_daily_export_run()
    original_report = copy.deepcopy(run.steps[-1].output["exported"])

    artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
    )
    payload = json.loads(artifact.content)

    assert payload["download_envelope_version"] == 1
    assert payload["reviewed_artifact"] == original_report
    assert (
        canonical_json_sha256(payload["reviewed_artifact"])
        == payload["final_approval_attestation"]["report_hash"]
    )
    assert payload["review_history"][-1]["reviewer_id"] == str(
        run.reviews[-1].reviewer_id
    )
    assert payload["final_approval_attestation"]["review_id"] == str(run.reviews[-1].id)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "wrong_review", "wrong_hash"])
async def test_verified_download_fails_closed_on_missing_or_wrong_attestation(
    corruption: str,
) -> None:
    run = _trusted_daily_export_run()
    if corruption == "missing":
        run.reproducibility_manifest.pop("final_approval_attestation")
    elif corruption == "wrong_review":
        run.reproducibility_manifest["final_approval_attestation"]["review_id"] = str(
            uuid4()
        )
    else:
        run.reproducibility_manifest["final_approval_attestation"]["report_hash"] = (
            "0" * 64
        )

    with pytest.raises(ResearchExportError) as raised:
        await ExportService().export(
            run.id, uuid4(), ExportFormat.JSON, _mock_db_for_run(run)
        )

    assert raised.value.code == "verified_artifact_attestation_invalid"


@pytest.mark.asyncio
async def test_verified_csv_exposes_complete_review_and_final_attestation() -> None:
    run = _trusted_daily_export_run()

    artifact = await ExportService().export(
        run.id, uuid4(), ExportFormat.CSV, _mock_db_for_run(run)
    )
    rows = list(csv.DictReader(io.StringIO(artifact.content.decode("utf-8"))))

    review_row = next(row for row in rows if row["record_type"] == "review")
    attestation_row = next(
        row for row in rows if row["record_type"] == "final_attestation"
    )
    assert review_row["review_id"] == str(run.reviews[-1].id)
    assert review_row["reviewer_id"] == str(run.reviews[-1].reviewer_id)
    assert attestation_row["review_output_hash"] == run.steps[-1].outputs_hash
    assert attestation_row["report_hash"] == run.steps[-1].output["report_hash"]
    assert (
        attestation_row["verification_output_hash"]
        == run.steps[-1].output["verification_output_hash"]
    )


@pytest.mark.asyncio
async def test_csv_review_projection_is_order_independent_and_extraction_owned() -> (
    None
):
    context, _verification = _daily_brief_context()
    source_id = context["extractions"][0]["source_id"]
    reviews = [
        SimpleNamespace(
            id=uuid4(),
            step_index=1,
            review_kind="screening",
            output_hash="a" * 64,
            decision="approve",
            decision_payload={
                "items": [
                    {
                        "source_id": source_id,
                        "part_id": "p0001",
                        "decision": "include",
                        "reason": "screening reason",
                    }
                ]
            },
            created_at=datetime(2026, 9, 27, 10, 1, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id=uuid4(),
            step_index=2,
            review_kind="extraction",
            output_hash="b" * 64,
            decision="approve",
            decision_payload={
                "items": [
                    {
                        "source_id": source_id,
                        "part_id": "p0001",
                        "decision": "reject",
                        "reason": "extraction reason",
                    }
                ]
            },
            created_at=datetime(2026, 9, 27, 10, 2, tzinfo=timezone.utc),
        ),
    ]

    async def rendered(review_rows: list[SimpleNamespace]) -> ExportArtifact:
        run, _step, _source = _make_run(
            status="completed", manifest={"final_status": "unverified"}
        )
        run.blueprint.template_source = "daily_research_brief"
        search = _make_step(run.id, 0, step_type="search")
        search.output = {
            "contract_version": 1,
            "stage_type": "search",
            "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
            "source_records": context["source_records"],
            "coverage": context["coverage"],
            "selected_sources": ["openalex"],
        }
        extract = _make_step(run.id, 1, step_type="extract")
        extract.output = {
            "contract_version": 1,
            "stage_type": "extract",
            "usage": {"model_calls": 1, "total_tokens": 2, "batches": []},
            "extractions": context["extractions"],
            "processing_coverage": {},
        }
        run.steps = [search, extract]
        run.reviews = review_rows
        return await ExportService().export(
            run.id, uuid4(), ExportFormat.CSV, _mock_db_for_run(run)
        )

    forward = await rendered(reviews)
    reverse = await rendered(list(reversed(reviews)))
    row = list(csv.DictReader(io.StringIO(forward.content.decode())))[0]

    assert forward.content == reverse.content
    assert (row["decision"], row["reason"]) == ("reject", "extraction reason")


@pytest.mark.asyncio
async def test_post_resume_export_has_complete_deterministic_provenance() -> None:
    context, verification = _daily_brief_context()
    context["artifact_provenance"]["stage_hashes"] = {"0": "a" * 64}
    context["artifact_provenance"]["models"] = [
        {"step_index": 1, "model_id": "screen-v1"},
        {"step_index": 2, "model_id": "extract-v1"},
        {"step_index": 3, "model_id": "synthesize-v1"},
        {"step_index": 4, "model_id": "verify-v1"},
    ]
    context["artifact_provenance"]["generated_at"] = "2026-09-27T10:05:00+00:00"
    context["deduplication"] = {"before": 3, "after": 2}
    context["approved_review_overlays"][0]["created_at"] = "2026-09-27T10:04:00+00:00"
    context["stage_results"] = {
        "0": {
            "contract_version": 1,
            "stage_type": "search",
            "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
            "source_records": context["source_records"],
            "coverage": context["coverage"],
            "selected_sources": ["openalex"],
        },
        "1": {
            "contract_version": 1,
            "stage_type": "screen",
            "usage": {"model_calls": 1, "total_tokens": 1, "batches": []},
            "screening": context["screening"],
            "included_source_ids": context["included_source_ids"],
            "processing_coverage": {},
        },
        "2": {
            "contract_version": 1,
            "stage_type": "extract",
            "usage": {"model_calls": 1, "total_tokens": 1, "batches": []},
            "extractions": context["extractions"],
            "processing_coverage": {},
        },
        "3": {
            "contract_version": 1,
            "stage_type": "synthesize",
            "usage": {"model_calls": 1, "total_tokens": 1, "batches": []},
            "synthesis": context["synthesis"],
            "processing_coverage": {},
        },
        "4": verification,
    }

    result = await StepExecutor(connectors={}, providers={}).execute(
        {"type": "export", "parameters": {"contract_version": 1, "format": "markdown"}},
        context,
    )
    report = result.output["exported"]

    assert [item["step_index"] for item in report["stages"]] == [0, 1, 2, 3, 4]
    assert all(item["output_hash"] for item in report["stages"])
    assert report["deduplication"] == {"before": 3, "after": 2}
    assert len(report["models"]) == 4
    assert report["timestamps"]["generated_at"] == "2026-09-27T10:05:00+00:00"
    assert report["reviews"][0]["created_at"] == "2026-09-27T10:04:00+00:00"
    assert report["coverage"]["exhaustive"] is False
    assert "## Limitations" in result.output["markdown"]
    assert "## Provenance" in result.output["markdown"]
    assert all(item["stage_type"] != "export" for item in report["stages"])
