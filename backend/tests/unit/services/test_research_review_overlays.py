"""Approved-review overlays are immutable, deterministic stage handoffs."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.services.research_engine.contracts import (
    canonical_stage_output_hash,
    rehydrate_stage_outputs,
)
from src.services.research_engine.review_service import ResearchReviewService


def _result(*, first: Any = None, all_items: Sequence[Any] | None = None) -> Mock:
    result = Mock()
    result.scalars.return_value.first.return_value = first
    result.scalars.return_value.all.return_value = list(all_items or [])
    return result


def _usage() -> dict[str, object]:
    return {"model_calls": 1, "total_tokens": 2, "batches": []}


def _screen_output() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": _usage(),
        "screening": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "included": True,
                "reason": "model suggestion",
            },
            {
                "source_id": "source-a",
                "part_id": "p0002",
                "included": False,
                "reason": "model suggestion",
            },
            {
                "source_id": "source-b",
                "part_id": "p0001",
                "included": True,
                "reason": "model suggestion",
            },
        ],
        "included_source_ids": ["source-a", "source-b"],
        "processing_coverage": {},
    }


def _extract_output() -> dict[str, object]:
    return {
        "contract_version": 1,
        "stage_type": "extract",
        "usage": _usage(),
        "extractions": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "data": {"finding": "kept"},
                "evidence": [{"evidence_id": "e0001", "quote": "kept"}],
            },
            {
                "source_id": "source-b",
                "part_id": "p0001",
                "data": {"finding": "removed"},
                "evidence": [{"evidence_id": "e0002", "quote": "removed"}],
            },
        ],
        "processing_coverage": {},
    }


def _review(
    *,
    index: int,
    kind: str,
    output: dict[str, object],
    items: list[dict[str, object]],
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        reviewer_id=uuid4(),
        step_index=index,
        review_kind=kind,
        output_hash=canonical_stage_output_hash(output),
        decision="approve",
        decision_payload={"items": items},
        created_at="2026-09-27T12:00:00+00:00",
    )


def _service(
    run: Any, steps: Sequence[Any], reviews: Sequence[Any]
) -> ResearchReviewService:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _result(first=run),
            _result(all_items=reviews),
            _result(all_items=steps),
        ]
    )
    return ResearchReviewService(session)


@pytest.mark.asyncio
async def test_approved_overlays_only_change_downstream_inputs_and_cold_resume_matches() -> (
    None
):
    """Applying decisions to stored output itself would corrupt audit history."""

    run = SimpleNamespace(id=uuid4())
    screen_output = _screen_output()
    extract_output = _extract_output()
    steps = [
        SimpleNamespace(step_index=0, output=screen_output),
        SimpleNamespace(step_index=1, output=extract_output),
    ]
    reviews = [
        _review(
            index=0,
            kind="screening",
            output=screen_output,
            items=[
                {"source_id": "source-a", "part_id": "p0001", "decision": "include"},
                {"source_id": "source-a", "part_id": "p0002", "decision": "include"},
                {
                    "source_id": "source-b",
                    "part_id": "p0001",
                    "decision": "exclude",
                    "reason": "outside scope",
                },
            ],
        ),
        _review(
            index=1,
            kind="extraction",
            output=extract_output,
            items=[
                {"source_id": "source-a", "part_id": "p0001", "decision": "accept"},
                {
                    "source_id": "source-b",
                    "part_id": "p0001",
                    "decision": "reject",
                    "reason": "unsupported",
                },
            ],
        ),
    ]
    original_steps = copy.deepcopy([step.output for step in steps])
    uninterrupted_context = rehydrate_stage_outputs(steps)

    uninterrupted = await _service(run, steps, reviews).apply_approved_overlays(
        run_id=run.id,
        context=uninterrupted_context,
    )
    cold_context = rehydrate_stage_outputs(copy.deepcopy(steps))
    cold = await _service(run, steps, reviews).apply_approved_overlays(
        run_id=run.id,
        context=cold_context,
    )

    assert uninterrupted == cold
    assert uninterrupted["included_source_ids"] == ["source-a"]
    extractions = cast(list[object], extract_output["extractions"])
    assert uninterrupted["extractions"] == [extractions[0]]
    assert [step.output for step in steps] == original_steps
    assert canonical_stage_output_hash(steps[0].output) == reviews[0].output_hash
    assert canonical_stage_output_hash(steps[1].output) == reviews[1].output_hash


@pytest.mark.asyncio
async def test_declined_reviews_never_become_downstream_overlays() -> None:
    """A durable decline records audit history but cannot alter later inputs."""

    run = SimpleNamespace(id=uuid4())
    screen_output = _screen_output()
    step = SimpleNamespace(step_index=0, output=screen_output)
    declined = _review(
        index=0,
        kind="screening",
        output=screen_output,
        items=[
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "decision": "exclude",
                "reason": "declined",
            }
        ],
    )
    declined.decision = "decline"
    original = rehydrate_stage_outputs([step])

    projected = await _service(run, [step], []).apply_approved_overlays(
        run_id=run.id,
        context=original,
    )

    assert projected == original
    assert "approved_review_overlays" not in projected
