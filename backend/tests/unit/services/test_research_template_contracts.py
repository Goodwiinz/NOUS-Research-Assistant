"""Behavioral contracts for the shipped research templates."""

import json
import re
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.contracts import (
    parse_provider_json,
    resolve_parameters,
    validate_extraction_record,
)
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderConfig,
)
from src.services.research_engine.step_executor import (
    ExecutionBudget,
    StepExecutionError,
    StepExecutor,
    StepResult,
)
from src.services.research_engine.verification import QualityMark

TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "services"
    / "research_engine"
    / "blueprints"
    / "templates"
)
SOURCE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_SOURCE_ID = "22222222-2222-4222-8222-222222222222"
ABSTRACT = "Treatment reduced the measured score among 42 participants."


class SyntheticConnector(SourceConnector):
    """Return a fixed source record without making a network request."""

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        del query, max_results, kwargs
        return [
            SourceDocument(
                connector_type="arxiv",
                external_id="synthetic-1",
                title="Synthetic treatment study",
                authors=["A. Researcher"],
                abstract=ABSTRACT,
                url="https://example.test/paper/1",
            )
        ]


class RecordingLimitConnector(SyntheticConnector):
    def __init__(self) -> None:
        self.requested_limits: list[int] = []

    async def search(
        self, query: str, max_results: int = 50, **kwargs: Any
    ) -> list[SourceDocument]:
        self.requested_limits.append(max_results)
        return await super().search(query, max_results, **kwargs)


class ContractProvider(LLMProvider):
    """Generate stage responses from the IDs and evidence in each request."""

    def __init__(self) -> None:
        super().__init__(ProviderConfig(provider_type="test", model_id="test-model"))
        self.requests: list[LLMRequest] = []

    async def is_model_available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        stage = re.search(
            r"stage_type=(screen|extract|synthesize|verify)",
            request.system_prompt or "",
        )
        stage_type = stage.group(1) if stage else "legacy"
        prompt_body = json.loads(request.prompt)
        records = prompt_body.get("records", [])
        parameters = prompt_body.get("context", {}).get("parameters", {})

        if stage_type == "screen":
            content = json.dumps(
                {
                    "screening": [
                        {
                            "source_id": record["source_id"],
                            "part_id": record["part_id"],
                            "included": True,
                            "reason": "Relevant",
                        }
                        for record in records
                    ]
                }
            )
        elif stage_type == "extract":
            if parameters.get("output_kind") == "claims":
                content = json.dumps(
                    {
                        "records": [
                            {
                                "source_id": record["source_id"],
                                "part_id": record["part_id"],
                                "claims": (
                                    [
                                        {
                                            "claim_text": "Treatment reduced the measured score",
                                            "quote": "Treatment reduced the measured score",
                                            "confidence": 0.95,
                                            "page_reference": None,
                                        }
                                    ]
                                    if "Treatment reduced the measured score"
                                    in record["text"]
                                    else []
                                ),
                            }
                            for record in records
                        ]
                    }
                )
            else:
                generated = []
                for record in records:
                    source_text = record["text"]
                    data: dict[str, Any]
                    evidence: list[dict[str, Any]]
                    if parameters.get("output_kind") == "review_fields":
                        fields = parameters.get("fields", [])
                        data = {
                            field: (
                                "Treatment reduced the measured score"
                                if field == "findings"
                                and "Treatment reduced the measured score"
                                in source_text
                                else None
                            )
                            for field in fields
                        }
                        evidence = (
                            [
                                {
                                    "pointer": "/findings",
                                    "quote": "Treatment reduced the measured score",
                                    "page_reference": None,
                                }
                            ]
                            if data.get("findings")
                            else []
                        )
                    else:
                        schema = parameters.get("schema", {})
                        properties = schema.get("properties", {})
                        data = {}
                        evidence = []
                        if "findings" in properties:
                            value = (
                                "Treatment reduced the measured score"
                                if "Treatment reduced the measured score" in source_text
                                else None
                            )
                            data["findings"] = value
                            if value is not None:
                                evidence.append(
                                    {
                                        "pointer": "/findings",
                                        "quote": value,
                                        "page_reference": None,
                                    }
                                )
                        if "sample_size" in properties:
                            sample_size_value: int | None
                            if "43 participants" in source_text:
                                sample_size_value = 43
                                quote = "43 participants"
                            else:
                                sample_size_value = (
                                    42 if "42 participants" in source_text else None
                                )
                                quote = "42 participants"
                            data["sample_size"] = sample_size_value
                            if sample_size_value is not None:
                                evidence.append(
                                    {
                                        "pointer": "/sample_size",
                                        "quote": quote,
                                        "page_reference": None,
                                    }
                                )
                    generated.append(
                        {
                            "source_id": record["source_id"],
                            "part_id": record["part_id"],
                            "data": data,
                            "evidence": evidence,
                        }
                    )
                content = json.dumps({"records": generated})
        elif stage_type == "synthesize":
            evidence_ids: list[str] = []
            for record in records:
                try:
                    evidence_items = json.loads(record["text"])
                except json.JSONDecodeError:
                    evidence_items = []
                evidence_ids.extend(
                    item["evidence_id"]
                    for item in evidence_items
                    if isinstance(item, dict) and item.get("evidence_id")
                )
            evidence_ids = list(dict.fromkeys(evidence_ids))
            content = json.dumps(
                {
                    "sections": (
                        [
                            {
                                "heading": "Findings",
                                "claims": [
                                    {
                                        "claim_text": "Treatment reduced the measured score",
                                        "evidence": [
                                            {
                                                "evidence_id": evidence_ids[0],
                                                "relation": "supports",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                        if evidence_ids
                        else []
                    )
                }
            )
        elif stage_type == "verify":
            claim_ids = []
            for record in records:
                claim_ids.append(json.loads(record["text"])["claim_id"])
            claim_ids = list(dict.fromkeys(claim_ids))
            content = json.dumps(
                {
                    "checks": [
                        {
                            "claim_id": claim_id,
                            "status": "supported",
                            "reason": "The cited excerpt supports the claim",
                        }
                        for claim_id in claim_ids
                    ]
                }
            )
        else:
            content = "42 participants: treatment reduced the measured score."

        return LLMResponse(
            content=content,
            model_id="test-model",
            input_tokens=40,
            output_tokens=20,
        )


def _load_template(name: str) -> dict[str, Any]:
    with (TEMPLATE_DIR / f"{name}.yaml").open(encoding="utf-8") as handle:
        return cast(dict[str, Any], yaml.safe_load(handle))


@pytest.mark.parametrize(
    "template_name",
    ["data_extraction", "evidence_synthesis", "systematic_literature_review"],
)
@pytest.mark.asyncio
async def test_shipped_template_reaches_verified_export(template_name: str) -> None:
    """The real template path must retain source evidence through verification."""
    blueprint = _load_template(template_name)
    blueprint["parameters"].update(
        {
            "query": "treatment outcome",
            "contract_version": 1,
            "extraction_schema": {
                "type": "object",
                "properties": {
                    "findings": {"type": "string"},
                    "sample_size": {"type": "integer"},
                },
                "required": ["findings", "sample_size"],
                "additionalProperties": False,
            },
            "extraction_fields": ["findings"],
            "inclusion_criteria": ["treatment outcome"],
            "exclusion_criteria": [],
        }
    )
    provider = ContractProvider()
    executor = StepExecutor(
        connectors={
            name: SyntheticConnector()
            for name in ("arxiv", "semantic_scholar", "pubmed", "rag_store")
        },
        providers={"claude-sonnet-4-6": provider, "test-model": provider},
    )
    events = [
        event
        async for event in WorkflowEngine(executor).run(
            blueprint,
            uuid4(),
            initial_context={
                "claim": "Treatment reduced the measured score among 42 participants.",
                "evidence": ABSTRACT,
            },
        )
    ]

    assert events[-1]["event"] == "run_complete"
    context = events[-1]["context"]
    assert context["verification"]["passed"] is True
    assert context["exported"]["verification"]["passed"] is True
    assert context["exported"]["sources"][0]["source_id"]
    rendered_prompts = {
        stage: next(
            request.system_prompt or ""
            for request in provider.requests
            if f"stage_type={stage}" in (request.system_prompt or "")
        )
        for stage in {"screen", "extract", "synthesize", "verify"}
        if any(
            f"stage_type={stage}" in (r.system_prompt or "") for r in provider.requests
        )
    }
    template_prompt_text = "\n".join(
        str(step.get("system_prompt_template") or "") for step in blueprint["steps"]
    )
    assert "Return as a JSON array" not in template_prompt_text
    assert (
        "Return a JSON object matching the schema exactly" not in template_prompt_text
    )
    if template_name == "data_extraction":
        extraction_prompt = rendered_prompts["extract"]
        verifier_prompt = rendered_prompts["verify"]
        assert '"records":[{"source_id":"<UUID>","part_id":"p0001"' in extraction_prompt
        assert '"data":{"sample_size":42}' in extraction_prompt
        assert '"evidence":[{"pointer":"/sample_size"' in extraction_prompt
        assert (
            "Use null only when the supplied extraction schema permits null"
            in extraction_prompt
        )
        assert "Use null only" not in verifier_prompt
        assert '"checks":[{"claim_id":"c0001"' in verifier_prompt
        extraction_request = next(
            request
            for request in provider.requests
            if "stage_type=extract" in (request.system_prompt or "")
        )
        request_parameters = json.loads(extraction_request.prompt)["context"][
            "parameters"
        ]
        assert request_parameters["schema"]["properties"]["sample_size"] == {
            "type": "integer"
        }
    else:
        assert (
            '"sections":[{"heading":"Findings","claims":[{"claim_text":"The study reports a lower score"'
            in rendered_prompts["synthesize"]
        )
        assert (
            '"evidence":[{"evidence_id":"e0001","relation":"supports"}]'
            in rendered_prompts["synthesize"]
        )
        assert '"checks":[{"claim_id":"c0001"' in rendered_prompts["verify"]
        assert "Use null only" not in rendered_prompts["synthesize"]
        assert "Use null only" not in rendered_prompts["verify"]
        extraction_prompt = rendered_prompts["extract"]
        assert '"records":[{"source_id":"<UUID>","part_id":"p0001"' in extraction_prompt
        if template_name == "evidence_synthesis":
            assert (
                '"claims":[{"claim_text":"Treatment reduced the measured score"'
                in extraction_prompt
            )
            assert "Use null only" not in extraction_prompt
            assert "Use no page reference unless supplied" in extraction_prompt
        if template_name == "systematic_literature_review":
            assert (
                "Use null only when the supplied extraction schema permits null"
                in extraction_prompt
            )
            assert '"screening":[{"source_id":"<UUID>"' in rendered_prompts["screen"]
            assert 'Requested review fields: ["findings"]' in extraction_prompt
    if template_name == "evidence_synthesis":
        template_text = json.dumps(_load_template(template_name)).lower()
        assert "general web" not in template_text
        assert "web sources" not in template_text


@pytest.mark.parametrize("requested, expected", [(3, 3), (100, 50)])
@pytest.mark.asyncio
async def test_data_extraction_search_limit_uses_effective_max_documents(
    requested: int, expected: int
) -> None:
    blueprint = _load_template("data_extraction")
    connector = RecordingLimitConnector()
    executor = StepExecutor(connectors={"rag_store": connector}, providers={})

    await executor.execute(
        blueprint["steps"][0],
        {
            **blueprint["parameters"],
            "query": "synthetic query",
            "max_documents": requested,
        },
    )

    assert connector.requested_limits == [expected]


@pytest.mark.asyncio
async def test_quote_from_another_source_is_rejected() -> None:
    """An exact quote from one paper cannot be attached to another source ID."""

    class WrongQuoteProvider(ContractProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            return LLMResponse(
                content=json.dumps(
                    {
                        "records": [
                            {
                                "source_id": SOURCE_ID,
                                "part_id": "p0001",
                                "data": {"sample_size": 42},
                                "evidence": [
                                    {
                                        "pointer": "/sample_size",
                                        "quote": "42 participants",
                                        "page_reference": None,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                model_id="test-model",
                input_tokens=40,
                output_tokens=20,
            )

    provider = WrongQuoteProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    with pytest.raises(StepExecutionError, match="contract validation") as error:
        await executor.execute(
            {
                "type": "extract",
                "parameters": {
                    "model_id": "test-model",
                    "contract_version": 1,
                    "output_kind": "structured_data",
                },
            },
            {
                "contract_version": 1,
                "source_records": [
                    {
                        "source_id": SOURCE_ID,
                        "title": "Paper A",
                        "abstract": "No numeric sample size reported.",
                    },
                    {
                        "source_id": SECOND_SOURCE_ID,
                        "title": "Paper B",
                        "abstract": "The study included 42 participants.",
                    },
                ],
            },
        )
    assert error.value.consumed_tokens > 0
    assert error.value.model_calls == 1


@pytest.mark.asyncio
async def test_ten_1125_character_abstracts_batch_without_overflow() -> None:
    """Complete UTF-8 requests stay bounded and every source gets a disposition."""
    provider = ContractProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    source_records = [
        {
            "source_id": str(uuid4()),
            "title": f"Paper {index}",
            "abstract": ("é" * 1124) + "x",
            "evidence_level": "abstract",
        }
        for index in range(10)
    ]
    result = await executor.execute(
        {
            "type": "extract",
            "parameters": {
                "model_id": "test-model",
                "contract_version": 1,
                "output_kind": "claims",
                "system_prompt_template": "Fixed instructions. " + ("rule " * 400),
            },
        },
        {"contract_version": 1, "query": "treatment", "source_records": source_records},
    )

    assert provider.requests
    assert all(
        len((request.system_prompt or "").encode("utf-8"))
        + len(request.prompt.encode("utf-8"))
        <= 16_384
        for request in provider.requests
    )
    coverage = result.output["processing_coverage"]["0"]
    outcomes = set(coverage["processed_source_ids"] + coverage["excluded_source_ids"])
    outcomes.update(item["source_id"] for item in coverage["omitted"])
    assert outcomes == {record["source_id"] for record in source_records}


@pytest.mark.asyncio
async def test_equal_values_across_source_parts_merge_evidence() -> None:
    provider = ContractProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    abstract = "The study included 42 participants. " * 800
    result = await executor.execute(
        {
            "type": "extract",
            "parameters": {
                "model_id": "test-model",
                "contract_version": 1,
                "output_kind": "structured_data",
                "schema": {
                    "type": "object",
                    "properties": {"sample_size": {"type": "integer"}},
                    "required": ["sample_size"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "contract_version": 1,
            "source_records": [
                {
                    "source_id": SOURCE_ID,
                    "title": "Long synthetic abstract",
                    "abstract": abstract,
                    "evidence_level": "abstract",
                }
            ],
        },
    )

    extractions = result.output["extractions"]
    assert len(extractions) == 1
    assert extractions[0]["data"] == {"sample_size": 42}
    assert len(extractions[0]["evidence"]) > 1
    assert len({item["part_id"] for item in extractions[0]["evidence"]}) > 1
    assert result.output["diagnostics"] == []
    assert result.output["processing_coverage"]["0"]["complete"] is True


@pytest.mark.asyncio
async def test_conflicting_values_across_source_parts_remain_unverified() -> None:
    provider = ContractProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    abstract = "The study included 42 participants. " + ("background text. " * 1000)
    abstract += "The study included 43 participants."
    result = await executor.execute(
        {
            "type": "extract",
            "parameters": {
                "model_id": "test-model",
                "contract_version": 1,
                "output_kind": "structured_data",
                "schema": {
                    "type": "object",
                    "properties": {"sample_size": {"type": "integer"}},
                    "required": ["sample_size"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "contract_version": 1,
            "source_records": [
                {
                    "source_id": SOURCE_ID,
                    "title": "Conflicting synthetic abstract",
                    "abstract": abstract,
                    "evidence_level": "abstract",
                }
            ],
        },
    )

    diagnostics = result.output["diagnostics"]
    assert any(
        item["reason"] == "conflicting_values_across_source_parts"
        for item in diagnostics
    )
    assert len(result.output["extractions"]) > 1
    assert result.output["processing_coverage"]["0"]["complete"] is False
    assert result.quality_marks[0].passed is False


@pytest.mark.asyncio
async def test_failed_verification_resume_cannot_be_overwritten() -> None:
    """A persisted failure remains false when a later step is resumed."""

    class PassingExecutor(StepExecutor):
        def __init__(self) -> None:
            super().__init__(connectors={}, providers={})

        async def execute(
            self,
            step_def: dict[str, Any],
            context: dict[str, Any],
            *,
            budget: ExecutionBudget | None = None,
        ) -> StepResult:
            del step_def, context, budget
            return StepResult(output={"verification": {"passed": True}})

    initial = {
        "verification": {
            "passed": False,
            "semantic_status": "contradicted",
            "continued_after_failure": True,
        }
    }
    events = [
        event
        async for event in WorkflowEngine(PassingExecutor()).run(
            {"steps": [{"type": "export", "parameters": {"format": "json"}}]},
            uuid4(),
            initial_context=initial,
        )
    ]

    assert events[-1]["context"]["verification"]["passed"] is False
    assert events[-1]["context"]["verification"]["continued_after_failure"] is True


def test_typed_parameter_placeholder_preserves_json_value() -> None:
    parameters = {"fields": ["year", "authors"], "limit": 10}
    assert resolve_parameters(
        {"fields": "$fields", "limit": "{limit}"}, parameters
    ) == {
        "fields": ["year", "authors"],
        "limit": 10,
    }


def test_provider_json_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_provider_json('{"checks": [], "checks": []}')


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_provider_json_rejects_nested_exponent_overflow(number: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        parse_provider_json('{"outer":[{"value":' + number + "}]}")


def test_wrong_integer_is_rejected_by_extraction_schema() -> None:
    with pytest.raises(ValueError, match="required schema"):
        validate_extraction_record(
            {
                "source_id": SOURCE_ID,
                "part_id": "p0001",
                "data": {"sample_size": "42"},
                "evidence": [],
            },
            schema={
                "type": "object",
                "properties": {"sample_size": {"type": "integer"}},
                "required": ["sample_size"],
                "additionalProperties": False,
            },
            source_parts={(SOURCE_ID, "p0001"): "The study included 42 participants."},
        )


def test_missing_scalar_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="lack exact evidence"):
        validate_extraction_record(
            {
                "source_id": SOURCE_ID,
                "part_id": "p0001",
                "data": {"sample_size": 42},
                "evidence": [],
            },
            schema={
                "type": "object",
                "properties": {"sample_size": {"type": "integer"}},
                "required": ["sample_size"],
                "additionalProperties": False,
            },
            source_parts={(SOURCE_ID, "p0001"): "The study included 42 participants."},
        )


def test_invented_page_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="trusted source metadata"):
        validate_extraction_record(
            {
                "source_id": SOURCE_ID,
                "part_id": "p0001",
                "data": {"sample_size": 42},
                "evidence": [
                    {
                        "pointer": "/sample_size",
                        "quote": "42 participants",
                        "page_reference": "page 7",
                    }
                ],
            },
            schema={
                "type": "object",
                "properties": {"sample_size": {"type": "integer"}},
                "required": ["sample_size"],
                "additionalProperties": False,
            },
            source_parts={(SOURCE_ID, "p0001"): "The study included 42 participants."},
            trusted_pages={(SOURCE_ID, "p0001"): set()},
        )


@pytest.mark.asyncio
async def test_remote_extraction_schema_reference_is_rejected_before_call() -> None:
    provider = ContractProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    with pytest.raises(ValueError, match="remote JSON Schema references"):
        await executor.execute(
            {
                "type": "extract",
                "parameters": {
                    "contract_version": 1,
                    "model_id": "test-model",
                    "schema": {"$ref": "https://example.test/schema.json"},
                },
            },
            {
                "contract_version": 1,
                "source_records": [
                    {
                        "source_id": SOURCE_ID,
                        "title": "Synthetic treatment study",
                        "abstract": ABSTRACT,
                    }
                ],
            },
        )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_semantic_contradiction_cannot_pass_verification() -> None:
    class ContradictionProvider(ContractProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            prompt_body = json.loads(request.prompt)
            claim_ids = [
                json.loads(record["text"])["claim_id"]
                for record in prompt_body["records"]
            ]
            return LLMResponse(
                content=json.dumps(
                    {
                        "checks": [
                            {
                                "claim_id": claim_id,
                                "status": "contradicted",
                                "reason": "The evidence does not support the claim.",
                            }
                            for claim_id in claim_ids
                        ]
                    }
                ),
                model_id="test-model",
                input_tokens=40,
                output_tokens=20,
            )

    provider = ContradictionProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    result = await executor.execute(
        {
            "type": "verify",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        {
            "contract_version": 1,
            "source_records": [
                {
                    "source_id": SOURCE_ID,
                    "title": "Synthetic treatment study",
                    "evidence_level": "abstract",
                    "abstract": ABSTRACT,
                }
            ],
            "extractions": [
                {
                    "source_id": SOURCE_ID,
                    "part_id": "p0001",
                    "claims": [
                        {
                            "claim_text": "Treatment reduced the score",
                            "evidence_id": "e0001",
                            "quote": "Treatment reduced the measured score",
                            "page_reference": None,
                        }
                    ],
                }
            ],
            "synthesis": {
                "claims": [
                    {
                        "claim_id": "c0001",
                        "claim_text": "Treatment reduced the score",
                        "evidence": [{"evidence_id": "e0001", "relation": "supports"}],
                    }
                ]
            },
        },
    )

    assert result.output["verification"]["passed"] is False
    assert result.output["verification"]["semantic_status"] == "failed"
    assert result.output["verification"]["claims"][0]["status"] == "contradicted"
    assert result.quality_marks[0].passed is False


@pytest.mark.asyncio
async def test_synthesis_reduces_map_claims_and_preserves_evidence_ids() -> None:
    class ReductionProvider(ContractProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            body = json.loads(request.prompt)
            is_reduction = body["context"]["parameters"].get("reduction_mode") is True
            claims = []
            for record in body["records"]:
                source_value = json.loads(record["text"])
                items = (
                    source_value if isinstance(source_value, list) else [source_value]
                )
                if is_reduction:
                    references = [
                        reference
                        for item in items
                        for reference in item.get("evidence", [])
                    ]
                    claims.append(
                        {
                            "claim_text": "The studies report related measurements",
                            "evidence": references,
                        }
                    )
                else:
                    claims.extend(
                        {
                            "claim_text": f"Finding for {item['evidence_id']}",
                            "evidence": [
                                {
                                    "evidence_id": item["evidence_id"],
                                    "relation": "supports",
                                }
                            ],
                        }
                        for item in items
                    )
            if is_reduction:
                # A reduction batch can have several records; combine them into
                # one supported claim while retaining every supplied reference.
                combined: dict[str, str] = {}
                for claim in claims:
                    for reference in claim["evidence"]:
                        combined[reference["evidence_id"]] = reference["relation"]
                claims = [
                    {
                        "claim_text": "The studies report related measurements",
                        "evidence": [
                            {"evidence_id": evidence_id, "relation": relation}
                            for evidence_id, relation in sorted(combined.items())
                        ],
                    }
                ]
            return LLMResponse(
                content=json.dumps(
                    {"sections": [{"heading": "Findings", "claims": claims}]}
                ),
                model_id="test-model",
                input_tokens=40,
                output_tokens=20,
            )

    source_text = " ".join(f"Finding {index}." for index in range(65))
    extraction = {
        "source_id": SOURCE_ID,
        "part_id": "p0001",
        "claims": [
            {
                "claim_text": f"Finding {index}",
                "evidence_id": f"e{index + 1:04d}",
                "quote": f"Finding {index}.",
                "confidence": 0.95,
                "page_reference": None,
            }
            for index in range(65)
        ],
    }
    provider = ReductionProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    result = await executor.execute(
        {
            "type": "synthesize",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        {
            "contract_version": 1,
            "source_records": [
                {
                    "source_id": SOURCE_ID,
                    "title": "Synthetic treatment study",
                    "evidence_level": "abstract",
                    "abstract": source_text,
                }
            ],
            "extractions": [extraction],
        },
    )

    synthesis = result.output["synthesis"]
    assert len(synthesis["claims"]) < 65
    assert result.output["usage"]["model_calls"] == len(provider.requests)
    assert any(
        json.loads(request.prompt)["context"]["parameters"].get("reduction_mode")
        for request in provider.requests
    )
    retained_evidence = {
        reference["evidence_id"]
        for claim in synthesis["claims"]
        for reference in claim["evidence"]
    }
    assert retained_evidence == {f"e{index + 1:04d}" for index in range(65)}
    assert result.output["processing_coverage"]["0"]["complete"] is True
