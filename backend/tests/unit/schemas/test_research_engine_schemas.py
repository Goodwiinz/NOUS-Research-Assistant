"""Tests for research engine Pydantic schemas."""

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas.research_engine as research_schemas
from src.schemas.research_engine import (
    BlueprintCreate,
    BlueprintResponse,
    BlueprintStepDefinition,
    BlueprintUpdate,
    EvidenceResponse,
    ExecutionMode,
    GroundingStatus,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    QualityMark,
    RunCreate,
    RunResponse,
    RunStatus,
    SourceResponse,
    StepResponse,
    StepType,
)

# ============================================================================
# Enum Tests
# ============================================================================


class TestStepType:
    def test_values(self):
        assert StepType.SEARCH == "search"
        assert StepType.SCREEN == "screen"
        assert StepType.EXTRACT == "extract"
        assert StepType.SYNTHESIZE == "synthesize"
        assert StepType.VERIFY == "verify"
        assert StepType.EXPORT == "export"

    def test_all_values(self):
        assert len(StepType) == 6


class TestExecutionMode:
    def test_values(self):
        assert ExecutionMode.DETERMINISTIC == "deterministic"
        assert ExecutionMode.EXPLORATORY == "exploratory"

    def test_all_values(self):
        assert len(ExecutionMode) == 2


class TestRunStatus:
    def test_values(self):
        assert RunStatus.PENDING == "pending"
        assert RunStatus.RUNNING == "running"
        assert RunStatus.PAUSED == "paused"
        assert RunStatus.COMPLETED == "completed"
        assert RunStatus.FAILED == "failed"

    def test_all_values(self):
        assert len(RunStatus) == 5


class TestGroundingStatus:
    def test_values(self):
        assert GroundingStatus.VERIFIED == "verified"
        assert GroundingStatus.UNVERIFIED == "unverified"
        assert GroundingStatus.FAILED == "failed"

    def test_all_values(self):
        assert len(GroundingStatus) == 3


# ============================================================================
# Project Schema Tests
# ============================================================================


class TestProjectCreate:
    def test_valid_minimal(self):
        p = ProjectCreate(name="Test Project")
        assert p.name == "Test Project"
        assert p.description is None
        assert p.settings == {}

    def test_valid_full(self):
        p = ProjectCreate(
            name="My Project",
            description="A description",
            settings={"key": "value"},
        )
        assert p.name == "My Project"
        assert p.description == "A description"
        assert p.settings == {"key": "value"}

    def test_name_required(self):
        with pytest.raises(ValidationError):
            ProjectCreate()

    def test_name_min_length(self):
        with pytest.raises(ValidationError):
            ProjectCreate(name="")

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            ProjectCreate(name="x" * 256)

    def test_name_at_max_length(self):
        p = ProjectCreate(name="x" * 255)
        assert len(p.name) == 255


class TestProjectUpdate:
    def test_all_optional(self):
        p = ProjectUpdate()
        assert p.name is None
        assert p.description is None
        assert p.status is None
        assert p.settings is None

    def test_partial_update(self):
        p = ProjectUpdate(name="Updated")
        assert p.name == "Updated"
        assert p.description is None


class TestProjectResponse:
    def test_from_attributes(self):
        assert ProjectResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        now = datetime.utcnow()
        uid = uuid4()
        p = ProjectResponse(
            id=uid,
            name="Project",
            description="Desc",
            status="active",
            settings={},
            created_at=now,
            updated_at=now,
        )
        assert p.id == uid
        assert p.name == "Project"


# ============================================================================
# Blueprint Schema Tests
# ============================================================================


class TestBlueprintStepDefinition:
    def test_valid_minimal(self):
        s = BlueprintStepDefinition(type=StepType.SEARCH, name="Search step")
        assert s.type == StepType.SEARCH
        assert s.name == "Search step"
        assert s.parameters == {}
        assert s.mode == ExecutionMode.DETERMINISTIC
        assert s.temperature == 0.0

    def test_valid_full(self):
        s = BlueprintStepDefinition(
            type=StepType.EXTRACT,
            name="Extract",
            description="Extract data",
            parameters={"k": "v"},
            model_id="gpt-4",
            model_version="0613",
            mode=ExecutionMode.EXPLORATORY,
            temperature=1.5,
            seed=42,
            system_prompt_template="You are a helpful assistant.",
        )
        assert s.model_id == "gpt-4"
        assert s.seed == 42
        assert s.temperature == 1.5

    def test_name_required(self):
        with pytest.raises(ValidationError):
            BlueprintStepDefinition(type=StepType.SEARCH)

    def test_name_empty(self):
        with pytest.raises(ValidationError):
            BlueprintStepDefinition(type=StepType.SEARCH, name="")

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            BlueprintStepDefinition(type=StepType.SEARCH, name="x" * 256)

    def test_temperature_min(self):
        with pytest.raises(ValidationError):
            BlueprintStepDefinition(type=StepType.SEARCH, name="s", temperature=-0.1)

    def test_temperature_max(self):
        with pytest.raises(ValidationError):
            BlueprintStepDefinition(type=StepType.SEARCH, name="s", temperature=2.1)

    def test_temperature_boundaries(self):
        s0 = BlueprintStepDefinition(type=StepType.SEARCH, name="s", temperature=0.0)
        s2 = BlueprintStepDefinition(type=StepType.SEARCH, name="s", temperature=2.0)
        assert s0.temperature == 0.0
        assert s2.temperature == 2.0


class TestBlueprintCreate:
    def _step(self, name="Step 1"):
        return BlueprintStepDefinition(type=StepType.SEARCH, name=name)

    def test_valid_minimal(self):
        b = BlueprintCreate(name="Blueprint", steps=[self._step()])
        assert b.name == "Blueprint"
        assert len(b.steps) == 1
        assert b.parameters == {}
        assert b.template_source is None

    def test_valid_full(self):
        b = BlueprintCreate(
            name="BP",
            template_source="arxiv-slr",
            steps=[self._step("A"), self._step("B")],
            parameters={"depth": 3},
        )
        assert len(b.steps) == 2
        assert b.template_source == "arxiv-slr"

    def test_empty_steps_rejected(self):
        with pytest.raises(ValidationError):
            BlueprintCreate(name="BP", steps=[])

    def test_name_required(self):
        with pytest.raises(ValidationError):
            BlueprintCreate(steps=[self._step()])

    def test_step_count_is_server_bounded(self):
        with pytest.raises(ValidationError):
            BlueprintCreate(
                name="BP",
                steps=[self._step(str(index)) for index in range(33)],
            )

    def test_nested_parameters_are_server_bounded(self):
        with pytest.raises(ValidationError):
            BlueprintCreate(
                name="BP",
                steps=[
                    BlueprintStepDefinition(
                        type=StepType.SEARCH,
                        name="Search",
                        parameters={"nested": {"payload": "x" * 40_000}},
                    )
                ],
            )

    def test_nested_prompt_is_server_bounded(self):
        with pytest.raises(ValidationError):
            BlueprintCreate(
                name="BP",
                steps=[
                    BlueprintStepDefinition(
                        type=StepType.SYNTHESIZE,
                        name="Synthesize",
                        parameters={"system_prompt_template": "x" * 20_000},
                    )
                ],
            )


class TestBlueprintUpdate:
    def test_all_optional(self):
        b = BlueprintUpdate()
        assert b.name is None
        assert b.steps is None
        assert b.parameters is None

    def test_oversized_prompt_is_rejected_on_update(self):
        with pytest.raises(ValidationError):
            BlueprintUpdate(
                steps=[
                    BlueprintStepDefinition(
                        type=StepType.SYNTHESIZE,
                        name="Synthesize",
                        system_prompt_template="x" * 20_000,
                    )
                ]
            )


class TestBlueprintResponse:
    def test_from_attributes(self):
        assert BlueprintResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        now = datetime.utcnow()
        uid = uuid4()
        pid = uuid4()
        step = BlueprintStepDefinition(type=StepType.SEARCH, name="S")
        b = BlueprintResponse(
            id=uid,
            project_id=pid,
            name="BP",
            template_source=None,
            version=1,
            steps=[step],
            parameters={},
            is_immutable=False,
            created_at=now,
            updated_at=now,
        )
        assert b.id == uid
        assert b.version == 1


# ============================================================================
# Run Schema Tests
# ============================================================================


class TestRunCreate:
    def test_defaults(self):
        r = RunCreate()
        assert r.parameters_override == {}
        assert r.scope_confirmation is None

    def test_with_overrides(self):
        r = RunCreate(parameters_override={"max_results": 10})
        assert r.parameters_override["max_results"] == 10

    def test_oversized_nested_override_is_rejected(self):
        with pytest.raises(ValidationError):
            RunCreate(parameters_override={"nested": ["x" * 40_000]})

    def test_accepts_typed_scope_confirmation(self):
        confirmation = research_schemas.DailyBriefScopeConfirmation(
            research_question="What changed in grounded generation?",
            inclusion_criteria=["Peer-reviewed empirical work"],
            exclusion_criteria=[],
            providers=["openalex"],
            limit_per_provider=25,
            confirmed=True,
        )

        request = RunCreate(scope_confirmation=confirmation)

        assert request.scope_confirmation == confirmation


class TestDailyBriefScopeConfirmation:
    def _valid(self, **overrides):
        values = {
            "research_question": "What changed in grounded generation?",
            "inclusion_criteria": ["Peer-reviewed empirical work"],
            "exclusion_criteria": [],
            "providers": ["openalex", "crossref"],
            "limit_per_provider": 25,
            "notes": "Focus on reproducible evaluations.",
            "confirmed": True,
        }
        values.update(overrides)
        return research_schemas.DailyBriefScopeConfirmation(**values)

    def test_accepts_exact_field_boundaries(self):
        confirmation = self._valid(
            research_question="q" * 2000,
            inclusion_criteria=["i" * 500] * 25,
            exclusion_criteria=["e" * 500] * 25,
            providers=["arxiv", "crossref", "openalex", "pubmed"],
            limit_per_provider=50,
            notes="n" * 2000,
        )

        assert len(confirmation.research_question) == 2000
        assert len(confirmation.inclusion_criteria) == 25
        assert len(confirmation.exclusion_criteria) == 25

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("research_question", ""),
            ("research_question", "q" * 2001),
            ("inclusion_criteria", []),
            ("inclusion_criteria", ["criterion"] * 26),
            ("inclusion_criteria", ["i" * 501]),
            ("exclusion_criteria", ["criterion"] * 26),
            ("exclusion_criteria", ["e" * 501]),
            ("providers", []),
            (
                "providers",
                ["arxiv", "crossref", "openalex", "pubmed", "semantic_scholar"],
            ),
            ("limit_per_provider", 0),
            ("limit_per_provider", 51),
            ("notes", "n" * 2001),
            ("confirmed", False),
        ],
    )
    def test_rejects_out_of_bounds_values(self, field, value):
        with pytest.raises(ValidationError):
            self._valid(**{field: value})

    @pytest.mark.parametrize(
        "providers",
        [
            ["web"],
            ["rag_store"],
            ["unknown"],
            ["openalex", "openalex"],
        ],
    )
    def test_rejects_noncanonical_ineligible_or_duplicate_providers(self, providers):
        with pytest.raises(ValidationError):
            self._valid(providers=providers)

    def test_rejects_client_hash_and_other_extra_fields(self):
        with pytest.raises(ValidationError):
            self._valid(configuration_hash="client-controlled")
        with pytest.raises(ValidationError):
            self._valid(unexpected="value")


class TestRunResumeRequest:
    def test_defaults_to_ordinary_resume(self):
        request = research_schemas.RunResumeRequest()
        assert request.continue_unverified is False
        assert request.output_hash is None

    def test_accepts_current_verification_hash(self):
        request = research_schemas.RunResumeRequest(
            continue_unverified=True,
            output_hash="a" * 64,
        )
        assert request.output_hash == "a" * 64

    @pytest.mark.parametrize("output_hash", ["short", "g" * 64, "a" * 65])
    def test_rejects_malformed_verification_hash(self, output_hash):
        with pytest.raises(ValidationError):
            research_schemas.RunResumeRequest(
                continue_unverified=True,
                output_hash=output_hash,
            )


class TestBlueprintTemplateDetailResponse:
    def test_validates_full_server_template(self):
        from src.services.research_engine.blueprints.loader import BlueprintLoader

        template = BlueprintLoader().load_template("daily_research_brief")
        response = research_schemas.BlueprintTemplateDetailResponse(
            slug="daily_research_brief",
            **template,
        )

        assert response.contract_version == 1
        assert response.template_source == "daily_research_brief"
        assert [step.type.value for step in response.steps] == [
            "search",
            "screen",
            "extract",
            "synthesize",
            "verify",
            "export",
        ]
        assert response.constraints["providers"] == {"min": 1, "max": 4}


class TestRunResponse:
    def test_from_attributes(self):
        assert RunResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        now = datetime.utcnow()
        r = RunResponse(
            id=uuid4(),
            blueprint_id=uuid4(),
            blueprint_version=1,
            status=RunStatus.PENDING,
            started_at=None,
            completed_at=None,
            total_tokens=0,
            created_at=now,
            updated_at=now,
        )
        assert r.status == RunStatus.PENDING
        assert r.total_tokens == 0

    def test_defaults(self):
        now = datetime.utcnow()
        r = RunResponse(
            id=uuid4(),
            blueprint_id=uuid4(),
            blueprint_version=1,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        assert r.total_tokens == 0
        assert r.started_at is None
        assert r.completed_at is None


# ============================================================================
# Step Schema Tests
# ============================================================================


class TestQualityMark:
    def test_valid(self):
        q = QualityMark(check_type="relevance", passed=True, details="OK")
        assert q.check_type == "relevance"
        assert q.passed is True

    def test_minimal(self):
        q = QualityMark(check_type="format", passed=False)
        assert q.details is None


class TestStepResponse:
    def test_from_attributes(self):
        assert StepResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        now = datetime.utcnow()
        s = StepResponse(
            id=uuid4(),
            run_id=uuid4(),
            step_index=0,
            step_type=StepType.SEARCH,
            mode=ExecutionMode.DETERMINISTIC,
            model_id="gpt-4",
            model_version="0613",
            temperature=0.0,
            seed=42,
            token_count=100,
            started_at=now,
            completed_at=now,
        )
        assert s.step_index == 0
        assert s.token_count == 100

    def test_defaults(self):
        s = StepResponse(
            id=uuid4(),
            run_id=uuid4(),
            step_index=1,
            step_type=StepType.EXTRACT,
            mode=ExecutionMode.EXPLORATORY,
            model_id="claude-3",
            model_version="v1",
            temperature=0.7,
        )
        assert s.token_count == 0
        assert s.output is None
        assert s.quality_marks is None
        assert s.inputs_hash is None
        assert s.outputs_hash is None
        assert s.full_prompt is None


# ============================================================================
# Source Schema Tests
# ============================================================================


class TestSourceResponse:
    def test_from_attributes(self):
        assert SourceResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        s = SourceResponse(
            id=uuid4(),
            run_id=uuid4(),
            connector_type="arxiv",
            external_id="2301.00001",
            title="A Paper",
            authors=["Author A", "Author B"],
            abstract="Abstract text",
            url="https://arxiv.org/abs/2301.00001",
            content_hash="abc123",
        )
        assert s.connector_type == "arxiv"
        assert len(s.authors) == 2

    def test_authors_optional(self):
        s = SourceResponse(
            id=uuid4(),
            run_id=uuid4(),
            connector_type="web",
            external_id="ext1",
            title="Title",
            abstract="Abstract",
            url="https://example.com",
            content_hash="hash",
        )
        assert s.authors is None


# ============================================================================
# Evidence Schema Tests
# ============================================================================


class TestEvidenceResponse:
    def test_from_attributes(self):
        assert EvidenceResponse.model_config.get("from_attributes") is True

    def test_valid(self):
        e = EvidenceResponse(
            id=uuid4(),
            step_id=uuid4(),
            source_id=uuid4(),
            claim_text="LLMs improve accuracy by 20%",
            confidence=0.95,
            grounding_status=GroundingStatus.VERIFIED,
            page_reference="p. 5",
        )
        assert e.confidence == 0.95
        assert e.grounding_status == GroundingStatus.VERIFIED
