"""Pure GOO-320 rules: carry-forward by reference, targeted work, missing
history and reconciled update accounting (no database)."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research_engine import review_update_rules as rules
from src.services.research_engine.prisma import (
    Outcome,
    PrismaInconsistency,
    PrismaInputs,
    Record,
    Report,
    derive_prisma_flow,
)

pytestmark = pytest.mark.unit

TA, FT = "title_abstract", "full_text"
CRITERIA = {TA: "c" * 64, FT: "c" * 64}


def _parent(
    report: UUID, stage: str, outcome: str | None = "include", **kw: Any
) -> rules.ParentDecision:
    return rules.ParentDecision(
        report_id=report,
        stage=stage,
        resolution_id=kw.get("resolution_id", uuid4()),
        event_id=kw.get("event_id", uuid4()),
        outcome=outcome,
        basis=kw.get("basis", "agreement"),
        criteria_hash=kw.get("criteria_hash", "c" * 64),
    )


def _delta(**classes: str) -> dict[str, dict[str, Any]]:
    return {
        key: {"report_id": key, "class": value, "reason": None}
        for key, value in classes.items()
    }


def test_unchanged_carried_by_reference_without_actor_copy() -> None:
    r1 = uuid4()
    ta, ft = _parent(r1, TA), _parent(r1, FT)
    carried, work, attention = rules.carry_forward(
        _delta(**{str(r1): "unchanged"}), [ta, ft], CRITERIA, set(), reports=[r1]
    )
    assert [c["resolution_id"] for c in carried] == [
        str(ta.resolution_id),
        str(ft.resolution_id),
    ]
    assert carried[0]["event_id"] == str(ta.event_id)  # attribution by reference
    assert set(carried[0]) == {
        "report_id",
        "stage",
        "resolution_id",
        "event_id",
        "outcome",
        "basis",
    }
    assert work == {TA: [], FT: []} and attention == []
    # A title/abstract exclude carries no full-text stage at all.
    r2 = uuid4()
    carried, _, _ = rules.carry_forward(
        _delta(**{str(r2): "unchanged"}),
        [_parent(r2, TA, "exclude")],
        CRITERIA,
        set(),
        reports=[r2],
    )
    assert [c["stage"] for c in carried] == [TA]


def test_changed_and_corrected_go_to_required_work() -> None:
    new, changed, corrected = uuid4(), uuid4(), uuid4()
    delta = _delta(
        **{
            str(new): "new",
            str(changed): "changed",
            str(corrected): "corrected_retracted",
        }
    )
    parents = [_parent(changed, TA), _parent(corrected, TA), _parent(corrected, FT)]
    carried, work, attention = rules.carry_forward(
        delta, parents, CRITERIA, set(), reports=[new, changed, corrected]
    )
    assert carried == [] and attention == []
    assert set(work[TA]) == {new, changed, corrected}
    assert work[FT] == []  # full text waits for a new title/abstract include
    # A merge or split since the parent blocks carrying an unchanged report.
    merged = uuid4()
    carried, work, _ = rules.carry_forward(
        _delta(**{str(merged): "unchanged"}),
        [_parent(merged, TA)],
        CRITERIA,
        {merged},
        reports=[merged],
    )
    assert carried == [] and work[TA] == [merged]


def test_changed_criteria_hash_blocks_carry_forward() -> None:
    r = uuid4()
    parents = [_parent(r, TA), _parent(r, FT, criteria_hash="c" * 64)]
    criteria = {TA: "c" * 64, FT: "d" * 64}
    carried, work, _ = rules.carry_forward(
        _delta(**{str(r): "unchanged"}), parents, criteria, set(), reports=[r]
    )
    assert [c["stage"] for c in carried] == [TA]
    assert work[FT] == [r]
    carried, work, _ = rules.carry_forward(
        _delta(**{str(r): "unchanged"}),
        parents,
        {TA: "e" * 64, FT: "e" * 64},
        set(),
        reports=[r],
    )
    assert carried == [] and work[TA] == [r] and work[FT] == []


def test_unknown_goes_to_needs_attention_not_carried() -> None:
    r, outside = uuid4(), uuid4()
    delta = {
        str(r): {"report_id": str(r), "class": "unknown", "reason": "not_returned"}
    }
    parents = [_parent(r, TA)]
    carried, work, attention = rules.carry_forward(
        delta, parents, CRITERIA, set(), reports=[r, outside]
    )
    assert carried == [] and work == {TA: [], FT: []}
    expected: list[dict[str, Any]] = [
        {"report_id": str(r), "class": "unknown", "reason": "not_returned"},
        {"report_id": str(outside), "class": None, "reason": "not_in_delta"},
    ]
    assert attention == sorted(expected, key=lambda a: str(a["report_id"]))
    # Explicit carried_with_uncertainty is a flagged carry, never silent.
    carried, _, attention = rules.carry_forward(
        delta, parents, CRITERIA, set(), reports=[r], uncertain={r}
    )
    assert carried[0]["uncertain"] is True and attention == []


def test_missing_resolution_labelled_decision_missing() -> None:
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    unattributed = _parent(r3, TA, "exclude")
    parents = [
        _parent(r1, TA),  # include, but no full-text decision
        _parent(r2, TA, None, basis="conflict"),  # unresolved
        unattributed,
    ]
    missing = rules.missing_history(
        [r1, r2, r3], parents, unattributed={unattributed.resolution_id}
    )
    assert {(m["report_id"], m["stage"], m["kind"]) for m in missing} == {
        (str(r1), FT, "decision_missing"),
        (str(r2), TA, "decision_missing"),
        (str(r3), TA, "attribution_missing"),
    }
    assert not any(m.get("outcome") == "exclude" for m in missing)


# --- accounting ----------------------------------------------------------------


def _inputs(
    included: dict[UUID, UUID | None], records: dict[str, UUID], excluded: set[UUID]
) -> PrismaInputs:
    """Reports with records; ``included`` maps report -> confirmed study."""
    reports = set(records.values())
    outcomes = []
    seq = 0
    for report in sorted(reports, key=str):
        decisions: list[tuple[str, str, str | None]] = [(TA, "include", None)]
        if report in included:
            decisions.append((FT, "include", None))
        elif report in excluded:
            decisions.append((FT, "exclude", "wrong population"))
        for stage, decision, reason in decisions:
            seq += 1
            outcomes.append(
                Outcome(
                    stage, report, decision, reason, uuid4(), seq, False, "agreement"
                )
            )
    attempts = ()
    return PrismaInputs(
        records=tuple(Record(k, "import", "fixture", r) for k, r in records.items()),
        rejected_imports=0,
        workspace_documents=0,
        reports=tuple(
            Report(
                r,
                None,
                included.get(r),
                "confirmed" if included.get(r) else None,
            )
            for r in reports
        ),
        outcomes=tuple(outcomes),
        attempts=attempts,
        merges=(),
    )


def _retrieved(inputs: PrismaInputs) -> PrismaInputs:
    from dataclasses import replace

    from src.services.research_engine.prisma import Attempt

    full_text = {o.report_id for o in inputs.outcomes if o.stage == FT}
    return replace(
        inputs,
        attempts=tuple(
            Attempt(uuid4(), r, uuid4(), "retrieved", i + 1, None, uuid4())
            for i, r in enumerate(sorted(full_text, key=str))
        ),
    )


def _flows() -> dict[str, Any]:
    study = uuid4()
    r1, r2, r5, n1, n2 = (uuid4() for _ in range(5))
    parent = _retrieved(
        _inputs(
            {r1: None, r2: study, r5: None},
            {"import:1": r1, "import:2": r2, "import:5": r5},
            set(),
        )
    )
    successor = _retrieved(
        _inputs(
            {r1: None, r2: study, n1: None, n2: study},
            {
                "import:1": r1,
                "import:2": r2,
                "import:5": r5,
                "import:n1": n1,
                "import:n2": n2,
            },
            {r5},
        )
    )
    return {
        "parent": parent,
        "successor": successor,
        "r5": r5,
        "parent_reports": {r1, r2, r5},
    }


def _accounting(f: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    parent, successor = f["parent"], f["successor"]
    args: dict[str, Any] = {
        "parent_flow": derive_prisma_flow(parent),
        "successor_flow": derive_prisma_flow(successor),
        "delta_counts": {"changed": 1, "corrected_retracted": 1, "new": 2},
        "carried": [{"stage": TA}, {"stage": FT}],
        "parent_keys": {r.key for r in parent.records},
        "successor_keys": {r.key for r in successor.records},
        "parent_units": rules.included_units(parent),
        "successor_units": rules.included_units(successor),
        "parent_reports": {str(r) for r in f["parent_reports"]},
    }
    args.update(overrides)
    return rules.accounting(**args)


def test_accounting_reconciles_and_rejects_mismatch() -> None:
    f = _flows()
    boxes = _accounting(f)
    assert boxes["studies_in_previous_version"] == 3
    assert boxes["new_records_identified"] == 2
    assert boxes["amended_out"] == 1  # R5 include -> exclude
    assert boxes["new_studies_included"] == 1  # N1; N2 is a report of S
    assert boxes["total_studies_included"] == 3
    assert boxes["amended_inclusion"] == [
        {"report_id": str(f["r5"]), "from": "include", "to": "not_included"}
    ]
    assert all(boxes["checks"].values())
    # A parent include left undecided (needs attention) is withheld, never
    # reported as an amended decision.
    withheld = _accounting(f, withheld={str(f["r5"])})
    assert withheld["amended_inclusion"] == []
    assert withheld["withheld_reports"] == [str(f["r5"])]
    # A flow that disagrees with the decisions it should rest on is refused.
    wrong = derive_prisma_flow(f["successor"])
    wrong["counts"] = {**wrong["counts"], "included_studies": 5}
    with pytest.raises(PrismaInconsistency, match="reconcile"):
        _accounting(f, successor_flow=wrong)
    # A parent record missing from the successor cannot be explained.
    with pytest.raises(PrismaInconsistency, match="reconcile"):
        _accounting(f, successor_keys={"import:n1"})


def test_retried_import_keys_do_not_double_count() -> None:
    f = _flows()
    first = _accounting(f)
    retried = _accounting(
        f,
        successor_keys=sorted({r.key for r in f["successor"].records}) * 2,
    )
    assert retried["new_records_identified"] == first["new_records_identified"] == 2
    assert retried == first


def test_version_hash_is_canonical() -> None:
    assert rules.version_hash({"b": 1, "a": [2]}) == rules.version_hash(
        {"a": [2], "b": 1}
    )
    assert len(rules.version_hash({})) == 64
