"""Pure evidence-table, contradiction and certainty rules (GOO-310).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-310 section):

- ``build_rows`` keyed by ``report_id`` instead of the analysis unit:
  ``-k one_study`` fails, study S appears twice;
- ``cell_state`` treating no tip as ``value``: ``-k missing`` fails, a
  missing cell reports agreement.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research_engine import evidence_rules as rules
from src.services.research_engine import protocol_methods
from src.services.research_engine.contracts import canonical_json_sha256

pytestmark = pytest.mark.unit

OUTCOME, TIMEPOINT = "depressive_symptoms", "12 weeks"
FIELD = uuid4()
OTHER_FIELD = uuid4()


def _tip(
    unit: str,
    report: UUID,
    value: Any = None,
    missingness: str | None = None,
    field: UUID = FIELD,
) -> rules.Tip:
    return rules.Tip(
        accepted_value_id=uuid4(),
        document_id=uuid4(),
        report_id=report,
        unit=unit,
        field_id=field,
        source_hash="a" * 64,
        text_sha256="b" * 64,
        value=value,
        missingness=missingness,
    )


def _ratings(**overrides: int | None) -> dict[str, int | None]:
    return {**{d: 0 for d in rules.GRADE_DOMAINS}, **overrides}


def test_two_reports_of_one_study_make_one_row() -> None:
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    study, lone = f"study:{uuid4()}", f"report:{r3}"
    units = {study: [r1, r2], lone: [r3]}
    tips = [_tip(study, r1, 4.1), _tip(study, r2, 4.1), _tip(lone, r3, 5.0)]
    rows = rules.build_rows(units, tips, [FIELD], OUTCOME, TIMEPOINT)
    assert [row["unit"] for row in rows] == sorted([study, lone])
    by_unit = {row["unit"]: row for row in rows}
    s = by_unit[study]
    assert s["row_key"] == f"{study}|{OUTCOME}|{TIMEPOINT}"
    assert s["report_ids"] == sorted([str(r1), str(r2)])
    cell = s["cells"][str(FIELD)]
    assert cell["state"] == "value" and cell["value"] == 4.1
    assert len(cell["tips"]) == 2
    assert {t["report_id"] for t in cell["tips"]} == {str(r1), str(r2)}


def test_missing_cell_is_missing_not_blank_and_not_agreement() -> None:
    r1 = uuid4()
    unit = f"report:{r1}"
    rows = rules.build_rows({unit: [r1]}, [], [FIELD], OUTCOME, TIMEPOINT)
    cell = rows[0]["cells"][str(FIELD)]
    assert cell == {"state": "missing", "value": None, "missingness": None, "tips": []}
    assert rules.cell_state([]) == "missing"
    # A reported "not reported" is its own state, never a blank value.
    reported = [_tip(unit, r1, missingness="not_reported")]
    assert rules.cell_state(reported) == "missingness"
    mixed = [*reported, _tip(unit, r1, 4.0)]
    assert rules.cell_state(mixed) == "conflict"


def test_disagreeing_tips_in_one_unit_is_conflict_cell() -> None:
    r1, r2 = uuid4(), uuid4()
    study = f"study:{uuid4()}"
    tips = [_tip(study, r1, 4.1), _tip(study, r2, 4.4)]
    (row,) = rules.build_rows({study: [r1, r2]}, tips, [FIELD], OUTCOME, TIMEPOINT)
    cell = row["cells"][str(FIELD)]
    assert cell["state"] == "conflict" and cell["value"] is None
    assert {t["value"] for t in cell["tips"]} == {4.1, 4.4}
    # Tips on another field never leak into this cell.
    other = _tip(study, r1, 9.9, field=OTHER_FIELD)
    (row,) = rules.build_rows(
        {study: [r1, r2]}, [*tips, other], [FIELD], OUTCOME, TIMEPOINT
    )
    assert set(row["cells"]) == {str(FIELD)}


def test_table_hash_stable_and_order_of_field_ids_matters() -> None:
    r1 = uuid4()
    unit = f"report:{r1}"
    rows = rules.build_rows({unit: [r1]}, [], [FIELD, OTHER_FIELD], OUTCOME, TIMEPOINT)
    args: dict[str, Any] = {
        "protocol_version_id": uuid4(),
        "form_version_id": uuid4(),
        "outcome_key": OUTCOME,
        "timepoint": TIMEPOINT,
        "rows": rows,
        "excluded": [],
    }
    first = rules.table_hash(field_ids=[FIELD, OTHER_FIELD], **args)
    assert first == rules.table_hash(field_ids=[FIELD, OTHER_FIELD], **args)
    assert len(first) == 64
    assert first != rules.table_hash(field_ids=[OTHER_FIELD, FIELD], **args)
    body = {
        **{k: str(v) if "version" in k else v for k, v in args.items()},
        "field_ids": [str(FIELD), str(OTHER_FIELD)],
    }
    assert first == canonical_json_sha256(body)  # the contracts byte rule


def _row(kind: str, actor: UUID | None = None) -> rules.ChainRow:
    return rules.ChainRow(
        id=uuid4(),
        kind=kind,
        actor_id=actor or uuid4(),
        actor_role=(
            "adjudicator" if kind in ("resolved", "acknowledged") else "reviewer"
        ),
        explanation=f"{kind} because",
    )


def test_contradiction_status_ignores_dissent_and_keeps_it_listed() -> None:
    opened = _row("opened")
    assert rules.contradiction_status([opened]) == "unresolved"
    resolved = _row("resolved")
    dissent = _row("dissent")
    chain = [opened, resolved, dissent]
    assert rules.contradiction_status(chain) == "resolved"
    listed = rules.dissent(chain)
    assert [d["id"] for d in listed] == [str(dissent.id)]
    assert listed[0]["superseded"] is False
    # A later decision supersedes the earlier one, which stays listed.
    acknowledged = _row("acknowledged")
    chain.append(acknowledged)
    assert rules.contradiction_status(chain) == "acknowledged"
    listed = rules.dissent(chain)
    assert {d["id"]: d["superseded"] for d in listed} == {
        str(resolved.id): True,
        str(dissent.id): False,
    }
    with pytest.raises(ValueError):
        rules.contradiction_status([resolved])


def test_certainty_level_derivation_and_null_propagation() -> None:
    assert rules.certainty_level("high", _ratings()) == "high"
    assert (
        rules.certainty_level("high", _ratings(risk_of_bias=-1, imprecision=-1))
        == "low"
    )
    assert (
        rules.certainty_level("high", _ratings(risk_of_bias=-2, inconsistency=-2))
        == "very_low"
    )
    assert rules.certainty_level("low", _ratings(indirectness=-1)) == "very_low"
    assert rules.certainty_level("high", _ratings(risk_of_bias=None)) is None
    missing = _ratings()
    missing.pop("publication_bias")
    assert rules.certainty_level("high", missing) is None


def _check(**overrides: Any) -> None:
    unit = "study:s"
    appraisal = uuid4()
    args: dict[str, Any] = {
        "start": "high",
        "ratings": _ratings(risk_of_bias=-1),
        "level": "moderate",
        "rob_cited": [appraisal],
        "rob_required": [appraisal],
        "rob_statuses": {unit: "adjudicated"},
        "contradiction_cited": [],
        "contradiction_all": [],
        **overrides,
    }
    rules.check_certainty(**args)


def test_rob_rating_requires_every_unit_resolved() -> None:
    _check()
    with pytest.raises(ValueError, match="Risk of bias unresolved for report:r3"):
        _check(rob_statuses={"study:s": "agreed", "report:r3": "awaiting_independent"})
    with pytest.raises(ValueError, match="Risk of bias unresolved for report:r3"):
        _check(rob_statuses={"study:s": "agreed", "report:r3": None})
    with pytest.raises(ValueError, match="current appraisals"):
        _check(rob_cited=[uuid4()])
    # A null rating needs no resolved appraisal and stays unknown.
    _check(
        ratings=_ratings(risk_of_bias=None),
        level=None,
        rob_cited=[],
        rob_statuses={"report:r3": "awaiting_independent"},
    )
    with pytest.raises(ValueError, match="not derived"):
        _check(level="high")
    group = uuid4()
    with pytest.raises(ValueError, match="every contradiction"):
        _check(
            ratings=_ratings(risk_of_bias=-1, inconsistency=-1),
            level="low",
            contradiction_all=[group],
        )
    _check(
        ratings=_ratings(risk_of_bias=-1, inconsistency=-1),
        level="low",
        contradiction_all=[group],
        contradiction_cited=[group],
    )
    with pytest.raises(ValueError, match="rating"):
        _check(ratings=_ratings(risk_of_bias=-3))
    with pytest.raises(ValueError, match="starting level"):
        _check(start="moderate")


def test_stance_groups_need_both_supporting_and_opposing() -> None:
    def row(claim: str, stance: str) -> dict[str, Any]:
        return {
            "id": uuid4(),
            "claim_hash": claim,
            "claim_text": f"claim {claim}",
            "source_id": uuid4(),
            "stance": stance,
            "confidence": 0.9,
            "model_version": "m1",
            "inference_model_version": None,
        }

    rows = [
        row("c1", "supporting"),
        row("c1", "opposing"),
        row("c2", "supporting"),
        row("c2", "neutral"),
    ]
    (group,) = rules.stance_groups(rows)
    assert group["claim_hash"] == "c1"
    assert group["review_state"] == "unreviewed_model_suggestion"
    assert {s["stance"] for s in group["suggestions"]} == {"supporting", "opposing"}


def test_no_confidence_in_certainty() -> None:
    ratings: dict[str, Any] = {**_ratings(risk_of_bias=-1), "confidence": -2}
    assert rules.certainty_level("high", ratings) == "moderate"


def test_certainty_method_parses_the_protocol() -> None:
    snapshot = {
        "appraisal_synthesis": {
            "certainty": {"method": "grade", "version": "handbook-2013"}
        }
    }
    assert protocol_methods.certainty_method(snapshot) == ("grade", "handbook-2013")
    bad_snapshots: list[dict[str, Any]] = [
        {},
        {"appraisal_synthesis": {"certainty": {"method": "grade"}}},
        {"appraisal_synthesis": {"certainty": {"method": "other", "version": "1"}}},
    ]
    for bad in bad_snapshots:
        with pytest.raises(ValueError, match="no certainty method"):
            protocol_methods.certainty_method(bad)
