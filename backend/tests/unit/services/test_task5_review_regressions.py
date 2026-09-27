"""Astra Task 5 review regressions at the research engine boundaries."""

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.contracts import merge_stage_output
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.prompt_batches import build_prompt_batches
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

SOURCE_ID = "11111111-1111-4111-8111-111111111111"


class CallbackProvider(LLMProvider):
    def __init__(self, callback: Callable[[LLMRequest], str]) -> None:
        super().__init__(ProviderConfig(provider_type="test", model_id="test-model"))
        self.callback = callback
        self.requests: list[LLMRequest] = []

    async def is_model_available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self.callback(request)
        return LLMResponse(
            content=content,
            model_id="test-model",
            input_tokens=40,
            output_tokens=20,
        )


def _synthesis_context(
    *, evidence_count: int = 2, quote: str = "Treatment reduced the score."
) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "source_records": [
            {
                "source_id": SOURCE_ID,
                "title": "Synthetic source",
                "abstract": quote,
                "evidence_level": "abstract",
            }
        ],
        "extractions": [
            {
                "source_id": SOURCE_ID,
                "part_id": "p0001",
                "claims": [
                    {
                        "claim_text": f"Finding {index}",
                        "evidence_id": f"e{index:04d}",
                        "quote": quote,
                        "page_reference": None,
                    }
                    for index in range(1, evidence_count + 1)
                ],
            }
        ],
    }


def _all_evidence_ids(request: LLMRequest) -> list[str]:
    ids: list[str] = []
    body = json.loads(request.prompt)
    for record in body["records"]:
        values = json.loads(record["text"])
        values = values if isinstance(values, list) else [values]
        ids.extend(item["evidence_id"] for item in values if "evidence_id" in item)
    return list(dict.fromkeys(ids))


@pytest.mark.asyncio
async def test_initial_synthesis_cannot_silently_omit_required_evidence() -> None:
    provider = CallbackProvider(
        lambda request: json.dumps(
            {
                "sections": [
                    {
                        "heading": "Findings",
                        "claims": [
                            {
                                "claim_text": "Finding one",
                                "evidence": [
                                    {
                                        "evidence_id": _all_evidence_ids(request)[0],
                                        "relation": "supports",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    context = _synthesis_context()
    result = await executor.execute(
        {
            "type": "synthesize",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        context,
    )

    downstream = merge_stage_output(context, result.output, 1)
    provider.callback = lambda request: json.dumps(
        {
            "checks": [
                {
                    "claim_id": json.loads(record["text"])["claim_id"],
                    "status": "supported",
                    "reason": "Exact quote supports the claim",
                }
                for record in json.loads(request.prompt)["records"]
            ]
        }
    )
    verified = await executor.execute(
        {
            "type": "verify",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        downstream,
    )
    downstream = merge_stage_output(downstream, verified.output, 2)
    exported = await executor.execute(
        {"type": "export", "parameters": {"contract_version": 1, "format": "json"}},
        downstream,
    )
    assert downstream["verification"]["passed"] is False
    assert exported.output["exported"]["verification"]["passed"] is False
    coverage = result.output["processing_coverage"]["0"]
    assert coverage["complete"] is False
    assert {item["reason"] for item in coverage["omitted"]} == {
        "omitted_evidence:e0002"
    }
    assert result.quality_marks and result.quality_marks[0].passed is False


@pytest.mark.asyncio
async def test_structured_synthesis_records_are_never_character_split() -> None:
    provider = CallbackProvider(lambda _request: '{"sections":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    context = _synthesis_context(evidence_count=5, quote="x" * 4000)

    await executor.execute(
        {
            "type": "synthesize",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        context,
    )

    records = [
        record
        for request in provider.requests
        for record in json.loads(request.prompt)["records"]
    ]
    assert records
    assert all(isinstance(json.loads(record["text"]), list) for record in records)
    covered_ids = {
        item["evidence_id"] for record in records for item in json.loads(record["text"])
    }
    assert covered_ids == {f"e{index:04d}" for index in range(1, 6)}


@pytest.mark.asyncio
async def test_oversized_single_structured_evidence_object_fails_before_dispatch() -> (
    None
):
    provider = CallbackProvider(lambda _request: '{"sections":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    context = _synthesis_context(evidence_count=1, quote="x" * 4000)
    context["source_records"][0]["abstract"] = "x" * 20_000
    context["extractions"][0]["data"] = {"large": "x" * 20_000}
    context["extractions"][0]["evidence"] = [
        {
            "evidence_id": "e0001",
            "pointer": "/large",
            "quote": "x" * 4000,
            "page_reference": None,
        }
    ]

    with pytest.raises((StepExecutionError, ValueError)):
        await executor.execute(
            {
                "type": "synthesize",
                "parameters": {"contract_version": 1, "model_id": "test-model"},
            },
            context,
        )

    assert provider.requests == []


def test_token_reservation_uses_full_input_bytes_plus_framing_and_output() -> None:
    budget = ExecutionBudget(max_total_tokens=1000)

    reserved = budget.reserve(input_bytes=123, output_tokens=77)

    assert reserved == 712
    assert budget.reserved_tokens == 712


@pytest.mark.asyncio
async def test_16000_byte_request_cannot_dispatch_with_less_than_8000_tokens() -> None:
    provider = CallbackProvider(lambda _request: '{"records":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    budget = ExecutionBudget(max_total_tokens=7_999, active_step_index=1)

    with pytest.raises(StepExecutionError, match="budget exhausted") as error:
        await executor._complete_batches(
            provider,
            [{"input_bytes": 16_000, "prompt": "", "system_prompt": ""}],
            {"max_tokens": 2048},
            budget,
        )

    assert provider.requests == []
    assert error.value.consumed_tokens == 0
    assert error.value.model_calls == 0


def _verification_context(
    *, claim_count: int = 1, quote_size: int = 80, references_per_claim: int = 1
) -> dict[str, Any]:
    quote = "q" * quote_size
    evidence = [
        {
            "evidence_id": f"e{index:04d}",
            "part_id": "p0001",
            "quote": quote,
            "page_reference": None,
        }
        for index in range(1, claim_count * references_per_claim + 1)
    ]
    claims = []
    for claim_index in range(claim_count):
        start = claim_index * references_per_claim
        claims.append(
            {
                "claim_id": f"c{claim_index + 1:04d}",
                "claim_text": f"Claim {claim_index + 1}",
                "evidence": [
                    {
                        "evidence_id": item["evidence_id"],
                        "relation": "supports",
                    }
                    for item in evidence[start : start + references_per_claim]
                ],
            }
        )
    return {
        "contract_version": 1,
        "source_records": [
            {
                "source_id": SOURCE_ID,
                "title": "Synthetic source",
                "abstract": quote,
                "evidence_level": "abstract",
            }
        ],
        "extractions": [
            {
                "source_id": SOURCE_ID,
                "part_id": "p0001",
                "claims": [
                    {
                        "claim_text": f"Raw claim {index}",
                        "evidence_id": item["evidence_id"],
                        "quote": item["quote"],
                        "page_reference": None,
                    }
                    for index, item in enumerate(evidence, start=1)
                ],
            }
        ],
        "synthesis": {"claims": claims},
    }


@pytest.mark.asyncio
async def test_verification_packs_complete_v1_claim_objects_across_batches() -> None:
    provider = CallbackProvider(
        lambda request: json.dumps(
            {
                "checks": [
                    {
                        "claim_id": json.loads(record["text"])["claim_id"],
                        "status": "supported",
                        "reason": "The exact quote supports the claim",
                    }
                    for record in json.loads(request.prompt)["records"]
                ]
            }
        )
    )
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    result = await executor.execute(
        {
            "type": "verify",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        _verification_context(claim_count=10, quote_size=2200),
    )

    assert len(provider.requests) >= 2
    supplied_ids = []
    for request in provider.requests:
        for record in json.loads(request.prompt)["records"]:
            claim = json.loads(record["text"])
            assert claim["claim_id"].startswith("c")
            supplied_ids.append(claim["claim_id"])
    assert supplied_ids == [f"c{index:04d}" for index in range(1, 11)]
    assert result.output["verification"]["passed"] is True


@pytest.mark.asyncio
async def test_oversized_v1_verification_claim_is_rejected_before_dispatch() -> None:
    provider = CallbackProvider(lambda _request: '{"checks":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    with pytest.raises((StepExecutionError, ValueError)):
        await executor.execute(
            {
                "type": "verify",
                "parameters": {"contract_version": 1, "model_id": "test-model"},
            },
            _verification_context(
                claim_count=1, quote_size=4000, references_per_claim=5
            ),
        )

    assert provider.requests == []


@pytest.mark.asyncio
async def test_missing_verifier_claim_id_fails_with_paid_usage() -> None:
    provider = CallbackProvider(lambda _request: '{"checks":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    with pytest.raises(StepExecutionError, match="contract validation") as error:
        await executor.execute(
            {
                "type": "verify",
                "parameters": {"contract_version": 1, "model_id": "test-model"},
            },
            _verification_context(),
        )

    assert len(provider.requests) == 1
    assert error.value.consumed_tokens == 60
    assert error.value.model_calls == 1
    assert error.value.batch_metadata[0]["input_hash"]
    assert error.value.batch_metadata[0]["output_hash"]


@pytest.mark.asyncio
async def test_malformed_reduction_response_keeps_ordered_paid_batch_hashes() -> None:
    calls = 0

    def respond(request: LLMRequest) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            return '{"sections": ['
        evidence_ids = _all_evidence_ids(request)
        return json.dumps(
            {
                "sections": [
                    {
                        "heading": "Findings",
                        "claims": [
                            {
                                "claim_text": f"Finding {evidence_id}",
                                "evidence": [
                                    {
                                        "evidence_id": evidence_id,
                                        "relation": "supports",
                                    }
                                ],
                            }
                            for evidence_id in evidence_ids
                        ],
                    }
                ]
            }
        )

    provider = CallbackProvider(respond)
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    with pytest.raises(StepExecutionError, match="reduction failed") as error:
        await executor.execute(
            {
                "type": "synthesize",
                "parameters": {"contract_version": 1, "model_id": "test-model"},
            },
            _synthesis_context(evidence_count=65),
        )

    assert calls == 2
    assert error.value.consumed_tokens == 120
    assert error.value.model_calls == 2
    assert len(error.value.batch_metadata) == 2
    assert all(
        item["input_hash"] and item["output_hash"]
        for item in error.value.batch_metadata
    )
    assert (
        error.value.batch_metadata[0]["input_hash"]
        != error.value.batch_metadata[1]["input_hash"]
    )


@pytest.mark.asyncio
async def test_nonshrinking_reduction_is_incomplete() -> None:
    def respond(request: LLMRequest) -> str:
        body = json.loads(request.prompt)
        claims = []
        for record in body["records"]:
            value = json.loads(record["text"])
            values = value if isinstance(value, list) else [value]
            for item in values:
                if body["context"]["parameters"].get("reduction_mode"):
                    claims.append(
                        {"claim_text": item["claim_text"], "evidence": item["evidence"]}
                    )
                else:
                    claims.append(
                        {
                            "claim_text": "Finding " + item["evidence_id"],
                            "evidence": [
                                {
                                    "evidence_id": item["evidence_id"],
                                    "relation": "supports",
                                }
                            ],
                        }
                    )
        return json.dumps({"sections": [{"heading": "Findings", "claims": claims}]})

    provider = CallbackProvider(respond)
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    result = await executor.execute(
        {
            "type": "synthesize",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        _synthesis_context(evidence_count=65),
    )

    assert result.output["processing_coverage"]["0"]["complete"] is False
    assert any(
        item.get("reason") == "non_shrinking_output"
        for item in result.output["synthesis"]["reduction_diagnostics"]
    )


@pytest.mark.asyncio
async def test_timeout_after_paid_batch_retains_stage_usage() -> None:
    class HangsOnSecondBatch(CallbackProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            if len(self.requests) == 2:
                await asyncio.sleep(5)
            content = json.dumps(
                {
                    "records": [
                        {
                            "source_id": item["source_id"],
                            "part_id": item["part_id"],
                            "claims": [],
                        }
                        for item in json.loads(request.prompt)["records"]
                    ]
                }
            )
            return LLMResponse(
                content=content,
                model_id="test-model",
                input_tokens=40,
                output_tokens=20,
            )

    provider = HangsOnSecondBatch(lambda _request: "")
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    sources = [
        {
            "source_id": f"source-{index}",
            "title": f"Source {index}",
            "abstract": "Synthetic evidence sentence.",
        }
        for index in range(10)
    ]
    events = [
        event
        async for event in WorkflowEngine(executor, max_wall_time_seconds=0.2).run(
            {
                "steps": [
                    {
                        "id": "extract",
                        "type": "extract",
                        "parameters": {
                            "contract_version": 1,
                            "model_id": "test-model",
                            "output_kind": "claims",
                        },
                    }
                ]
            },
            uuid4(),
            initial_context={"contract_version": 1, "source_records": sources},
        )
    ]

    errors = [event for event in events if event["event"] == "step_error"]
    assert len(errors) == 1
    assert errors[0]["consumed_tokens"] == 60
    assert errors[0]["model_calls"] == 1
    assert len(errors[0]["batch_metadata"]) == 1
    assert errors[0]["batch_metadata"][0]["input_hash"]
    assert errors[0]["batch_metadata"][0]["output_hash"]


@pytest.mark.asyncio
async def test_fixed_prompt_overflow_fails_before_provider_dispatch() -> None:
    provider = CallbackProvider(lambda _request: '{"records":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    with pytest.raises(ValueError, match="fixed prompt instructions exceed"):
        await executor.execute(
            {
                "type": "extract",
                "parameters": {
                    "contract_version": 1,
                    "model_id": "test-model",
                    "output_kind": "claims",
                    "system_prompt_template": "x" * 20_000,
                },
            },
            {
                "contract_version": 1,
                "source_records": [
                    {"source_id": SOURCE_ID, "title": "Source", "abstract": "text"}
                ],
            },
        )

    assert provider.requests == []


@pytest.mark.asyncio
async def test_provider_json_rejects_recursive_exponent_overflow_in_contract_path() -> (
    None
):
    provider = CallbackProvider(
        lambda _request: (
            '{"records":[{"source_id":"'
            + SOURCE_ID
            + '","part_id":"p0001","data":{"nested":{"value":1e999}},'
            + '"evidence":[{"pointer":"/nested/value","quote":"value was 1",'
            + '"page_reference":null}]}]}'
        )
    )
    executor = StepExecutor(connectors={}, providers={"test-model": provider})

    with pytest.raises(StepExecutionError, match="contract validation") as error:
        await executor.execute(
            {
                "type": "extract",
                "parameters": {
                    "contract_version": 1,
                    "model_id": "test-model",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "nested": {
                                "type": "object",
                                "properties": {"value": {"type": "number"}},
                                "required": ["value"],
                            }
                        },
                        "required": ["nested"],
                    },
                },
            },
            {
                "contract_version": 1,
                "source_records": [
                    {
                        "source_id": SOURCE_ID,
                        "title": "Source",
                        "abstract": "value was 1",
                    }
                ],
            },
        )

    assert error.value.consumed_tokens == 60


@pytest.mark.asyncio
async def test_screened_out_and_all_excluded_sources_do_no_extraction_or_synthesis_work() -> (
    None
):
    provider = CallbackProvider(lambda _request: "{}")
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    source_records = [
        {
            "source_id": SOURCE_ID,
            "title": "Excluded source",
            "abstract": "No evidence is selected.",
        }
    ]

    extraction = await executor.execute(
        {
            "type": "extract",
            "parameters": {
                "contract_version": 1,
                "model_id": "test-model",
                "output_kind": "claims",
            },
        },
        {
            "contract_version": 1,
            "source_records": source_records,
            "included_source_ids": [],
        },
    )
    synthesis = await executor.execute(
        {
            "type": "synthesize",
            "parameters": {"contract_version": 1, "model_id": "test-model"},
        },
        {"contract_version": 1, "source_records": source_records, "extractions": []},
    )

    assert provider.requests == []
    assert (
        extraction.output["processing_coverage"]["0"]["omitted"][0]["reason"]
        == "excluded_by_screening"
    )
    assert synthesis.output["synthesis"]["unverified_reason"]


@pytest.mark.asyncio
async def test_inside_stage_budget_failure_carries_paid_batches_once() -> None:
    provider = CallbackProvider(lambda _request: "")
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    sources = [
        {
            "source_id": f"source-{index}",
            "title": f"Source {index}",
            "abstract": "Synthetic evidence sentence.",
        }
        for index in range(20)
    ]
    budget = ExecutionBudget(max_total_tokens=8000, active_step_index=2)

    def respond(request: LLMRequest) -> str:
        if len(provider.requests) == 1:
            budget.max_total_tokens = 1000
        return json.dumps(
            {
                "records": [
                    {
                        "source_id": item["source_id"],
                        "part_id": item["part_id"],
                        "claims": [],
                    }
                    for item in json.loads(request.prompt)["records"]
                ]
            }
        )

    provider.callback = respond

    with pytest.raises(StepExecutionError) as error:
        await executor.execute(
            {
                "type": "extract",
                "parameters": {
                    "contract_version": 1,
                    "model_id": "test-model",
                    "output_kind": "claims",
                    "max_tokens": 1000,
                },
            },
            {"contract_version": 1, "source_records": sources},
            budget=budget,
        )

    assert error.value.consumed_tokens == 60
    assert error.value.model_calls == len(provider.requests) == 1
    assert len(error.value.batch_metadata) == 1
    assert error.value.batch_metadata[0]["input_hash"]


@pytest.mark.asyncio
async def test_direct_paused_event_carries_continued_after_failure_context() -> None:
    class PauseExecutor(StepExecutor):
        async def execute(
            self, step_def: dict[str, Any], context: dict[str, Any], **_: Any
        ) -> StepResult:
            del step_def, context
            from src.services.research_engine.verification import QualityMark

            return StepResult(
                output={
                    "contract_version": 1,
                    "stage_type": "export",
                    "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
                    "format": "json",
                    "exported": {},
                },
                quality_marks=[QualityMark("manual_review", False, "still failed")],
            )

    events = [
        event
        async for event in WorkflowEngine(PauseExecutor({}, {})).run(
            {"steps": [{"type": "export", "parameters": {"contract_version": 1}}]},
            uuid4(),
            initial_context={
                "contract_version": 1,
                "verification": {"passed": False, "continued_after_failure": True},
            },
        )
    ]

    paused = next(event for event in events if event["event"] == "run_paused")
    assert paused["context"]["verification"]["passed"] is False
    assert paused["context"]["verification"]["continued_after_failure"] is True


def test_structured_prompt_records_are_packed_only_at_object_boundaries() -> None:
    objects = [{"evidence_id": f"e{index}", "quote": "x" * 100} for index in range(6)]
    records = [
        {
            "source_id": SOURCE_ID,
            "title": "Synthetic",
            "evidence_level": "abstract",
            "full_text": json.dumps(objects),
        }
    ]
    batches = build_prompt_batches(
        records,
        {"system_prompt": "instructions"},
        500,
        structured_records=True,
    )

    returned = []
    for batch in batches:
        for record in batch["records"]:
            value = json.loads(record["text"])
            returned.extend(value if isinstance(value, list) else [value])
    assert returned == objects


def test_individually_oversized_structured_record_is_rejected_before_provider() -> None:
    provider = CallbackProvider(lambda _request: '{"sections":[]}')
    executor = StepExecutor(connectors={}, providers={"test-model": provider})
    records = [
        {
            "source_id": SOURCE_ID,
            "title": "Synthetic",
            "abstract": json.dumps({"evidence_id": "e1", "quote": "x" * 20_000}),
        }
    ]

    with pytest.raises(
        (StepExecutionError, ValueError), match="oversized|byte limit|room"
    ):
        executor._prepare_batches(
            step_def={},
            params={},
            context={},
            stage_type="synthesize",
            records=records,
            structured_records=True,
        )

    assert provider.requests == []
