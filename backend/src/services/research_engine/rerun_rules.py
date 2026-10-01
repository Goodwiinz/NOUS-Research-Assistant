"""Fresh-rerun eligibility and pre-declared comparison rules (GOO-313).

Pure: callers pass plain values and get plain values back. The only import
outside the standard library is ``manifest_rules``, which is itself
stdlib-only (``test_rerun_boundary`` enforces both).

A rule is declared (and hashed) before a rerun executes. It names every
output of the manifest exactly once: ``bytes`` compares digests, and
``json_numeric`` compares the listed JSON pointers within
``abs + rel * |expected|`` and every other leaf exactly.
"""

import copy
import json
import math
from typing import Any, Mapping

from src.services.research_engine import manifest_rules

RULE_SCHEMA = "nous.rerun-rule/1"
COMPARISON_SCHEMA = "nous.rerun-comparison/1"
MODES = ("bytes", "json_numeric")
MAX_POINTERS = 64
_MISSING = object()


def _outputs(manifest: Mapping[str, Any]) -> list[str]:
    return [str(o.get("name")) for o in manifest.get("outputs") or []]


def default_rule(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Byte equality for every output, in manifest order."""
    return {
        "schema": RULE_SCHEMA,
        "outputs": [{"name": name, "mode": "bytes"} for name in _outputs(manifest)],
    }


def _tolerance(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("bad_tolerance")
    if not math.isfinite(value) or value < 0:
        raise ValueError("bad_tolerance")
    return float(value)


def _entry(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping) or item.get("mode") not in MODES:
        raise ValueError("invalid_rule")
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("invalid_rule")
    if item["mode"] == "bytes":
        if set(item) != {"name", "mode"}:
            raise ValueError("invalid_rule")
        return {"name": name, "mode": "bytes"}
    if not set(item) <= {"name", "mode", "pointers", "abs", "rel", "non_numeric"}:
        raise ValueError("invalid_rule")
    pointers = item.get("pointers")
    if (
        not isinstance(pointers, list)
        or not pointers
        or len(pointers) > MAX_POINTERS
        or not all(isinstance(p, str) and p.startswith("/") for p in pointers)
        or len(set(pointers)) != len(pointers)
    ):
        raise ValueError("bad_tolerance")
    if item.get("non_numeric", "exact") != "exact":
        raise ValueError("bad_tolerance")
    return {
        "name": name,
        "mode": "json_numeric",
        "pointers": list(pointers),
        "abs": _tolerance(item.get("abs", 0.0)),
        "rel": _tolerance(item.get("rel", 0.0)),
        "non_numeric": "exact",
    }


def validate_rule(
    rule: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """The normalized rule (manifest output order, every key explicit), or
    ``ValueError``: ``invalid_rule``, ``unknown_output``,
    ``rule_must_cover_every_output`` or ``bad_tolerance``."""
    if not isinstance(rule, Mapping) or rule.get("schema") != RULE_SCHEMA:
        raise ValueError("invalid_rule")
    if set(rule) != {"schema", "outputs"} or not isinstance(rule["outputs"], list):
        raise ValueError("invalid_rule")
    entries = [_entry(item) for item in rule["outputs"]]
    names = _outputs(manifest)
    if any(e["name"] not in names for e in entries):
        raise ValueError("unknown_output")
    declared = [e["name"] for e in entries]
    if len(set(declared)) != len(declared) or set(declared) != set(names):
        raise ValueError("rule_must_cover_every_output")
    by_name = {e["name"]: e for e in entries}
    return {
        "schema": RULE_SCHEMA,
        "outputs": [by_name[n] for n in names if n in by_name],
    }


def rule_hash(rule: Mapping[str, Any]) -> str:
    return manifest_rules.manifest_hash(rule)


def eligibility(
    *,
    run_status: str,
    conformance: str,
    manifest: Mapping[str, Any] | None,
    analyze_steps: int,
    artifacts: Mapping[str, tuple[bool, bool]],
) -> list[str]:
    """Every reason the run cannot be rerun (empty when eligible).
    ``artifacts`` maps an archived file's name to ``(exists, hash_ok)``."""
    reasons: list[str] = []
    if manifest is None:
        reasons.append("no_manifest")
        missing = manifest_rules.legacy_view(None)["missing"]
    else:
        missing = manifest_rules.completeness(manifest)[1]
    reasons.extend(f"manifest_incomplete:{path}" for path in missing)
    if run_status != "completed":
        reasons.append("run_not_completed")
    if conformance != "conformant":
        reasons.append("run_not_conformant")
    # ponytail: one analyze step per run; multi-step chains when a pilot
    # needs one (a nous.rerun-rule/2 and a manifest per step).
    if analyze_steps != 1:
        reasons.append("unsupported_workflow_shape")
    for name, (exists, hash_ok) in sorted(artifacts.items()):
        if not exists:
            reasons.append(f"artifact_missing:{name}")
        elif not hash_ok:
            reasons.append(f"artifact_corrupt:{name}")
    return reasons


def _resolve(document: Any, pointer: str) -> Any:
    """RFC 6901; ``_MISSING`` when the path does not exist."""
    value = document
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _blank(document: Any, pointer: str) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    parent = _resolve(document, "/".join([""] + [p for p in pointer.split("/")[1:-1]]))
    if not parts or parent is _MISSING:
        return
    last = parts[-1]
    if isinstance(parent, dict) and last in parent:
        parent[last] = None
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        parent[int(last)] = None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _MISSING


def _numeric(
    entry: Mapping[str, Any], expected: bytes, actual: bytes
) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
    """(numeric rows, passed, reason) for one ``json_numeric`` output."""
    want, got = _json(expected), _json(actual)
    if want is _MISSING or got is _MISSING:
        return None, False, "not_json"
    rows: list[dict[str, Any]] = []
    reason = None
    for pointer in entry["pointers"]:
        a, b = _resolve(got, pointer), _resolve(want, pointer)
        if a is _MISSING or b is _MISSING:
            reason = "pointer_missing"
            rows.append(
                {
                    "pointer": pointer,
                    "expected": None if b is _MISSING else b,
                    "actual": None if a is _MISSING else a,
                    "abs_diff": None,
                    "within": False,
                }
            )
            continue
        if _number(a) and _number(b):
            diff = abs(float(a) - float(b))
            within = diff <= entry["abs"] + entry["rel"] * abs(float(b))
        else:
            diff, within = None, a == b
        rows.append(
            {
                "pointer": pointer,
                "expected": b,
                "actual": a,
                "abs_diff": diff,
                "within": within,
            }
        )
    rest_want, rest_got = copy.deepcopy(want), copy.deepcopy(got)
    for pointer in entry["pointers"]:
        _blank(rest_want, pointer)
        _blank(rest_got, pointer)
    if reason is None and rest_want != rest_got:
        reason = "non_numeric_mismatch"
    return rows, reason is None and all(r["within"] for r in rows), reason


def compare(
    rule: Mapping[str, Any],
    manifest: Mapping[str, Any],
    actual: Mapping[str, bytes],
    expected: Mapping[str, bytes],
) -> tuple[list[dict[str, Any]], bool]:
    """One row per manifest output (in manifest order) and ``reproduced``.
    ``expected`` holds the archived output bytes (needed by
    ``json_numeric``); digests come from the manifest."""
    entries = {e["name"]: e for e in rule["outputs"]}
    rows: list[dict[str, Any]] = []
    reproduced = True
    for output in manifest.get("outputs") or []:
        name = str(output["name"])
        entry = entries[name]
        data = actual.get(name)
        row: dict[str, Any] = {
            "name": name,
            "mode": entry["mode"],
            "expected_sha256": output.get("sha256"),
            "actual_sha256": None if data is None else manifest_rules.sha256_hex(data),
            "equal": False,
            "numeric": None,
            "reason": None,
        }
        if data is None:
            row["reason"] = "missing_output"
            reproduced = False
            rows.append(row)
            continue
        row["equal"] = row["actual_sha256"] == row["expected_sha256"]
        passed = row["equal"]
        if entry["mode"] == "json_numeric":
            numeric, within, reason = _numeric(entry, expected.get(name, b""), data)
            row["numeric"], row["reason"] = numeric, None if row["equal"] else reason
            passed = row["equal"] or within
        reproduced = reproduced and passed
        rows.append(row)
    return rows, reproduced
