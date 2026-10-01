"""Deterministic SMD Hedges' g with DerSimonian-Laird pooling (GOO-311).

Rows are built with GOO-310's own ``evidence_rules.build_rows`` from the gold
fixture, so the input shape is the real table shape. Mutation verification
(``docs/testing/agent-orchestration-mutation-checks.md``, GOO-311):

- ``J = 1`` in ``hedges_g`` fails ``-k gold`` (a ~1e-3 gold mismatch);
- dropping the ``max(0, ...)`` tau2 clamp fails ``-k homogeneous`` (tau2 < 0);
- expanding one input per report id fails ``-k duplicate`` (A weighted twice);
- skipping the ``invalid_variance`` check fails ``-k missing_variance``.
"""

import json
import random
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from src.services.research_engine import evidence_rules as er
from src.services.research_engine import protocol_methods
from src.services.research_engine import synthesis_rules as sr
from src.services.research_engine.contracts import canonical_json_bytes

pytestmark = pytest.mark.unit

GOLD = Path(__file__).resolve().parents[2] / "fixtures/synthesis/smd_dl_gold_v1.json"
NUMBERS = ("estimate", "se", "ci_low", "ci_high", "q", "tau2", "i2")


def _gold() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(GOLD.read_text(encoding="utf-8")))


def _case(case_id: str) -> dict[str, Any]:
    return next(c for c in _gold()["cases"] if c["id"] == case_id)


def _id(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "goo311/" + "/".join(parts))


ROLES = {role: str(_id("field", role)) for role in sr.ROLES}


def _fields(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    gold = _gold()
    return {
        ROLES[role]: {**gold["fields"][role], **(overrides or {}).get(role, {})}
        for role in sr.ROLES
    }


def _rows(studies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each report of a study carries the same accepted values, as GOO-304
    tips on its own document; ``build_rows`` folds them into one row."""
    units: dict[str, list[UUID]] = {}
    tips: list[er.Tip] = []
    for study in studies:
        for report in study["reports"]:
            report_id = _id("report", report)
            units.setdefault(study["unit"], []).append(report_id)
            for role, value in zip(sr.ROLES, study["arms"]):
                tips.append(
                    er.Tip(
                        accepted_value_id=_id("accepted", report, role),
                        document_id=_id("document", report),
                        report_id=report_id,
                        unit=study["unit"],
                        field_id=UUID(ROLES[role]),
                        source_hash="a" * 64,
                        text_sha256=None,
                        value=value,
                        missingness=None,
                    )
                )
    field_ids = [UUID(ROLES[role]) for role in sr.ROLES]
    return er.build_rows(units, tips, field_ids, "depressive_symptoms", "12 weeks")


def _run(
    studies: list[dict[str, Any]],
    overrides: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    failures = sr.check_config(_fields(overrides), ROLES, "12 weeks")
    included, excluded = sr.select_inputs(
        rows if rows is not None else _rows(studies), ROLES, []
    )
    return sr.compute(included, excluded, failures)


def _assert_gold(result: dict[str, Any], case_id: str) -> None:
    case = _case(case_id)
    tol = _gold()["tolerance"]["abs"]
    expected = case["expected"]
    assert result["status"] == "computed"
    assert result["numbers"]["df"] == expected["df"]
    for key in NUMBERS:
        assert result["numbers"][key] == pytest.approx(expected[key], abs=tol), key
    assert [u["g"] for u in result["included"]] == pytest.approx(expected["g"], abs=tol)
    assert [u["v"] for u in result["included"]] == pytest.approx(expected["v"], abs=tol)


def test_matches_gold_heterogeneous_within_declared_tolerance() -> None:
    _assert_gold(_run(_case("heterogeneous")["studies"]), "heterogeneous")


def test_matches_gold_homogeneous_tau2_and_i2_clamped() -> None:
    result = _run(_case("homogeneous")["studies"])
    _assert_gold(result, "homogeneous")
    assert result["numbers"]["tau2"] == 0.0
    assert result["numbers"]["i2"] == 0.0


def test_duplicate_reports_one_weight() -> None:
    case = _case("duplicate_report")
    result = _run(case["studies"])
    _assert_gold(result, case["expected_same_as"])
    single = _run(_case("heterogeneous")["studies"])
    a = result["included"][0]
    assert a["unit"] == "study:A" and len(a["report_ids"]) == 2
    assert len(a["accepted_value_ids"]) == 12  # both reports' tips, one unit
    assert [u["w_random"] for u in result["included"]] == [
        u["w_random"] for u in single["included"]
    ]
    assert result["excluded"] == []


def test_conflicting_reports_excluded_with_reason() -> None:
    studies = _case("heterogeneous")["studies"]
    rows = _rows(studies)
    # Report A-r2 disagrees on mean_i: GOO-310 marks the cell a conflict.
    rows[0]["cells"][ROLES["mean_i"]]["state"] = "conflict"
    result = _run(studies, rows=rows)
    assert [u["unit"] for u in result["included"]] == ["study:B", "study:C", "study:D"]
    assert result["excluded"][0]["unit"] == "study:A"
    assert result["excluded"][0]["reason"] == "conflicting_reports:mean_i"


def test_missing_variance_and_bad_n_are_structured_exclusions() -> None:
    case = _case("missing_variance")
    result = _run(case["studies"])
    _assert_gold(result, case["expected_same_as"])
    assert [(e["unit"], e["reason"]) for e in result["excluded"]] == [
        (e["unit"], e["reason"]) for e in case["expected_excluded"]
    ]
    bad_n = [*_case("heterogeneous")["studies"]]
    bad_n.append({"unit": "study:H", "reports": ["H-r1"], "arms": [1, 1, 1, 2, 1, 9]})
    bad_n.append({"unit": "study:I", "reports": ["I-r1"], "arms": [1, 1, 9.5, 2, 1, 9]})
    bad_n.append({"unit": "study:J", "reports": ["J-r1"], "arms": [1, 1, 9, "2", 1, 9]})
    reasons = {(e["unit"], e["reason"]) for e in _run(bad_n)["excluded"]}
    assert reasons == {
        ("study:H", "invalid_sample_size:i"),
        ("study:I", "invalid_sample_size:i"),
        ("study:J", "non_numeric:mean_c"),
    }


def test_missing_cell_is_missing_input() -> None:
    studies = _case("heterogeneous")["studies"]
    rows = _rows(studies)
    rows[1]["cells"][ROLES["n_i"]] = {"state": "missing", "tips": []}
    result = _run(studies, rows=rows)
    assert (result["excluded"][0]["unit"], result["excluded"][0]["reason"]) == (
        "study:B",
        "missing_input:n_i",
    )


@pytest.mark.parametrize("case_id", ["unit_mismatch", "timepoint_mismatch"])
def test_unit_and_timepoint_mismatch_fail_run_level(case_id: str) -> None:
    case = _case(case_id)
    result = _run(case["studies"], case["field_overrides"])
    assert result["status"] == "validation_failed"
    assert result["numbers"] is None
    run = [e["reason"] for e in result["excluded"] if e["unit"] is None]
    assert run == case["expected_run_failures"]
    assert all("g" not in u for u in result["included"])


def test_role_not_in_table_and_wrong_type() -> None:
    fields = _fields({"n_c": {"type": "text"}})
    del fields[ROLES["sd_c"]]
    reasons = [(e.reason, e.detail) for e in sr.check_config(fields, ROLES, "12 weeks")]
    assert ("role_not_in_table", "sd_c") in reasons
    assert ("wrong_field_type", "n_c") in reasons


def test_insufficient_studies() -> None:
    case = _case("single_study")
    result = _run(case["studies"])
    assert result["status"] == "validation_failed"
    assert [e["reason"] for e in result["excluded"]] == case["expected_run_failures"]


def test_pool_rejects_duplicate_unit_keys() -> None:
    with pytest.raises(ValueError, match="One weight per analysis unit"):
        sr.pool_dl([("study:A", -0.4, 0.03), ("study:A", -0.4, 0.03), ("b", 0, 1)])


def test_input_hash_stable_and_sensitive() -> None:
    cfg_hash = sr.config_hash(sr.config(ROLES))
    assert cfg_hash == sr.config_hash(sr.config(dict(reversed(list(ROLES.items())))))
    one = sr.input_hash("t" * 64, cfg_hash, _id("protocol"))
    assert one == sr.input_hash("t" * 64, cfg_hash, _id("protocol"))
    assert one != sr.input_hash("u" + "t" * 63, cfg_hash, _id("protocol"))
    other = sr.config_hash(sr.config({**ROLES, "sd_c": ROLES["sd_i"]}))
    assert one != sr.input_hash("t" * 64, other, _id("protocol"))


def test_order_independent_result() -> None:
    studies = _case("missing_variance")["studies"]
    rows = _rows(studies)
    first = _run(studies, rows=rows)
    shuffled = list(rows)
    random.Random(311).shuffle(shuffled)
    second = _run(studies, rows=shuffled)
    hashes = {
        sr.result_hash(r["included"], r["excluded"], r["numbers"])
        for r in (first, second)
    }
    assert len(hashes) == 1


def test_canonical_bytes_match_contracts() -> None:
    value = {"b": [1.5, None, "é"], "a": -0.30847113639164425}
    assert sr._canonical(value).encode("utf-8") == canonical_json_bytes(value)


def test_synthesis_selection_parses_one_pair() -> None:
    method = {
        "measure": "smd_hedges_g",
        "model": "random_effects_dl",
        "outcome": "depressive_symptoms",
        "timepoint": "12 weeks",
    }
    snapshot = {"appraisal_synthesis": {"synthesis": method}}
    assert protocol_methods.synthesis_selection(snapshot) == (
        "smd_hedges_g",
        "random_effects_dl",
        "depressive_symptoms",
        "12 weeks",
    )
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, protocol_methods.NO_SYNTHESIS),
        (
            {"appraisal_synthesis": {"synthesis": {"method": "narrative"}}},
            protocol_methods.NO_SYNTHESIS,
        ),
        (
            {"appraisal_synthesis": {"synthesis": {**method, "model": "reml"}}},
            protocol_methods.UNSUPPORTED_SYNTHESIS,
        ),
    ]
    for bad, message in cases:
        with pytest.raises(ValueError, match=message):
            protocol_methods.synthesis_selection(bad)
