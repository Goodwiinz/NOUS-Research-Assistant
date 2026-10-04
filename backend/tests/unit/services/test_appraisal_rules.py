"""Pure RoB 2 structure, validation and derived appraisal status (GOO-309)."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research_engine import appraisal_rules as ar
from src.services.research_engine import protocol_methods as pm
from src.services.research_engine.contracts import canonical_json_sha256

pytestmark = pytest.mark.unit

RCT = "randomized_parallel_group"
KEY = ("study:1", "depressive_symptoms", "12 weeks")


def _snapshot(**appraisal_synthesis: Any) -> dict[str, Any]:
    return {
        "appraisal_synthesis": appraisal_synthesis,
        "outcomes": {
            "declared": [
                {
                    "key": "depressive_symptoms",
                    "label": "Depression",
                    "timepoints": ["12 weeks"],
                }
            ]
        },
    }


def _domain(judgment: str | None, **extra: Any) -> dict[str, Any]:
    rationale = None if judgment is None else f"{judgment} because"
    return {"judgment": judgment, "rationale": rationale, **extra}


def _all(judgment: str | None) -> dict[str, Any]:
    return {d: _domain(judgment) for d in ar.SPEC["domains"]}


def _row(
    judgments: dict[str, str | None],
    *,
    assessor: UUID | None = None,
    overall: str | None = None,
    resolves: list[UUID] | None = None,
    signals: dict[str, dict[str, str]] | None = None,
) -> ar.Row:
    domains = ar.normalize(
        {
            d: {**_domain(j), "signals": (signals or {}).get(d, {})}
            for d, j in judgments.items()
        }
    )
    return ar.Row(
        id=uuid4(),
        assessor_id=assessor or uuid4(),
        study_design=RCT,
        applicability="applicable",
        domains=domains,
        overall=overall,
        resolves=tuple(resolves or ()),
    )


def test_protocol_without_appraisal_or_outcomes_raises() -> None:
    method = {"instrument": "rob2", "version": "2019-08-22", "mode": "single"}
    snapshot = _snapshot(appraisal=method)
    assert pm.appraisal_method(snapshot) == ("rob2", "2019-08-22", "single")
    assert pm.declared_outcomes(snapshot) == {"depressive_symptoms": ("12 weeks",)}
    with pytest.raises(ValueError, match="no appraisal instrument"):
        pm.appraisal_method({"appraisal_synthesis": {"method": "narrative"}})
    with pytest.raises(ValueError, match="no appraisal instrument"):
        pm.appraisal_method({})
    for outcomes in ({"primary": "x"}, {"declared": []}, {"declared": [{"key": "a"}]}):
        with pytest.raises(ValueError, match="no outcomes"):
            pm.declared_outcomes({"outcomes": outcomes})
    duplicate = {"key": "a", "label": "A", "timepoints": ["t"]}
    with pytest.raises(ValueError, match="no outcomes"):
        pm.declared_outcomes({"outcomes": {"declared": [duplicate, duplicate]}})


def test_mode_must_be_screening_mode() -> None:
    method = {"instrument": "rob2", "version": "2019-08-22", "mode": "triple"}
    with pytest.raises(ValueError, match="no appraisal instrument"):
        pm.appraisal_method(_snapshot(appraisal=method))
    for mode in ("single", "dual_independent"):
        assert (
            pm.appraisal_method(_snapshot(appraisal={**method, "mode": mode}))[2]
            == mode
        )


def test_spec_hash_is_stable_and_spec_has_no_question_text() -> None:
    assert ar.SPEC_HASH == canonical_json_sha256(ar.SPEC)
    assert ar.spec("rob2", "2019-08-22") is ar.SPEC
    with pytest.raises(ValueError, match="Unknown appraisal instrument"):
        ar.spec("robins-i", "2016")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in {"text", "question", "guidance", "algorithm"}
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert isinstance(value, str) and len(value) <= 60, value

    walk(ar.SPEC)
    signals = [s for d in ar.SPEC["domains"].values() for s in d["signals"]]
    assert all(len(s) == 3 and s[1] == "." for s in signals)
    assert len(signals) == 22


def test_non_randomized_design_must_be_not_applicable_with_empty_domains() -> None:
    with pytest.raises(ValueError, match="does not apply to cohort"):
        ar.validate(ar.SPEC, "cohort", "applicable", _all("low"), "low")
    assert ar.validate(ar.SPEC, "cohort", "not_applicable", {}, None) == {}
    with pytest.raises(ValueError, match="empty domains"):
        ar.validate(ar.SPEC, "cohort", "not_applicable", _all("low"), None)
    with pytest.raises(ValueError, match="empty domains"):
        ar.validate(ar.SPEC, "cohort", "not_applicable", {}, "low")
    with pytest.raises(ValueError, match="assess it"):
        ar.validate(ar.SPEC, RCT, "not_applicable", {}, None)
    with pytest.raises(ValueError, match="Unknown study design"):
        ar.validate(ar.SPEC, "anecdote", "not_applicable", {}, None)


def test_unknown_signal_or_domain_or_response_rejected() -> None:
    cases = [
        ({"D6": _domain(None)}, "Unknown domain"),
        ({"D1": {**_domain(None), "signals": {"2.1": "Y"}}}, "Unknown signalling"),
        ({"D1": {**_domain(None), "signals": {"1.1": "Maybe"}}}, "Unknown response"),
        ({"D1": _domain("medium")}, "Unknown judgement"),
        ({"D1": {**_domain(None), "text": "x"}}, "Unknown domain field"),
        (
            {
                "D1": {
                    **_domain(None),
                    "evidence": [{"kind": "model", "id": str(uuid4())}],
                }
            },
            "Unknown evidence kind",
        ),
        (
            {
                "D1": {
                    **_domain(None),
                    "evidence": [{"kind": "observation", "id": str(uuid4())}] * 21,
                }
            },
            "at most 20",
        ),
    ]
    for domains, message in cases:
        with pytest.raises(ValueError, match=message):
            ar.validate(ar.SPEC, RCT, "applicable", domains, None)


def test_missing_answers_stay_null_and_overall_must_be_null() -> None:
    domains = ar.validate(ar.SPEC, RCT, "applicable", {"D1": _domain("low")}, None)
    assert domains["D1"]["signals"] == {"1.1": None, "1.2": None, "1.3": None}
    assert domains["D3"] == {
        "judgment": None,
        "signals": {"3.1": None, "3.2": None, "3.3": None, "3.4": None},
        "rationale": None,
        "evidence": [],
    }
    assert ar.overall_floor(domains) is None
    with pytest.raises(ValueError, match="stay unknown"):
        ar.validate(ar.SPEC, RCT, "applicable", {"D1": _domain("low")}, "high")


def test_overall_may_escalate_but_not_go_below_worst_domain() -> None:
    domains = {**_all("low"), "D2": _domain("some_concerns")}
    assert ar.overall_floor(ar.normalize(domains)) == "some_concerns"
    for overall in ("some_concerns", "high", None):
        ar.validate(ar.SPEC, RCT, "applicable", domains, overall)
    with pytest.raises(ValueError, match="below the worst domain"):
        ar.validate(ar.SPEC, RCT, "applicable", domains, "low")


def test_judgment_without_rationale_rejected() -> None:
    for rationale in (None, "", "   ", "x" * 4001):
        with pytest.raises(ValueError, match="rationale"):
            ar.validate(
                ar.SPEC,
                RCT,
                "applicable",
                {"D1": {"judgment": "low", "rationale": rationale}},
                None,
            )


def test_status_awaiting_agreed_conflict_adjudicated() -> None:
    judged: dict[str, str | None] = {d: "low" for d in ar.SPEC["domains"]}
    a = _row(judged, overall="low", signals={"D1": {"1.1": "Y"}})
    assert ar.status("dual_independent", [a], None).value == "awaiting_independent"
    assert ar.status("dual_independent", [a], None).current_ids == []
    b = _row(judged, overall="low", signals={"D1": {"1.1": "PY"}})
    agreed = ar.status("dual_independent", [a, b], None)
    assert agreed.value == "agreed" and agreed.unresolved_domains == []
    assert agreed.current_ids == sorted([a.id, b.id], key=str)
    # Same assessor twice does not reach the independent count.
    same = _row(judged, assessor=a.assessor_id, overall="low")
    assert ar.status("dual_independent", [a, same], None).value == (
        "awaiting_independent"
    )
    c = _row({**judged, "D3": "high"}, overall="high")
    conflict = ar.status("dual_independent", [a, c], None)
    assert conflict.value == "conflict" and conflict.unresolved_domains == ["D3"]
    unknown = _row({**judged, "D4": None})
    assert ar.status("dual_independent", [a, unknown], None).unresolved_domains == [
        "D4"
    ]
    adjudicated = _row(judged, overall="low", resolves=[a.id, c.id])
    done = ar.status("dual_independent", [a, c], adjudicated)
    assert done.value == "adjudicated" and done.current_ids == [adjudicated.id]
    stale = _row(judged, overall="low", resolves=[a.id, b.id])
    assert ar.status("dual_independent", [a, c], stale).value == "conflict"


def test_single_mode_reveals_on_first_submission() -> None:
    row = _row({"D1": "low"})
    assert ar.revealed_keys("single", {KEY: [row]}) == {KEY}
    assert ar.revealed_keys("dual_independent", {KEY: [row]}) == set()
    assert ar.status("single", [row], None).value == "agreed"
    assert ar.status("single", [row], None).unresolved_domains == [
        "D2",
        "D3",
        "D4",
        "D5",
    ]
