"""Tests for the research engine ExportService."""

import copy
import csv
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
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
    ExportService,
    ResearchExportError,
)
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
    step.temperature = 0.0
    step.seed = 42
    step.inputs_hash = "abc123"
    step.outputs_hash = "def456"
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
            "final_status": "verified",
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
        step.outputs_hash = canonical_stage_output_hash(output)
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
    assert payload["final_status"] == "verified"
    assert payload["coverage"]["exhaustive"] is False


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
        "artifact_status",
        "warning",
        "source_id",
        "bibliography",
        "extracted",
        "evidence",
        "evidence_level",
        "decision",
        "reason",
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
            "final_status": "verified",
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
