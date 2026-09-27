"""Daily Brief prompt inputs are explicit, bounded, and safe to persist by hash."""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.services.research_engine.prompt_batches import MAX_PROMPT_BYTES
from src.services.research_engine.prompt_context import PromptContextBuilder
from src.services.research_engine.providers.base import LLMResponse
from src.services.research_engine.step_executor import StepExecutor


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
