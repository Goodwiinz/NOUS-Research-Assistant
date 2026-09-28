"""Daily Brief prompt inputs are explicit, bounded, and safe to persist by hash."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.services.research_engine import contracts
from src.services.research_engine import step_executor as step_executor_module
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    merge_stage_output,
)
from src.services.research_engine.prompt_batches import MAX_PROMPT_BYTES
from src.services.research_engine.prompt_context import PromptContextBuilder
from src.services.research_engine.providers.base import LLMResponse
from src.services.research_engine.step_executor import StepExecutionError, StepExecutor


def _scope() -> dict[str, object]:
    return {
        "research_question": "Which interventions improve retention?",
        "inclusion_criteria": ["Randomized studies"],
        "exclusion_criteria": ["Animal studies"],
        "providers": ["openalex"],
        "limit_per_provider": 25,
        "notes": "Prefer results from the last five years",
        "confirmed": True,
        "actor_id": "00000000-0000-0000-0000-000000000001",
        "confirmed_at": "2026-09-27T12:00:00+00:00",
        "scope_hash": "a" * 64,
    }


def _capabilities() -> list[dict[str, object]]:
    return [
        {
            "id": "openalex",
            "label": "OpenAlex",
            "daily_brief_eligible": True,
            "available": True,
            "features": {"full_text": False, "date_filter": True, "cursor": True},
            "api_key": "must-never-leak",
            "internal_url": "https://internal.invalid",
        },
        {
            "id": "crossref",
            "label": "Crossref",
            "daily_brief_eligible": True,
            "available": True,
            "features": {"full_text": False, "date_filter": True, "cursor": True},
        },
    ]


def _upstream() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": {"model_calls": 1, "total_tokens": 50, "batches": []},
        "extractions": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "data": {"effect": "positive"},
                "evidence": [
                    {
                        "evidence_id": "e0001",
                        "pointer": "/effect",
                        "quote": "Retention improved.",
                    }
                ],
            }
        ],
        "unrelated_prior_output": "must-never-appear",
        "review_notes": "private reviewer note",
    }


def test_daily_brief_prompt_context_contains_only_declared_stage_inputs() -> None:
    """Passing an ambient run context could leak secrets or unrelated stages."""

    built = PromptContextBuilder.build(
        stage_type="synthesize",
        scope_confirmation=_scope(),
        capability_manifest=_capabilities(),
        upstream_envelope=_upstream(),
        target_schema={"type": "object", "required": ["sections"]},
        budget={"remaining_tokens": 4096, "remaining_calls": 3},
    )

    assert built.version == "daily-brief-v1"
    assert len(built.hash) == 64
    assert built.payload == {
        "prompt_context_version": "daily-brief-v1",
        "stage_type": "synthesize",
        "confirmed_scope": {
            "research_question": "Which interventions improve retention?",
            "inclusion_criteria": ["Randomized studies"],
            "exclusion_criteria": ["Animal studies"],
            "providers": ["openalex"],
            "limit_per_provider": 25,
            "notes": "Prefer results from the last five years",
        },
        "selected_capabilities": [
            {
                "id": "openalex",
                "label": "OpenAlex",
                "daily_brief_eligible": True,
                "available": True,
                "features": {
                    "full_text": False,
                    "date_filter": True,
                    "cursor": True,
                },
            }
        ],
        "upstream_envelope": {
            "contract_version": 1,
            "stage_type": "extract",
        },
        "evidence_ids": ["e0001"],
        "target_schema": {"type": "object", "required": ["sections"]},
        "budget": {"remaining_tokens": 4096, "remaining_calls": 3},
    }
    serialized = json.dumps(built.payload)
    assert "must-never-leak" not in serialized
    assert "internal.invalid" not in serialized
    assert "unrelated_prior_output" not in serialized
    assert "private reviewer note" not in serialized
    assert "Retention improved." not in serialized
    assert "actor_id" not in serialized
    assert "scope_hash" not in serialized


def test_prompt_context_rejects_unconfirmed_scope_and_wrong_immediate_envelope() -> (
    None
):
    """A browser draft or non-adjacent envelope cannot become model input."""

    unconfirmed = _scope()
    unconfirmed["confirmed"] = False
    with pytest.raises(ValueError, match="confirmed scope"):
        PromptContextBuilder.build(
            stage_type="synthesize",
            scope_confirmation=unconfirmed,
            capability_manifest=_capabilities(),
            upstream_envelope=_upstream(),
            target_schema={"type": "object"},
            budget={"remaining_tokens": 1, "remaining_calls": 1},
        )

    wrong_stage = _upstream()
    wrong_stage["stage_type"] = "search"
    with pytest.raises(ValueError, match="immediate upstream"):
        PromptContextBuilder.build(
            stage_type="synthesize",
            scope_confirmation=_scope(),
            capability_manifest=_capabilities(),
            upstream_envelope=wrong_stage,
            target_schema={"type": "object"},
            budget={"remaining_tokens": 1, "remaining_calls": 1},
        )


def test_only_version_and_hash_are_safe_persistence_metadata() -> None:
    """Persisting payload or rendered prompt content would expose research data."""

    built = PromptContextBuilder.build(
        stage_type="synthesize",
        scope_confirmation=_scope(),
        capability_manifest=_capabilities(),
        upstream_envelope=_upstream(),
        target_schema={"type": "object"},
        budget={"remaining_tokens": 1024, "remaining_calls": 1},
    )

    assert built.persistence_metadata() == {
        "prompt_context_version": "daily-brief-v1",
        "prompt_context_hash": built.hash,
    }
    assert "research_question" not in built.persistence_metadata()
    assert "prompt" not in built.persistence_metadata()


def test_legacy_prompt_path_remains_unchanged() -> None:
    """Adding Daily Brief context must not reinterpret a legacy free-form prompt."""

    legacy = {"query": "legacy", "content": "existing context"}

    assert PromptContextBuilder.build_legacy(legacy) is legacy


def test_public_immediate_stage_envelope_keeps_nested_copy_isolation() -> None:
    context = _search_context([])

    envelope = contracts.immediate_stage_envelope(context, "search")
    envelope["coverage"]["partial"] = True

    assert context["stage_results"]["0"]["coverage"] == {"partial": False}


def test_daily_brief_prompt_projection_does_not_copy_unused_envelope_payload() -> None:
    class _RejectDeepcopy:
        def __deepcopy__(self, _memo: dict[int, object]) -> object:
            raise AssertionError("unused upstream payload was deep-copied")

    expected_context = _search_context([])
    expected_context["stage_results"]["0"]["unused_payload"] = "ignored"
    expected = StepExecutor._daily_brief_prompt_context(
        stage_type="screen",
        context=expected_context,
        params={"_target_schema": {"type": "object"}},
        budget=None,
    )

    context = _search_context([])
    context["stage_results"]["0"]["unused_payload"] = _RejectDeepcopy()
    actual = StepExecutor._daily_brief_prompt_context(
        stage_type="screen",
        context=context,
        params={"_target_schema": {"type": "object"}},
        budget=None,
    )

    assert actual.payload == expected.payload
    assert actual.hash == expected.hash


class _ScreeningProvider:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> LLMResponse:
        self.requests.append(request)
        body = json.loads(request.prompt)
        return LLMResponse(
            content=json.dumps(
                {
                    "screening": [
                        {
                            "source_id": record["source_id"],
                            "part_id": record["part_id"],
                            "included": True,
                            "reason": "Matches confirmed scope",
                        }
                        for record in body["records"]
                    ]
                }
            ),
            model_id="test-model",
            input_tokens=10,
            output_tokens=5,
        )


class _ScreenAndExtractionProvider(_ScreeningProvider):
    def __init__(self, *, malformed_record_index: int | None = None) -> None:
        super().__init__()
        self.malformed_record_index = malformed_record_index
        self.extraction_record_count = 0

    async def complete(self, request: Any) -> LLMResponse:
        if "[stage_type=extract]" not in request.system_prompt:
            return await super().complete(request)

        self.requests.append(request)
        body = json.loads(request.prompt)
        records = []
        for record in body["records"]:
            self.extraction_record_count += 1
            finding: str | int = "reduced"
            if self.extraction_record_count == self.malformed_record_index:
                finding = 7
            records.append(
                {
                    "source_id": record["source_id"],
                    "part_id": record["part_id"],
                    "data": {"finding": finding},
                    "evidence": [
                        {
                            "pointer": "/finding",
                            "quote": "Treatment reduced the measured score",
                            "page_reference": None,
                        }
                    ],
                }
            )
        return LLMResponse(
            content=json.dumps({"records": records}),
            model_id="test-model",
            input_tokens=10,
            output_tokens=5,
        )


def _legacy_part_coverage(
    source_records: list[dict[str, Any]], requests: list[Any]
) -> list[dict[str, Any]]:
    """Reference the original ordered coverage algorithm for golden equality."""

    coverage: list[dict[str, Any]] = []
    for request in requests:
        for record in json.loads(request.prompt)["records"]:
            source = next(
                item
                for item in source_records
                if item.get("source_id") == record["source_id"]
            )
            original = source.get("full_text") or source.get("abstract") or ""
            earlier = [
                item for item in coverage if item["source_id"] == record["source_id"]
            ]
            offset = max((item["end_char"] for item in earlier), default=0)
            start = original.find(record["text"], offset)
            if start < 0:
                start = offset
            coverage.append(
                {
                    "source_id": record["source_id"],
                    "part_id": record["part_id"],
                    "start_char": start,
                    "end_char": start + len(record["text"]),
                }
            )
    return coverage


def _search_context(records: list[dict[str, Any]]) -> dict[str, Any]:
    search = {
        "contract_version": 1,
        "stage_type": "search",
        "usage": {"model_calls": 0, "total_tokens": 0, "batches": []},
        "source_records": records,
        "coverage": {"partial": False},
        "selected_sources": ["openalex"],
    }
    return {
        "contract_version": 1,
        "scope_confirmation": _scope(),
        "provider_manifest": _capabilities(),
        "source_records": records,
        "stage_results": {"0": search},
    }


@pytest.mark.asyncio
async def test_max_normal_daily_brief_screen_context_batches_content_once() -> None:
    """Twenty-five abstracts must fit by batching instead of duplicating all text."""

    records = [
        {
            "source_id": f"source-{index:02d}",
            "title": f"Study {index}",
            "abstract": (f"abstract-{index} " + "x" * 800),
            "evidence_level": "abstract",
        }
        for index in range(25)
    ]
    provider = _ScreeningProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})  # type: ignore[dict-item]

    await executor.execute(
        {
            "type": "screen",
            "model_id": "test-model",
            "system_prompt_template": "Screen against $research_question",
            "parameters": {"contract_version": 1, "review_gate": "screening"},
        },
        _search_context(records),
    )

    assert len(provider.requests) > 1
    seen_ids: list[str] = []
    for request in provider.requests:
        assert (
            len(request.system_prompt.encode("utf-8"))
            + len(request.prompt.encode("utf-8"))
            <= MAX_PROMPT_BYTES
        )
        body = json.loads(request.prompt)
        fixed = body["context"]["prompt_context"]
        assert "source_records" not in fixed["upstream_envelope"]
        seen_ids.extend(record["source_id"] for record in body["records"])
    assert seen_ids == [record["source_id"] for record in records]


@pytest.mark.asyncio
async def test_daily_brief_stage_execution_does_not_mutate_input_context() -> None:
    records = [
        {
            "source_id": "source-a",
            "title": "Study A",
            "abstract": "Treatment reduced the measured score.",
            "evidence_level": "abstract",
        }
    ]
    provider = _ScreenAndExtractionProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})  # type: ignore[dict-item]
    screen_context = _search_context(records)
    screen_snapshot = copy.deepcopy(screen_context)

    screen_result = await executor.execute(
        {
            "type": "screen",
            "model_id": "test-model",
            "parameters": {"contract_version": 1, "review_gate": "screening"},
        },
        screen_context,
    )

    assert screen_context == screen_snapshot
    assert "_daily_brief_prompt_context" not in screen_context

    extract_context = merge_stage_output(screen_context, screen_result.output, 1)
    extract_snapshot = copy.deepcopy(extract_context)
    await executor.execute(
        {
            "type": "extract",
            "model_id": "test-model",
            "parameters": {
                "contract_version": 1,
                "output_kind": "review_fields",
                "fields": ["finding"],
                "review_gate": "extraction",
            },
        },
        extract_context,
    )

    assert extract_context == extract_snapshot
    assert "_daily_brief_prompt_context" not in extract_context


@pytest.mark.asyncio
async def test_max_coverage_keeps_legacy_offsets_and_request_order() -> None:
    long_text = (
        "Treatment reduced the measured score. " + ("background " * 80) + "\n"
    ) * 40
    records = [
        {
            "source_id": "source-long",
            "title": "Long study",
            "abstract": long_text,
            "evidence_level": "abstract",
        },
        *[
            {
                "source_id": f"source-{index:03d}",
                "title": f"Study {index}",
                "abstract": "Treatment reduced the measured score.",
                "evidence_level": "abstract",
            }
            for index in range(200)
        ],
    ]
    screen_provider = _ScreenAndExtractionProvider()
    screen_result = await StepExecutor(
        connectors={}, providers={"test-model": screen_provider}  # type: ignore[dict-item]
    ).execute(
        {
            "type": "screen",
            "model_id": "test-model",
            "parameters": {"contract_version": 1, "review_gate": "screening"},
        },
        _search_context(records),
    )
    expected_screen_coverage = _legacy_part_coverage(records, screen_provider.requests)
    expected_screen_order = [
        (item["source_id"], item["part_id"])
        for request in screen_provider.requests
        for item in json.loads(request.prompt)["records"]
    ]

    assert sum(source_id == "source-long" for source_id, _ in expected_screen_order) > 1
    assert [
        (item["source_id"], item["part_id"])
        for item in screen_result.output["screening"]
    ] == expected_screen_order
    assert screen_result.output["processing_coverage"]["0"]["source_parts"] == (
        expected_screen_coverage
    )

    extract_context = merge_stage_output(
        _search_context(records), screen_result.output, 1
    )
    extract_provider = _ScreenAndExtractionProvider()
    extract_result = await StepExecutor(
        connectors={}, providers={"test-model": extract_provider}  # type: ignore[dict-item]
    ).execute(
        {
            "type": "extract",
            "model_id": "test-model",
            "parameters": {
                "contract_version": 1,
                "output_kind": "review_fields",
                "fields": ["finding"],
                "review_gate": "extraction",
            },
        },
        extract_context,
    )
    expected_extract_coverage = _legacy_part_coverage(
        records, extract_provider.requests
    )
    expected_extract_order = [
        (item["source_id"], item["part_id"])
        for request in extract_provider.requests
        for item in json.loads(request.prompt)["records"]
    ]

    assert list(
        dict.fromkeys(source_id for source_id, _ in expected_extract_order)
    ) == [record["source_id"] for record in records]
    assert [
        part_id
        for source_id, part_id in expected_extract_order
        if source_id == "source-long"
    ] == [
        f"p{index:04d}"
        for index in range(
            1,
            sum(source_id == "source-long" for source_id, _ in expected_extract_order)
            + 1,
        )
    ]
    assert extract_result.output["processing_coverage"]["0"]["source_parts"] == (
        expected_extract_coverage
    )


@pytest.mark.asyncio
async def test_max_daily_brief_extraction_batches_screening_control_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 4-by-50 limit must not turn screening metadata into a fixed prompt."""

    original_validate_user_schema = contracts.validate_user_schema
    contract_compiles: list[dict[str, Any]] = []
    stage_compiles: list[dict[str, Any]] = []

    def _compile_contract(schema: Any):
        contract_compiles.append(copy.deepcopy(schema))
        return original_validate_user_schema(schema)

    def _compile_stage(schema: Any):
        stage_compiles.append(copy.deepcopy(schema))
        return original_validate_user_schema(schema)

    monkeypatch.setattr(contracts, "validate_user_schema", _compile_contract)
    monkeypatch.setattr(step_executor_module, "validate_user_schema", _compile_stage)

    records = [
        {
            "source_id": f"source-{index:03d}",
            "title": f"Study {index}",
            "abstract": "Treatment reduced the measured score. " + "x" * 512,
            "evidence_level": "abstract",
        }
        for index in range(200)
    ]
    screening = [
        {
            "source_id": record["source_id"],
            "part_id": "p0001",
            "included": True,
            "reason": "Matches",
        }
        for record in records
    ]
    coverage = {
        "complete": True,
        "seen_source_ids": [record["source_id"] for record in records],
        "processed_source_ids": [record["source_id"] for record in records],
        "excluded_source_ids": [],
        "omitted": [],
        "source_parts": [
            {
                "source_id": record["source_id"],
                "part_id": "p0001",
                "start_char": 0,
                "end_char": len(record["abstract"]),
            }
            for record in records
        ],
    }
    screen = {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 4, "total_tokens": 40, "batches": []},
        "screening": screening,
        "included_source_ids": [record["source_id"] for record in records],
        "processing_coverage": {"1": coverage},
    }
    context = _search_context(records)
    context.update(
        {
            "included_source_ids": screen["included_source_ids"],
            "screening": screening,
            "processing_coverage": screen["processing_coverage"],
            "stage_results": {**context["stage_results"], "1": screen},
        }
    )

    class _MaxExtractionProvider:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> LLMResponse:
            self.requests.append(request)
            body = json.loads(request.prompt)
            return LLMResponse(
                content=json.dumps(
                    {
                        "records": [
                            {
                                "source_id": record["source_id"],
                                "part_id": record["part_id"],
                                "data": {"finding": "reduced"},
                                "evidence": [
                                    {
                                        "pointer": "/finding",
                                        "quote": "Treatment reduced the measured score",
                                        "page_reference": None,
                                    }
                                ],
                            }
                            for record in body["records"]
                        ]
                    }
                ),
                model_id="test-model",
                input_tokens=10,
                output_tokens=5,
            )

    provider = _MaxExtractionProvider()
    result = await StepExecutor(
        connectors={}, providers={"test-model": provider}  # type: ignore[dict-item]
    ).execute(
        {
            "type": "extract",
            "model_id": "test-model",
            "parameters": {
                "contract_version": 1,
                "output_kind": "review_fields",
                "fields": ["finding"],
                "review_gate": "extraction",
            },
        },
        context,
    )

    assert len(result.output["extractions"]) == 200
    assert [item["source_id"] for item in result.output["extractions"]] == [
        record["source_id"] for record in records
    ]
    assert all(
        item["data"] == {"finding": "reduced"} and item["merged_part_ids"] == ["p0001"]
        for item in result.output["extractions"]
    )
    assert len(contract_compiles) == 1
    assert len(stage_compiles) == 1
    assert contract_compiles == stage_compiles
    assert len(provider.requests) > 1
    assert all(
        len(request.system_prompt.encode("utf-8")) + len(request.prompt.encode("utf-8"))
        <= MAX_PROMPT_BYTES
        for request in provider.requests
    )
    fixed_contexts = [
        json.loads(request.prompt)["context"]["prompt_context"]
        for request in provider.requests
    ]
    assert fixed_contexts.count(fixed_contexts[0]) == len(fixed_contexts)
    assert fixed_contexts[0]["upstream_envelope"]["processing_coverage"] == {
        "1": {
            "complete": True,
            "seen_source_count": 200,
            "processed_source_count": 200,
            "excluded_source_count": 0,
            "source_parts_count": 200,
            "omitted_reason_counts": {},
        }
    }
    assert result.prompt_metadata == {
        "prompt_context_version": "daily-brief-v1",
        "prompt_context_hash": canonical_json_sha256(fixed_contexts[0]),
    }


@pytest.mark.asyncio
async def test_stage_compiled_schema_still_rejects_a_malformed_later_record() -> None:
    records = [
        {
            "source_id": f"source-{index:03d}",
            "title": f"Study {index}",
            "abstract": "Treatment reduced the measured score.",
            "evidence_level": "abstract",
        }
        for index in range(200)
    ]
    provider = _ScreenAndExtractionProvider(malformed_record_index=137)

    with pytest.raises(StepExecutionError) as error:
        await StepExecutor(
            connectors={}, providers={"test-model": provider}  # type: ignore[dict-item]
        ).execute(
            {
                "type": "extract",
                "model_id": "test-model",
                "parameters": {
                    "contract_version": 1,
                    "output_kind": "review_fields",
                    "fields": ["finding"],
                },
            },
            {"contract_version": 1, "source_records": records},
        )

    assert provider.extraction_record_count == 200
    assert isinstance(error.value.__cause__, ValueError)
    assert "required schema" in str(error.value.__cause__)


@pytest.mark.asyncio
async def test_executor_prompt_context_uses_derived_review_fields_schema() -> None:
    """The prompt schema must match the extractor's actual derived validator."""

    records = [
        {
            "source_id": "source-a",
            "title": "Study A",
            "abstract": "Retention improved in the treatment group.",
            "evidence_level": "abstract",
        }
    ]

    class _ExtractionProvider:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def complete(self, request: Any) -> LLMResponse:
            self.requests.append(request)
            body = json.loads(request.prompt)
            record = body["records"][0]
            return LLMResponse(
                content=json.dumps(
                    {
                        "records": [
                            {
                                "source_id": record["source_id"],
                                "part_id": record["part_id"],
                                "data": {"authors": None, "year": None},
                                "evidence": [],
                            }
                        ]
                    }
                ),
                model_id="test-model",
                input_tokens=10,
                output_tokens=5,
            )

    screen = {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 1, "total_tokens": 1, "batches": []},
        "screening": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "included": True,
                "reason": "Matches",
            }
        ],
        "included_source_ids": ["source-a"],
        "processing_coverage": {},
    }
    context = _search_context(records)
    context.update(
        {
            "included_source_ids": ["source-a"],
            "screening": screen["screening"],
            "stage_results": {**context["stage_results"], "1": screen},
        }
    )
    provider = _ExtractionProvider()
    executor = StepExecutor(connectors={}, providers={"test-model": provider})  # type: ignore[dict-item]

    await executor.execute(
        {
            "type": "extract",
            "model_id": "test-model",
            "parameters": {
                "contract_version": 1,
                "output_kind": "review_fields",
                "fields": ["authors", "year"],
                "review_gate": "extraction",
            },
        },
        context,
    )

    body = json.loads(provider.requests[0].prompt)
    response_schema = body["context"]["prompt_context"]["target_schema"]
    data_schema = response_schema["properties"]["records"]["items"]["properties"][
        "data"
    ]
    assert data_schema["required"] == ["authors", "year"]
    assert data_schema["properties"]["authors"]["anyOf"] == [
        {"type": "array", "items": {"type": "string"}},
        {"type": "null"},
    ]
    assert data_schema["properties"]["year"] == {"type": ["integer", "null"]}
