"""
Unit tests for Research Engine data models.

Tests the active research engine SQLAlchemy models:
ResearchProject, ResearchBlueprint, ResearchRun,
ResearchStep, and ResearchSource.
"""

import importlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_source import ResearchSource
from src.models.research_step import ExecutionMode, ResearchStep, StepType


def _review_model():
    module = importlib.import_module("src.models.research_stage_review")
    return module.ResearchStageReview


class TestRunStatusEnum:
    """Tests for RunStatus enum."""

    def test_values(self):
        assert RunStatus.PENDING.value == "pending"
        assert RunStatus.RUNNING.value == "running"
        assert RunStatus.PAUSED.value == "paused"
        assert RunStatus.COMPLETED.value == "completed"
        assert RunStatus.FAILED.value == "failed"

    def test_member_count(self):
        assert len(RunStatus) == 5


class TestStepTypeEnum:
    """Tests for StepType enum."""

    def test_values(self):
        assert StepType.SEARCH.value == "search"
        assert StepType.SCREEN.value == "screen"
        assert StepType.EXTRACT.value == "extract"
        assert StepType.SYNTHESIZE.value == "synthesize"
        assert StepType.VERIFY.value == "verify"
        assert StepType.EXPORT.value == "export"

    def test_member_count(self):
        assert len(StepType) == 6


class TestExecutionModeEnum:
    """Tests for ExecutionMode enum."""

    def test_values(self):
        assert ExecutionMode.DETERMINISTIC.value == "deterministic"
        assert ExecutionMode.EXPLORATORY.value == "exploratory"

    def test_member_count(self):
        assert len(ExecutionMode) == 2


class TestResearchProject:
    """Tests for ResearchProject model."""

    def test_creation(self):
        owner_id = uuid4()
        project = ResearchProject(
            name="Test Project",
            description="A test research project",
            owner_id=owner_id,
            status="active",
            settings={"key": "value"},
        )
        assert project.name == "Test Project"
        assert project.description == "A test research project"
        assert project.owner_id == owner_id
        assert project.status == "active"
        assert project.settings == {"key": "value"}

    def test_defaults(self):
        project = ResearchProject(
            name="Defaults Test",
            owner_id=uuid4(),
        )
        assert project.status == "active"
        assert project.settings is None
        assert project.description is None

    def test_tablename(self):
        assert ResearchProject.__tablename__ == "research_projects"

    def test_to_dict(self):
        pid = uuid4()
        owner_id = uuid4()
        project = ResearchProject(
            id=pid,
            name="Dict Test",
            owner_id=owner_id,
        )
        result = project.to_dict()
        assert result["name"] == "Dict Test"
        assert result["owner_id"] == owner_id


class TestResearchBlueprint:
    """Tests for ResearchBlueprint model."""

    def test_creation(self):
        project_id = uuid4()
        bp = ResearchBlueprint(
            project_id=project_id,
            name="Blueprint 1",
            template_source="manual",
            version=1,
            steps=[{"type": "search"}],
            parameters={"depth": 3},
            is_immutable=False,
        )
        assert bp.project_id == project_id
        assert bp.name == "Blueprint 1"
        assert bp.template_source == "manual"
        assert bp.version == 1
        assert bp.steps == [{"type": "search"}]
        assert bp.parameters == {"depth": 3}
        assert bp.is_immutable is False

    def test_defaults(self):
        bp = ResearchBlueprint(
            project_id=uuid4(),
            name="Defaults",
        )
        assert bp.version == 1
        assert bp.is_immutable is False
        assert bp.steps == []
        assert bp.parameters == {}

    def test_tablename(self):
        assert ResearchBlueprint.__tablename__ == "research_blueprints"


class TestResearchRun:
    """Tests for ResearchRun model."""

    def test_creation(self):
        blueprint_id = uuid4()
        run = ResearchRun(
            blueprint_id=blueprint_id,
            blueprint_version=2,
            status="running",
            total_tokens=500,
        )
        assert run.blueprint_id == blueprint_id
        assert run.blueprint_version == 2
        assert run.status == "running"
        assert run.total_tokens == 500

    def test_defaults(self):
        run = ResearchRun(
            blueprint_id=uuid4(),
            blueprint_version=1,
        )
        assert run.status == "pending"
        assert run.total_tokens == 0
        assert run.started_at is None
        assert run.completed_at is None
        assert run.reproducibility_manifest is None

    def test_tablename(self):
        assert ResearchRun.__tablename__ == "research_runs"

    def test_exposes_append_only_stage_reviews_relationship(self):
        assert ResearchRun.reviews.property.back_populates == "run"


class TestResearchStep:
    """Tests for ResearchStep model."""

    def test_creation(self):
        run_id = uuid4()
        step = ResearchStep(
            run_id=run_id,
            step_index=0,
            step_type="search",
            mode="deterministic",
            inputs_hash="a" * 64,
            outputs_hash="b" * 64,
            full_prompt="Search for papers on AI",
            model_id="gpt-4",
            model_version="0613",
            temperature=0.0,
            seed=42,
            output={"results": []},
            quality_marks={"score": 0.95},
            token_count=100,
        )
        assert step.run_id == run_id
        assert step.step_index == 0
        assert step.step_type == "search"
        assert step.mode == "deterministic"
        assert step.inputs_hash == "a" * 64
        assert step.outputs_hash == "b" * 64
        assert step.full_prompt == "Search for papers on AI"
        assert step.model_id == "gpt-4"
        assert step.model_version == "0613"
        assert step.temperature == 0.0
        assert step.seed == 42
        assert step.output == {"results": []}
        assert step.quality_marks == {"score": 0.95}
        assert step.token_count == 100

    def test_defaults(self):
        step = ResearchStep(
            run_id=uuid4(),
            step_index=0,
            step_type="search",
        )
        assert step.mode == "deterministic"
        assert step.temperature == 0.0
        assert step.token_count == 0
        assert step.started_at is None
        assert step.completed_at is None

    def test_tablename(self):
        assert ResearchStep.__tablename__ == "research_steps"


class TestResearchSource:
    """Tests for ResearchSource model."""

    def test_creation(self):
        run_id = uuid4()
        source = ResearchSource(
            run_id=run_id,
            connector_type="arxiv",
            external_id="2301.00001",
            title="A Great Paper",
            authors=[{"name": "John Doe"}],
            abstract="This paper explores...",
            url="https://arxiv.org/abs/2301.00001",
            metadata_={"category": "cs.AI"},
            content_hash="c" * 64,
        )
        assert source.run_id == run_id
        assert source.connector_type == "arxiv"
        assert source.external_id == "2301.00001"
        assert source.title == "A Great Paper"
        assert source.authors == [{"name": "John Doe"}]
        assert source.abstract == "This paper explores..."
        assert source.url == "https://arxiv.org/abs/2301.00001"
        assert source.metadata_ == {"category": "cs.AI"}
        assert source.content_hash == "c" * 64

    def test_tablename(self):
        assert ResearchSource.__tablename__ == "research_sources"


class TestResearchStageReview:
    """Tests for the append-only human-review ledger model."""

    def test_uses_plain_base_and_only_review_ledger_columns(self):
        from src.models.base import Base, BaseModel

        review_model = _review_model()

        assert issubclass(review_model, Base)
        assert not issubclass(review_model, BaseModel)
        assert set(review_model.__table__.columns.keys()) == {
            "id",
            "owner_id",
            "organization_id",
            "run_id",
            "step_index",
            "stage_type",
            "review_kind",
            "reviewer_id",
            "output_hash",
            "decision",
            "decision_payload",
            "note",
            "created_at",
        }
        assert not hasattr(review_model, "soft_delete")

    def test_creation_preserves_review_audit_values(self):
        review_model = _review_model()
        owner_id = uuid4()
        organization_id = uuid4()
        run_id = uuid4()
        reviewer_id = uuid4()
        created_at = datetime.now(timezone.utc)

        review = review_model(
            owner_id=owner_id,
            organization_id=organization_id,
            run_id=run_id,
            step_index=2,
            stage_type="extract",
            review_kind="extraction",
            reviewer_id=reviewer_id,
            output_hash="a" * 64,
            decision="approve",
            decision_payload={"records": [{"id": "record-1", "decision": "accept"}]},
            note="Checked against the evidence.",
            created_at=created_at,
        )

        assert review.owner_id == owner_id
        assert review.organization_id == organization_id
        assert review.run_id == run_id
        assert review.step_index == 2
        assert review.stage_type == "extract"
        assert review.review_kind == "extraction"
        assert review.reviewer_id == reviewer_id
        assert review.output_hash == "a" * 64
        assert review.decision == "approve"
        assert review.decision_payload["records"][0]["id"] == "record-1"
        assert review.note == "Checked against the evidence."
        assert review.created_at == created_at

    def test_created_at_default_is_timezone_aware_utc(self):
        review_model = _review_model()
        default = review_model.__table__.c.created_at.default

        assert default is not None
        created_at = default.arg(None)
        assert created_at.tzinfo is not None
        assert created_at.utcoffset() == timezone.utc.utcoffset(created_at)

    def test_table_declares_owner_review_indexes_and_unique_gate(self):
        review_model = _review_model()
        table = review_model.__table__
        index_columns = {
            tuple(column.name for column in index.columns) for index in table.indexes
        }
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }

        assert ("run_id", "step_index") in index_columns
        assert ("owner_id",) in index_columns
        assert ("reviewer_id",) in index_columns
        assert ("organization_id",) in index_columns
        assert (
            "run_id",
            "step_index",
            "output_hash",
            "review_kind",
        ) in unique_columns

    def test_is_exported_from_models_package(self):
        models = importlib.import_module("src.models")
        assert models.ResearchStageReview is _review_model()
