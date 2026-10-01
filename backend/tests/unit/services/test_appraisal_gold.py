"""RoB 2 gold examples with unresolved states (GOO-309).

``rob2_gold_v1.json`` stays ``expert_reviewed: false`` until a named methods
expert signs it (then ``reviewer`` must be set too). Mutation verification
(``docs/testing/agent-orchestration-mutation-checks.md``, GOO-309): letting
``appraisal_rules.validate`` accept a non-null overall while a domain is
unknown fails case ``d3_unknown``'s negative control.
"""

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from src.services.research_engine import appraisal_rules as ar

pytestmark = pytest.mark.unit

GOLD = Path(__file__).resolve().parents[2] / "fixtures/appraisal/rob2_gold_v1.json"


def _gold() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(GOLD.read_text(encoding="utf-8")))


def test_header_pins_the_instrument_structure() -> None:
    gold = _gold()
    assert (gold["instrument"], gold["version"]) == ("rob2", "2019-08-22")
    assert gold["spec_hash"] == ar.SPEC_HASH
    assert gold["expert_reviewed"] is (gold["reviewer"] is not None)
    assert [c["id"] for c in gold["cases"]] == [
        "all_low",
        "one_high",
        "escalated",
        "d3_unknown",
        "cohort_not_applicable",
    ]


@pytest.mark.parametrize("case", _gold()["cases"], ids=lambda c: c["id"])
def test_case_validates_and_reproduces_expectations(case: dict[str, Any]) -> None:
    domains = ar.validate(
        ar.SPEC,
        case["study_design"],
        case["applicability"],
        case["domains"],
        case["overall"],
    )
    expected = case["expected"]
    assert ar.overall_floor(domains) == expected["overall_floor"]
    row = ar.Row(
        id=UUID(int=1),
        assessor_id=UUID(int=2),
        study_design=case["study_design"],
        applicability=case["applicability"],
        domains=domains,
        overall=case["overall"],
    )
    assert ar.status("single", [row], None).unresolved_domains == (
        expected["unresolved_domains"]
    )
    for domain_id, given in case["domains"].items():
        assert domains[domain_id]["rationale"] == given["rationale"]  # byte-identical
    if domains:  # not applicable stores {} and is never normalized
        assert ar.normalize(domains) == domains  # a round trip changes nothing


def test_overall_below_floor_or_over_unknown_is_rejected() -> None:
    cases = {c["id"]: c for c in _gold()["cases"]}
    one_high = cases["one_high"]
    with pytest.raises(ValueError, match="below the worst domain"):
        ar.validate(
            ar.SPEC,
            one_high["study_design"],
            "applicable",
            one_high["domains"],
            "some_concerns",
        )
    unknown = cases["d3_unknown"]
    for overall in ar.JUDGMENTS:
        with pytest.raises(ValueError, match="stay unknown"):
            ar.validate(
                ar.SPEC,
                unknown["study_design"],
                "applicable",
                unknown["domains"],
                overall,
            )
