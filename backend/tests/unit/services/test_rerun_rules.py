"""GOO-313 pure rerun rules: eligibility, pre-declared rules, comparison."""

import hashlib
import json
from typing import Any

import pytest

from src.services.research_engine import manifest_rules
from src.services.research_engine import rerun_rules as rules

pytestmark = pytest.mark.unit

SVG = b"<svg/>"
CSV = b"n,mean\n3,5.0\n"
METRICS = json.dumps({"n": 3, "pooled": {"estimate": 0.5, "label": "g"}}).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest() -> dict[str, Any]:
    outputs = [
        ("figure.svg", "figure", SVG),
        ("metrics.json", "metrics", METRICS),
        ("table.csv", "table", CSV),
    ]
    return {
        "schema": manifest_rules.SCHEMA,
        "code": {"sha256": "a" * 64},
        "environment": {"template_id": "tid", "lock_sha256": "b" * 64},
        "seed": 1,
        "protocol_version_id": "p",
        "question_version_id": "q",
        "started_at": "t0",
        "completed_at": "t1",
        "status": "completed",
        "inputs": [{"name": "data.csv", "sha256": "c" * 64, "artifact_id": "i"}],
        "outputs": [
            {"name": n, "role": r, "sha256": _sha(d), "artifact_id": n}
            for n, r, d in outputs
        ],
    }


EXPECTED = {"figure.svg": SVG, "metrics.json": METRICS, "table.csv": CSV}


def _numeric_rule(abs_tol: float = 1e-12) -> dict[str, Any]:
    return {
        "schema": rules.RULE_SCHEMA,
        "outputs": [
            {"name": "figure.svg", "mode": "bytes"},
            {
                "name": "metrics.json",
                "mode": "json_numeric",
                "pointers": ["/pooled/estimate"],
                "abs": abs_tol,
                "rel": 0.0,
            },
            {"name": "table.csv", "mode": "bytes"},
        ],
    }


def test_rule_must_cover_every_output() -> None:
    manifest = _manifest()
    partial = {
        "schema": rules.RULE_SCHEMA,
        "outputs": [{"name": "figure.svg", "mode": "bytes"}],
    }
    with pytest.raises(ValueError, match="rule_must_cover_every_output"):
        rules.validate_rule(partial, manifest)
    twice = rules.default_rule(manifest)
    twice["outputs"].append({"name": "figure.svg", "mode": "bytes"})
    with pytest.raises(ValueError, match="rule_must_cover_every_output"):
        rules.validate_rule(twice, manifest)
    unknown = rules.default_rule(manifest)
    unknown["outputs"].append({"name": "other.txt", "mode": "bytes"})
    with pytest.raises(ValueError, match="unknown_output"):
        rules.validate_rule(unknown, manifest)
    bad = _numeric_rule(-1.0)
    with pytest.raises(ValueError, match="bad_tolerance"):
        rules.validate_rule(bad, manifest)
    # The default rule covers every output with byte equality.
    default = rules.default_rule(manifest)
    assert rules.validate_rule(default, manifest) == default
    assert [o["mode"] for o in default["outputs"]] == ["bytes"] * 3


def test_rule_hash_changes_when_tolerance_changes() -> None:
    manifest = _manifest()
    tight = rules.validate_rule(_numeric_rule(1e-12), manifest)
    loose = rules.validate_rule(_numeric_rule(1e-3), manifest)
    assert rules.rule_hash(tight) != rules.rule_hash(loose)
    # Normalized: declaration order does not change the hash.
    shuffled = _numeric_rule(1e-12)
    shuffled["outputs"].reverse()
    assert rules.rule_hash(rules.validate_rule(shuffled, manifest)) == rules.rule_hash(
        tight
    )


def test_bytes_mode_reports_both_digests() -> None:
    manifest = _manifest()
    rule = rules.default_rule(manifest)
    rows, reproduced = rules.compare(rule, manifest, EXPECTED, EXPECTED)
    assert reproduced and [r["equal"] for r in rows] == [True] * 3
    changed = {**EXPECTED, "figure.svg": b"<svg></svg>"}
    rows, reproduced = rules.compare(rule, manifest, changed, EXPECTED)
    assert not reproduced
    assert rows[0]["expected_sha256"] == _sha(SVG)
    assert rows[0]["actual_sha256"] == _sha(b"<svg></svg>")
    assert rows[0]["equal"] is False and rows[0]["reason"] is None


def _metrics(estimate: float, label: str = "g") -> bytes:
    return json.dumps(
        {"n": 3, "pooled": {"estimate": estimate, "label": label}}
    ).encode()


def test_json_numeric_within_and_outside_tolerance_reports_abs_diff() -> None:
    manifest = _manifest()
    rule = rules.validate_rule(_numeric_rule(1e-6), manifest)
    near = {**EXPECTED, "metrics.json": _metrics(0.5 + 1e-9)}
    rows, reproduced = rules.compare(rule, manifest, near, EXPECTED)
    assert reproduced and rows[1]["equal"] is False
    (numeric,) = rows[1]["numeric"]
    assert numeric["within"] and numeric["abs_diff"] == pytest.approx(1e-9)
    far = {**EXPECTED, "metrics.json": _metrics(0.5 + 1e-3)}
    rows, reproduced = rules.compare(rule, manifest, far, EXPECTED)
    assert not reproduced
    (numeric,) = rows[1]["numeric"]
    assert not numeric["within"] and numeric["abs_diff"] == pytest.approx(1e-3)
    assert (numeric["expected"], numeric["actual"]) == (0.5, 0.5 + 1e-3)


def test_json_numeric_non_listed_leaf_compared_exactly() -> None:
    manifest = _manifest()
    rule = rules.validate_rule(_numeric_rule(1.0), manifest)
    relabeled = {**EXPECTED, "metrics.json": _metrics(0.5, label="h")}
    rows, reproduced = rules.compare(rule, manifest, relabeled, EXPECTED)
    assert not reproduced and rows[1]["numeric"][0]["within"]
    assert rows[1]["reason"] == "non_numeric_mismatch"
    not_json = {**EXPECTED, "metrics.json": b"not json"}
    rows, reproduced = rules.compare(rule, manifest, not_json, EXPECTED)
    assert not reproduced and rows[1]["reason"] == "not_json"
    gone = {**EXPECTED, "metrics.json": json.dumps({"n": 3}).encode()}
    rows, reproduced = rules.compare(rule, manifest, gone, EXPECTED)
    assert not reproduced and rows[1]["reason"] == "pointer_missing"


def test_missing_output_not_reproduced_with_reason() -> None:
    manifest = _manifest()
    rule = rules.default_rule(manifest)
    partial = {"figure.svg": SVG, "metrics.json": METRICS}
    rows, reproduced = rules.compare(rule, manifest, partial, EXPECTED)
    assert not reproduced
    assert [r["name"] for r in rows] == ["figure.svg", "metrics.json", "table.csv"]
    assert rows[2]["reason"] == "missing_output" and rows[2]["actual_sha256"] is None


def _eligibility(**override: Any) -> list[str]:
    fields: dict[str, Any] = {
        "run_status": "completed",
        "conformance": "conformant",
        "manifest": _manifest(),
        "analyze_steps": 1,
        "artifacts": {"data.csv": (True, True), "main.py": (True, True)},
    }
    return rules.eligibility(**(fields | override))


def test_legacy_and_incomplete_manifest_ineligible_with_each_missing_path() -> None:
    assert _eligibility() == []
    assert _eligibility(manifest=None) == [
        "no_manifest",
        "manifest_incomplete:schema_version<2",
    ]
    incomplete = _manifest()
    incomplete["seed"] = None
    incomplete["environment"] = None
    assert _eligibility(manifest=incomplete) == [
        "manifest_incomplete:environment.lock_sha256",
        "manifest_incomplete:environment.template_id",
        "manifest_incomplete:seed",
    ]
    assert _eligibility(run_status="failed", conformance="plan_verified") == [
        "run_not_completed",
        "run_not_conformant",
    ]
    assert _eligibility(
        artifacts={"data.csv": (True, False), "main.py": (False, False)}
    ) == ["artifact_corrupt:data.csv", "artifact_missing:main.py"]


def test_multi_analyze_run_unsupported_workflow_shape() -> None:
    assert _eligibility(analyze_steps=2) == ["unsupported_workflow_shape"]
    assert _eligibility(analyze_steps=0) == ["unsupported_workflow_shape"]
