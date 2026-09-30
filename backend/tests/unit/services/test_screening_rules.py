"""Pure GOO-301 screening rules: reasons, criterion version, observation shape."""

from typing import Any
from uuid import UUID

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


# --- GOO-302: derivation and reveal ------------------------------------------

_A, _B, _C = (UUID(int=i) for i in (1, 2, 3))


def _obs(n: int, reviewer: UUID, decision: str, reason: str | None = None) -> Any:
    return screening_rules.Obs(UUID(int=100 + n), reviewer, decision, reason)


def test_single_mode_resolves_on_first_observation() -> None:
    derived = screening_rules.derive("single", [_obs(1, _A, "include")])
    assert derived == screening_rules.Derived(
        "single", "include", None, [str(UUID(int=101))]
    )
    assert screening_rules.derive("single", []) is None


def test_dual_needs_two_distinct_reviewers() -> None:
    assert screening_rules.derive("dual_independent", [_obs(1, _A, "include")]) is None
    same_reviewer = [_obs(1, _A, "include"), _obs(2, _A, "include")]
    assert screening_rules.derive("dual_independent", same_reviewer) is None
    with pytest.raises(ValueError):
        screening_rules.derive("independent", [_obs(1, _A, "include")])


def test_dual_agreement_same_decision_and_reason() -> None:
    derived = screening_rules.derive(
        "dual_independent",
        [
            _obs(2, _B, "exclude", "wrong design"),
            _obs(1, _A, "exclude", "wrong design"),
        ],
    )
    assert derived == screening_rules.Derived(
        "agreement", "exclude", "wrong design", [str(UUID(int=101)), str(UUID(int=102))]
    )
    conflict = screening_rules.derive(
        "dual_independent", [_obs(1, _A, "include"), _obs(2, _B, "exclude")]
    )
    assert conflict is not None
    assert (conflict.basis, conflict.outcome) == ("conflict", None)


def test_full_text_different_reasons_is_conflict() -> None:
    derived = screening_rules.derive(
        "dual_independent",
        [
            _obs(1, _A, "exclude", "wrong design"),
            _obs(2, _B, "exclude", "wrong population"),
        ],
    )
    assert derived is not None
    assert (derived.basis, derived.outcome, derived.exclusion_reason) == (
        "conflict",
        None,
        None,
    )


@pytest.mark.parametrize("mode", screening_rules.MODES)
def test_uncertain_never_auto_resolves(mode: str) -> None:
    fresh = [_obs(1, _A, "uncertain"), _obs(2, _B, "uncertain")]
    derived = screening_rules.derive(mode, fresh[: screening_rules.REQUIRED[mode]])
    assert derived is not None
    assert (derived.basis, derived.outcome) == ("conflict", None)


def test_visible_only_own_or_revealed() -> None:
    own, peer = UUID(int=11), UUID(int=12)
    assert screening_rules.visible(_A, own, _A, set())
    assert not screening_rules.visible(_B, peer, _A, set())
    assert screening_rules.visible(_B, peer, _A, {peer})
    assert not screening_rules.visible(_B, peer, _C, {own})


def test_auto_resolution_id_is_deterministic_per_event() -> None:
    event = UUID(int=7)
    assert screening_rules.auto_resolution_id(event) == (
        screening_rules.auto_resolution_id(event)
    )
    assert screening_rules.auto_resolution_id(event) != (
        screening_rules.auto_resolution_id(UUID(int=8))
    )
