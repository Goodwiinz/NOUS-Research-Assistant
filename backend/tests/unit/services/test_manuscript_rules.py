"""Pure manuscript release rules (GOO-315).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-315 section):
``_claim_support`` accepting any live release for the draft (the content-hash
comparison dropped) makes ``-k claim_support`` fail.
"""

import hashlib
import io
import json
import zipfile
from typing import Any
from uuid import uuid4

from src.services.research import manuscript_rules as rules
from src.services.research.manuscript_rules import (
    CheckInputs,
    DeviationIn,
    FigureIn,
    ReleaseIn,
    RunIn,
    VenueIn,
    VersionIn,
)
from src.services.research_engine.audit_bundle import (
    SCHEMA,
    Part,
    verify_bundle,
    write_zip,
)

HASH = "a" * 64
OTHER = "b" * 64
PV = str(uuid4())
META: dict[str, Any] = {
    "project_id": str(uuid4()),
    "generated_at": "2026-09-30T00:00:00+00:00",
    "deployment_sha": None,
    "protocol_version_id": None,
    "stream_heads": {},
}


def _state(inputs: CheckInputs, key: str) -> str:
    return str(rules.evaluate(inputs)[key]["state"])


def test_claim_support_requires_live_draft_release_same_hash() -> None:
    blocker = {"code": "unassessed", "detail": "No assessment", "claim_version_id": "c"}
    live = ReleaseIn(id="r", content_hash=HASH, live=True)
    assert _state(CheckInputs(HASH, draft_release=live), "claim_support") == "pass"
    other_version = ReleaseIn(id="r", content_hash=OTHER, live=True)
    result = rules.evaluate(
        CheckInputs(HASH, draft_release=other_version, claim_blockers=(blocker,))
    )["claim_support"]
    assert result["state"] == "fail"
    assert result["items"] == [
        {"code": "unassessed", "detail": "No assessment", "ref": "c"}
    ]
    stale = ReleaseIn(id="r", content_hash=HASH, live=False)
    assert _state(CheckInputs(HASH, draft_release=stale), "claim_support") == "fail"
    none = rules.evaluate(CheckInputs(HASH))["claim_support"]
    assert none["items"][0]["code"] == "no_live_release"


def test_method_adherence_accepts_approved_amendment_only() -> None:
    run = RunIn(id="run", conformance_status="deviated")
    deviation = DeviationIn(
        id="dev", protocol_version_id=PV, run_id="run", disposition="approved"
    )
    base = CheckInputs(
        HASH, protocol_version_id=PV, runs=(run,), deviations=(deviation,)
    )
    result = rules.evaluate(base)["method_adherence"]
    assert result["state"] == "fail"  # the "approved" disposition text is not trusted
    assert {(i["code"], i["ref"]) for i in result["items"]} == {
        ("run_not_conformant", "run"),
        ("deviation_unamended", "dev"),
    }
    for not_amendment in (
        VersionIn("v2", PV, "initial", "approved"),
        VersionIn("v2", PV, "amendment", "draft"),
        VersionIn("v2", str(uuid4()), "amendment", "approved"),
    ):
        inputs = CheckInputs(
            HASH,
            protocol_version_id=PV,
            runs=(run,),
            deviations=(deviation,),
            versions=(not_amendment,),
        )
        assert _state(inputs, "method_adherence") == "fail", not_amendment
    for status in ("approved", "superseded"):
        inputs = CheckInputs(
            HASH,
            protocol_version_id=PV,
            runs=(run,),
            deviations=(deviation,),
            versions=(VersionIn("v2", PV, "amendment", status),),
        )
        assert _state(inputs, "method_adherence") == "pass"
    assert _state(CheckInputs(HASH), "method_adherence") == "unknown"
    plan_only = CheckInputs(
        HASH, protocol_version_id=PV, runs=(RunIn("r2", "plan_verified"),)
    )
    assert _state(plan_only, "method_adherence") == "fail"
    conformant = CheckInputs(
        HASH, protocol_version_id=PV, runs=(RunIn("r3", "conformant"),)
    )
    assert _state(conformant, "method_adherence") == "pass"


def test_synthesis_appraisal_not_applicable_unless_protocol_requires() -> None:
    assert _state(CheckInputs(HASH), "synthesis_appraisal") == "not_applicable"
    missing = rules.evaluate(
        CheckInputs(HASH, synthesis_required=True, appraisal_required=True)
    )["synthesis_appraisal"]
    assert missing["state"] == "fail"
    assert [i["code"] for i in missing["items"]] == [
        "synthesis_not_current",
        "appraisal_incomplete",
    ]
    done = CheckInputs(
        HASH,
        synthesis_required=True,
        synthesis_current=True,
        appraisal_required=True,
        appraisal_complete=True,
    )
    assert _state(done, "synthesis_appraisal") == "pass"


def test_reproducibility_reported_never_obligation() -> None:
    assert "experiment_reproducibility" not in rules.VERIFIED_OBLIGATIONS
    assert "reporting_completeness" not in rules.VERIFIED_OBLIGATIONS
    assert _state(CheckInputs(HASH), "experiment_reproducibility") == "not_applicable"
    unattempted = CheckInputs(
        HASH, figures=(FigureIn("f1", "complete", "not_attempted"),)
    )
    assert _state(unattempted, "experiment_reproducibility") == "unknown"
    reproduced = CheckInputs(HASH, figures=(FigureIn("f1", "complete", "reproduced"),))
    assert _state(reproduced, "experiment_reproducibility") == "pass"
    for figure in (
        FigureIn("f1", "incomplete", "reproduced"),
        FigureIn("f1", "complete", "not_reproduced"),
    ):
        failing = CheckInputs(
            HASH,
            figures=(figure,),
            draft_release=ReleaseIn("r", HASH, True),
            protocol_version_id=PV,
        )
        checks = rules.evaluate(failing)
        assert checks["experiment_reproducibility"]["state"] == "fail"
        assert rules.failing_obligations(checks) == []
    assert (
        _state(CheckInputs(HASH, prisma="inconsistent"), "reporting_completeness")
        == "fail"
    )
    assert (
        _state(CheckInputs(HASH, prisma="consistent"), "reporting_completeness")
        == "pass"
    )


def test_peer_review_not_applicable_without_rounds() -> None:
    assert _state(CheckInputs(HASH), "peer_review") == "not_applicable"
    assert _state(CheckInputs(HASH, has_review_rounds=True), "peer_review") == "pass"
    open_comment = {"comment_root_id": "c1", "state": "open", "anchor_state": "exact"}
    result = rules.evaluate(
        CheckInputs(HASH, has_review_rounds=True, open_obligations=(open_comment,))
    )["peer_review"]
    assert result == {
        "state": "fail",
        "items": [
            {"code": "open_comment", "detail": "open; anchor exact", "ref": "c1"}
        ],
    }


def test_failing_obligations_lists_each() -> None:
    checks = rules.evaluate(
        CheckInputs(
            HASH,
            synthesis_required=True,
            has_review_rounds=True,
            open_obligations=({"comment_root_id": "c"},),
        )
    )
    assert set(checks) == set(rules.CHECK_KEYS)
    assert rules.failing_obligations(checks) == list(rules.VERIFIED_OBLIGATIONS)
    assert rules.failing_obligations({}) == list(rules.VERIFIED_OBLIGATIONS)
    passing = {key: {"state": "not_applicable"} for key in rules.CHECK_KEYS}
    assert rules.failing_obligations(passing) == []
    del passing["venue"]  # a missing result fails
    assert rules.failing_obligations(passing, ("claim_support", "venue")) == ["venue"]


def test_snapshot_hash_stable_across_key_order() -> None:
    first = {"schema": rules.SNAPSHOT_SCHEMA, "draft": {"id": "d", "version": 1}}
    second = {"draft": {"version": 1, "id": "d"}, "schema": rules.SNAPSHOT_SCHEMA}
    assert rules.snapshot_hash(first) == rules.snapshot_hash(second)
    assert rules.snapshot_hash(first) != rules.snapshot_hash({**first, "x": 1})


def test_reference_mapping_follows_snapshot_keys() -> None:
    bib = "@article{doc2,\n    title = {B}\n}\n\n@misc{doc1,\n    title = {A}\n}\n"
    snapshot = {"references": [{"key": "doc1"}, {"key": "doc2"}]}
    mapping = rules.reference_mapping(snapshot, bib)
    assert [key for key, _ in mapping] == ["doc1", "doc2"]
    entry = "@misc{doc1,\n    title = {A}\n}"
    assert mapping[0][1] == hashlib.sha256(entry.encode()).hexdigest()
    try:
        rules.reference_mapping({"references": [{"key": "doc1"}]}, bib)
    except ValueError:
        pass
    else:
        raise AssertionError("an extra bib entry must not map")


def test_write_zip_schema_keyword_default_unchanged() -> None:
    part = Part("a.md", None, b"alpha", hashlib.sha256(b"alpha").hexdigest())
    default, default_sha = write_zip([part], **META)
    explicit, explicit_sha = write_zip([part], **META, schema=SCHEMA)
    assert (default, default_sha) == (explicit, explicit_sha)
    other, _ = write_zip([part], **META, schema=rules.PACKAGE_SCHEMA)
    with zipfile.ZipFile(io.BytesIO(other)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["schema"] == rules.PACKAGE_SCHEMA
    assert verify_bundle(other, schema=rules.PACKAGE_SCHEMA)["parts"] == ["a.md"]


# --- GOO-316: statements and venue -------------------------------------------


def test_statements_and_venue_are_obligations_only_when_bound() -> None:
    plain = rules.evaluate(CheckInputs(HASH))
    assert plain["statements"]["state"] == plain["venue"]["state"] == "not_applicable"
    assert rules.obligations({}) == rules.VERIFIED_OBLIGATIONS
    bound = {"statements": {"statement_set_id": "s", "set_hash": HASH}}
    assert rules.obligations(bound)[-2:] == ("statements", "venue")
    unapproved = rules.evaluate(
        CheckInputs(HASH, statements_bound=True, unapproved_authors=("b",))
    )
    assert unapproved["statements"] == {
        "state": "fail",
        "items": [
            {
                "code": "author_not_approved",
                "detail": "No approval of the bound statement set",
                "ref": "b",
            }
        ],
    }
    assert "statements" in rules.failing_obligations(
        unapproved, rules.obligations(bound)
    )
    assert "statements" not in rules.failing_obligations(unapproved)


def test_venue_check_binds_the_exact_package_hash() -> None:
    passed = VenueIn("v1", HASH, 1, "pass")
    current = CheckInputs(
        HASH, statements_bound=True, package_sha256=HASH, venue_checks=(passed,)
    )
    assert _state(current, "venue") == "pass"
    changed = CheckInputs(
        HASH, statements_bound=True, package_sha256=OTHER, venue_checks=(passed,)
    )
    stale = rules.evaluate(changed)["venue"]
    assert stale["state"] == "unknown"
    assert stale["items"][0]["code"] == "venue_check_stale"
    old_profile = VenueIn("v0", HASH, 0, "pass")
    assert (
        _state(
            CheckInputs(
                HASH,
                statements_bound=True,
                package_sha256=HASH,
                venue_checks=(old_profile,),
            ),
            "venue",
        )
        == "unknown"
    )
    failed = VenueIn("v2", HASH, 1, "fail", ("required",))
    latest = rules.evaluate(
        CheckInputs(
            HASH,
            statements_bound=True,
            package_sha256=HASH,
            venue_checks=(passed, failed),
        )
    )["venue"]
    assert latest["state"] == "fail"
    assert latest["items"][0]["detail"] == "required"
    unpackaged = CheckInputs(HASH, statements_bound=True)
    assert rules.evaluate(unpackaged)["venue"]["items"][0]["code"] == (
        "venue_not_checked"
    )
