"""Daily Brief prompt inputs are explicit, bounded, and safe to persist by hash."""

from __future__ import annotations

import json

import pytest

from src.services.research_engine.prompt_context import PromptContextBuilder


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
