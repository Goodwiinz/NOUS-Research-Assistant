"""Tests for the research engine ExportService."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.services.research_engine.export_service import ExportService

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
