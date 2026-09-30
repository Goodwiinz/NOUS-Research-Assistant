"""Pure GOO-301 screening rules: reasons, criterion version, observation shape."""

from typing import Any

import pytest

from src.services.research_engine import screening_rules

REASONS = ["wrong population", "wrong design"]


def _snapshot(**overrides: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "eligibility": {"population": "adults"},
        "sources_search": {"databases": ["openalex"]},
        "selection": {"full_text_exclusion_reasons": list(REASONS)},
        "reviewer_mode": {"mode": "dual_independent"},
    }
    snapshot.update(overrides)
    return snapshot


def test_full_text_exclude_requires_protocol_reason() -> None:
    screening_rules.validate_observation(
        "full_text", "exclude", "wrong design", REASONS
    )
    with pytest.raises(ValueError):
        screening_rules.validate_observation("full_text", "exclude", None, REASONS)


def test_reason_outside_protocol_list_rejected() -> None:
    with pytest.raises(ValueError):
        screening_rules.validate_observation(
            "full_text", "exclude", "too long ago", REASONS
        )


def test_title_abstract_rejects_any_reason() -> None:
    screening_rules.validate_observation("title_abstract", "exclude", None, REASONS)
    with pytest.raises(ValueError):
        screening_rules.validate_observation(
            "title_abstract", "exclude", "wrong design", REASONS
        )


@pytest.mark.parametrize("stage", screening_rules.STAGES)
@pytest.mark.parametrize("decision", ["include", "uncertain"])
def test_include_and_uncertain_reject_reason(stage: str, decision: str) -> None:
    screening_rules.validate_observation(stage, decision, None, REASONS)
    with pytest.raises(ValueError):
        screening_rules.validate_observation(stage, decision, "wrong design", REASONS)


def test_unknown_stage_or_decision_rejected() -> None:
    with pytest.raises(ValueError):
        screening_rules.validate_observation("abstract", "include", None, REASONS)
    with pytest.raises(ValueError):
        screening_rules.validate_observation("full_text", "maybe", None, REASONS)


@pytest.mark.parametrize(
    "reasons",
    [
        ["wrong design", "wrong design"],
        ["wrong design", ""],
        ["wrong design", "   "],
        [" wrong design"],
        [],
        ["x" * 201],
        ["r%d" % i for i in range(51)],
        "wrong design",
        [1],
    ],
)
def test_duplicate_or_blank_reasons_are_malformed(reasons: Any) -> None:
    snapshot = _snapshot(selection={"full_text_exclusion_reasons": reasons})
    with pytest.raises(ValueError):
        screening_rules.exclusion_reasons(snapshot)


def test_absent_reasons_are_empty() -> None:
    assert screening_rules.exclusion_reasons(_snapshot(selection={"x": 1})) == []
    assert screening_rules.exclusion_reasons(_snapshot()) == REASONS


def test_criteria_hash_ignores_non_eligibility_sections() -> None:
    base = screening_rules.criteria_hash(_snapshot())
    assert len(base) == 64
    assert (
        screening_rules.criteria_hash(_snapshot(sources_search={"databases": []}))
        == base
    )
    assert (
        screening_rules.criteria_hash(_snapshot(reviewer_mode={"mode": "single"}))
        == base
    )
    assert (
        screening_rules.criteria_hash(_snapshot(eligibility={"population": "kids"}))
        != base
    )
    assert (
        screening_rules.criteria_hash(
            _snapshot(selection={"full_text_exclusion_reasons": ["wrong design"]})
        )
        != base
    )


def test_suggestion_rows_any_part_included() -> None:
    output = {
        "screening": [
            {"source_id": "a", "part_id": "1", "included": False, "reason": "off"},
            {"source_id": "a", "part_id": "2", "included": True, "reason": "fits"},
            {"source_id": "b", "part_id": "1", "included": False, "reason": "no"},
            {"source_id": "b", "part_id": "2", "included": False, "reason": "nope"},
        ]
    }
    assert screening_rules.suggestion_rows(output) == [
        ("a", "include", "fits"),
        ("b", "exclude", "no"),
    ]
    assert screening_rules.suggestion_rows({}) == []


@pytest.mark.parametrize(
    "section", [None, {}, {"mode": "independent"}, {"mode": 2}, "single"]
)
def test_reviewer_mode_missing_or_unknown_is_malformed(section: Any) -> None:
    snapshot = _snapshot()
    if section is None:
        del snapshot["reviewer_mode"]
    else:
        snapshot["reviewer_mode"] = section
    with pytest.raises(ValueError):
        screening_rules.reviewer_mode(snapshot)
    assert screening_rules.reviewer_mode(_snapshot()) == "dual_independent"
