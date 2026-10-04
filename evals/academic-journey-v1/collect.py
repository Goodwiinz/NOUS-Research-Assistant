"""Collect retained GOO-308 plan-to-write journey trials into one report.

Every measurement is computed from each trial's audit bundle and
transitions alone. The bundle checksum half is re-implemented here with the
standard library; this file never imports ``backend/src``, so the check is
an audit rather than the product verifying itself. GOO-293's evidence
helpers are reused through ``importlib``. Any missing or tampered evidence
refuses the whole collection: no output file, non-zero exit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import statistics
import subprocess
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "academic_writing_collect",
    TASK_DIR.parent / "academic-writing-baseline-v1" / "collect.py",
)
assert _SPEC and _SPEC.loader
BASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(BASE)
EvidenceError = BASE.EvidenceError

VERDICTS = {
    "reporting_completeness": {"passed", "failed", "not_run"},
    "protocol_adherence": {"passed", "failed", "not_run"},
    "claim_correctness": {"passed", "failed", "not_run", "judge_unavailable"},
    "infrastructure": {"passed", "failed"},
}
PARTS = (
    "corpus.json",
    "prisma-flow.json",
    "claims.json",
    "methods.json",
    "extraction.json",
    "drafts/release-checks.json",
    "drafts",
)
MANIFEST, SUMS = "manifest.json", "SHA256SUMS"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha(value: Any) -> str:
    return _sha(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- bundle ------------------------------------------------------------------


def verify_bundle(data: bytes) -> dict[str, bytes]:
    """Recompute SHA256SUMS, the manifest and every body hash (stdlib only)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
    except zipfile.BadZipFile as exc:
        raise EvidenceError("audit bundle is not a readable zip") from exc
    sums = {}
    for line in members.get(SUMS, b"").decode().splitlines():
        digest, sep, path = line.partition("  ")
        if not sep:
            raise EvidenceError("malformed SHA256SUMS line")
        sums[path] = digest
    if set(sums) != set(members) - {SUMS}:
        raise EvidenceError("SHA256SUMS does not list exactly the bundle members")
    for path, digest in sums.items():
        if _sha(members[path]) != digest:
            raise EvidenceError(f"bundle member {path} does not match SHA256SUMS")
    manifest = json.loads(members[MANIFEST])
    listed = {part["path"]: part for part in manifest["parts"]}
    if set(listed) != set(members) - {SUMS, MANIFEST}:
        raise EvidenceError("manifest does not list exactly the bundle parts")
    for path, entry in listed.items():
        member = members[path]
        if entry["sha256"] != _sha(member) or entry["bytes"] != len(member):
            raise EvidenceError(f"bundle member {path} does not match the manifest")
        if path.endswith(".json"):
            package = json.loads(member)
            body_sha = _canonical_sha(package.get("body"))
            if body_sha != package.get("body_sha256"):
                raise EvidenceError(f"{path} body does not match its body_sha256")
        else:
            body_sha = _sha(member)
        if entry["body_sha256"] != body_sha:
            raise EvidenceError(f"{path} body_sha256 does not match the manifest")
    checks = json.loads(members["drafts/release-checks.json"])["body"]
    released = {
        f"drafts/{c['draft_id']}-v{c['draft_version']}.source.md": c for c in checks
    }
    for path, check in released.items():
        if path not in members or _sha(members[path]) != check["content_hash"]:
            raise EvidenceError(f"{path} does not hash to its release content_hash")
    return members


def _body(members: dict[str, bytes], path: str) -> Any:
    return json.loads(members[path])["body"]


def _part_status(members: dict[str, bytes]) -> dict[str, str]:
    manifest = json.loads(members[MANIFEST])
    status = {p["path"]: p["status"] for p in manifest["parts"]}
    drafts = [s for p, s in status.items() if p.endswith(".md")]
    status["drafts"] = "ok" if drafts and all(s == "ok" for s in drafts) else "empty"
    return {part: status.get(part, "missing") for part in PARTS}


# --- transitions -------------------------------------------------------------


def verify_transitions(path: Path) -> list[dict[str, Any]]:
    """Ids exist before use; next_seq never decreases; a named hash never drifts."""
    lines = []
    created: set[str] = set()
    heads: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        entry = json.loads(raw)
        BASE._reject_sensitive_configuration(entry, f"transitions[{number}]")
        ids = entry.get("ids") or {}
        for ref in ids.get("referenced", []):
            if ref not in created:
                raise EvidenceError(
                    f"transition {number} references {ref} before it exists"
                )
        created.update(ids.get("created", []))
        for stream, seq in (
            (entry.get("versions") or {}).get("next_seq") or {}
        ).items():
            if seq < heads.get(stream, seq):
                raise EvidenceError(f"transition {number}: {stream} next_seq decreased")
            heads[stream] = seq
        for name, digest in (entry.get("hashes") or {}).items():
            if name.endswith("response_sha256") or name in ("zip_sha256",):
                continue  # per-read hashes, not object versions
            if hashes.setdefault(name, digest) != digest:
                raise EvidenceError(f"transition {number}: hash of {name} drifted")
        entry["_created"] = sorted(created)
        lines.append(entry)
    return lines


def final_journey(lines: list[dict[str, Any]]) -> dict[str, str]:
    for entry in reversed(lines):
        if entry.get("path", "").endswith("/journey") and entry.get("status") == 200:
            stages = (entry.get("versions") or {}).get("stages") or []
            return dict(item.split(":", 1) for item in stages)
    return {}


# --- measurements --------------------------------------------------------------


def participants(
    members: dict[str, bytes], principals: dict[str, str], roles: list[dict[str, Any]]
) -> dict[str, Any]:
    rows: list[tuple[str, str]] = []  # (actor id, actor role)
    for decision in _body(members, "corpus.json")["identities"]["decisions"]:
        rows.append((decision["actor_user_id"], decision["actor_role"]))
    for assessment in _body(members, "claims.json")["assessments"]:
        rows.append((assessment["assessed_by_id"], assessment["actor_role"]))
    role_of = {r["user_id"]: r["role"] for r in roles}
    for matrix in _body(members, "extraction.json"):
        for tip in matrix["accepted_tips"]:
            actor = tip["accepted_by_id"]
            rows.append((actor, role_of.get(actor, "unassigned")))
    humans = {actor for actor, role in rows if role != "machine"}
    by_id = {user: name for name, user in principals.items()}
    return {
        "distinct_humans": len(humans),
        "by_principal": dict(Counter(by_id.get(a, "undeclared") for a in humans)),
        "undeclared_actors": len([a for a in humans if a not in by_id]),
        "declared_principals": len(principals),
    }


def correction_burden(members: dict[str, bytes]) -> dict[str, Any]:
    decisions = _body(members, "corpus.json")["identities"]["decisions"]
    claims = _body(members, "claims.json")
    identity = sum(
        d["event_type"] in ("identity.report_merged", "identity.report_split")
        for d in decisions
    )
    overridden = 0
    for matrix in _body(members, "extraction.json"):
        machine = {
            o["id"]: o.get("value")
            for o in matrix["observations"]
            if o["kind"] == "machine"
        }
        for tip in matrix["accepted_tips"]:
            cited = [machine[i] for i in tip["observation_ids"] if i in machine]
            overridden += tip["value"] not in cited
    versions = sum(
        bool(v["supersedes_claim_version_id"]) for v in claims["claim_versions"]
    )
    assessments = sum(
        bool(a["supersedes_assessment_id"]) for a in claims["assessments"]
    )
    total = identity + overridden + versions + assessments
    included = _body(members, "prisma-flow.json")["counts"]["included_reports"]
    return {
        "identity_merges_and_splits": identity,
        "adjudicated_screening_resolutions": None,  # not in bundle v1
        "overridden_accepted_values": overridden,
        "superseding_claim_versions": versions,
        "superseding_assessments": assessments,
        "total": total,
        "included_reports": included,
        "per_included_report": round(total / included, 3) if included else None,
    }


def dispute_times(members: dict[str, bytes]) -> list[float]:
    claims = _body(members, "claims.json")
    link_version = {link["id"]: link["claim_version_id"] for link in claims["links"]}
    durations = []
    for version in claims["claim_versions"]:
        rows = sorted(
            (
                a
                for a in claims["assessments"]
                if a["claim_version_id"] == version["id"]
            ),
            key=lambda a: a["created_at"],
        )
        closed = {a["supersedes_assessment_id"] for a in rows}
        tips = [a for a in rows if a["id"] not in closed]
        if not tips:
            continue
        tip = tips[-1]
        starts = [rows[0]["created_at"]] if rows[0]["stance"] != "supporting" else []
        starts += [
            o["created_at"]
            for o in claims["stance_observations"]
            if link_version.get(o["link_id"]) == version["id"]
            and o["stance"] != tip["stance"]
        ]
        if starts:
            durations.append(
                (_time(tip["created_at"]) - _time(min(starts))).total_seconds()
            )
    return durations


def reconstruction(
    members: dict[str, bytes], kind: str, gold: dict[str, Any] | None
) -> dict[str, bool]:
    flow = _body(members, "prisma-flow.json")["counts"]
    claims = _body(members, "claims.json")
    tips: dict[str, str] = {}
    closed = {a["supersedes_assessment_id"] for a in claims["assessments"]}
    for a in claims["assessments"]:
        if a["id"] not in closed:
            tips[a["claim_version_id"]] = a["stance"]
    if kind == "held_out_known_answer":
        assert gold is not None
        fields = {}
        accepted = []
        for matrix in _body(members, "extraction.json"):
            for version in matrix["form_versions"]:
                fields.update({f["field_id"]: f["name"] for f in version["fields"]})
            accepted += [
                {"field": fields.get(t["field_id"]), "value": str(t["value"])}
                for t in matrix["accepted_tips"]
            ]
        support = {v["text"]: tips.get(v["id"]) for v in claims["claim_versions"]}
        return {
            "prisma_counts": {k: flow.get(k) for k in gold["prisma_counts"]}
            == gold["prisma_counts"],
            "exclusion_reasons": flow["reports_excluded_by_reason"]
            == dict(Counter(gold["exclusion_reasons"].values())),
            "accepted_values": sorted(
                accepted, key=lambda a: (a["field"] or "", a["value"])
            )
            == sorted(gold["accepted_values"], key=lambda a: (a["field"], a["value"])),
            "claim_support": support == gold["claim_support"],
        }
    drafts = {d["id"]: d["content"] for d in claims["drafts"]}
    methods = _body(members, "methods.json")
    plans = {v["id"]: v for v in methods["protocol_versions"]}
    return {
        "exclusion_reasons": "" not in flow["reports_excluded_by_reason"]
        and None not in flow["reports_excluded_by_reason"],
        "links_slice": all(
            drafts[v["draft_id"]][v["start_char"] : v["end_char"]] == v["text"]
            for v in claims["claim_versions"]
            if v["draft_id"] in drafts
        ),
        "run_hashes": all(
            run["protocol_version_id"] in plans
            and run["effective_plan_hash"]
            == _canonical_sha(
                {
                    "protocol_content_hash": plans[run["protocol_version_id"]][
                        "content_hash"
                    ],
                    "execution_plan": plans[run["protocol_version_id"]][
                        "execution_plan"
                    ],
                }
            )
            for run in methods["runs"]
            if run["protocol_version_id"]
        ),
    }


# --- trials --------------------------------------------------------------------


def _check_scenarios(
    trial_dir: Path, protocol: dict[str, Any], trial: dict[str, Any]
) -> dict[str, str]:
    out = {}
    declared = trial.get("scenarios") or {}
    for scenario in protocol["scenarios"]:
        entry = declared.get(scenario["id"]) or {}
        if entry.get("status") == "observed":
            files = entry.get("evidence") or []
            if not files or not all((trial_dir / f).is_file() for f in files):
                raise EvidenceError(
                    f"{trial_dir.name}: {scenario['id']} evidence is missing"
                )
        elif entry.get("status") == "not_run":
            if not str(entry.get("reason") or "").strip():
                raise EvidenceError(
                    f"{trial_dir.name}: {scenario['id']} not_run needs a reason"
                )
        elif scenario["required"]:
            raise EvidenceError(
                f"{trial_dir.name}: required scenario {scenario['id']} has neither evidence nor a not_run reason"
            )
        out[scenario["id"]] = entry.get("status", "not_run")
    return out


def _check_principals(
    trial_dir: Path,
    protocol: dict[str, Any],
    trial: dict[str, Any],
    roles: list[dict[str, Any]],
) -> dict[str, str]:
    principals = trial.get("principals") or {}
    if set(principals) != set(protocol["principals"]):
        raise EvidenceError(
            f"{trial_dir.name}: principals must be exactly {protocol['principals']}"
        )
    if len(set(principals.values())) != len(principals):
        raise EvidenceError(f"{trial_dir.name}: principals must be distinct users")
    held: dict[str, set[str]] = {}
    for row in roles:
        held.setdefault(row["user_id"], set()).add(row["role"])
    for name, required in protocol["principal_roles"].items():
        if held.get(principals[name], set()) != set(required):
            raise EvidenceError(
                f"{trial_dir.name}: {name} roles do not match roles.json"
            )
    return principals


def collect_trial(
    trial_dir: Path, protocol: dict[str, Any], source_sha: str
) -> dict[str, Any]:
    project = {p["id"]: p for p in protocol["projects"]}[trial_dir.name]
    trial_path = trial_dir / "trial.json"
    if not trial_path.is_file():
        raise EvidenceError(f"{trial_dir.name}: trial.json is missing")
    trial = BASE._load_json(trial_path)
    BASE._reject_sensitive_configuration(trial, "trial")
    reason = str(trial.get("not_run_reason") or "").strip()
    if reason:
        # e.g. no signed consent yet: every dimension not_run, never simulated.
        return {
            "project": project["id"],
            "kind": project["kind"],
            "status": "not_run",
            "reason": reason,
            "stages": {},
            "verdicts": {name: "not_run" for name in VERDICTS},
        }
    required = list(protocol["required_evidence"]["all"])
    required += protocol["required_evidence"].get(project["kind"], [])
    for name in required:
        if not (trial_dir / name).is_file():
            raise EvidenceError(
                f"{trial_dir.name}: required evidence {name} is missing"
            )
    retained = {}
    for name in ("roles.json", "independent-review.json", "consent.json"):
        if (trial_dir / name).is_file():
            retained[name] = BASE._load_json(trial_dir / name)
            BASE._reject_sensitive_configuration(retained[name], name)
    BASE._verify_source_attestation(trial_path, trial.get("runtime") or {}, source_sha)
    roles = retained["roles.json"]
    principals = _check_principals(trial_dir, protocol, trial, roles)
    scenarios = _check_scenarios(trial_dir, protocol, trial)
    lines = verify_transitions(trial_dir / "transitions.jsonl")
    members = verify_bundle((trial_dir / "audit-bundle.zip").read_bytes())
    manifest_sha = _sha(members[MANIFEST])
    review = retained["independent-review.json"]
    if review.get("manifest_sha256") != manifest_sha:
        raise EvidenceError(
            f"{trial_dir.name}: independent review is not bound to this bundle"
        )
    if review.get("reviewer_user_id") in principals.values():
        raise EvidenceError(
            f"{trial_dir.name}: the independent reviewer must not be a principal"
        )
    blob = b"".join(members.values())
    for created in (lines[-1]["_created"] if lines else []):
        if created.encode() not in blob:
            raise EvidenceError(
                f"{trial_dir.name}: transition id {created} is not in the bundle"
            )

    status = _part_status(members)
    checks = _body(members, "drafts/release-checks.json")
    journey = final_journey(lines)
    stages = {}
    for stage, part in protocol["stage_evidence"].items():
        verified = status[part] == "ok" and (
            stage != "write" or any(c["release_status"] == "verified" for c in checks)
        )
        stages[stage] = journey.get(stage) == "complete" and verified
    gold = None
    if project["kind"] == "held_out_known_answer":
        gold = BASE._load_json(TASK_DIR / project["gold"])
    rebuilt = reconstruction(members, project["kind"], gold)
    parts_ok = sum(s == "ok" for s in status.values())
    methods = _body(members, "methods.json")
    adherence = (
        "not_run"
        if scenarios.get("S7") != "observed"
        else (
            "passed"
            if rebuilt.get("run_hashes", True)
            and all(
                r["conformance_status"] in ("plan_verified", "conformant")
                for r in methods["runs"]
            )
            else "failed"
        )
    )
    if project["kind"] == "held_out_known_answer":
        correctness = "passed" if rebuilt["claim_support"] else "failed"
    else:
        correctness = review.get("claim_correctness", "not_run")
    verdicts = {
        "reporting_completeness": (
            "passed" if parts_ok == len(PARTS) and all(rebuilt.values()) else "failed"
        ),
        "protocol_adherence": adherence,
        "claim_correctness": correctness,
        "infrastructure": trial.get("infrastructure", "failed"),
    }
    for name, allowed in VERDICTS.items():
        if verdicts[name] not in allowed:
            raise EvidenceError(
                f"{trial_dir.name}: {name} verdict {verdicts[name]!r} is not allowed"
            )
    return {
        "project": project["id"],
        "kind": project["kind"],
        "status": "observed",
        "manifest_sha256": manifest_sha,
        "bundle_sha256": BASE._file_digest(trial_dir / "audit-bundle.zip"),
        "trace_sha256": BASE._file_digest(trial_dir / "trace.zip"),
        "stages": stages,
        "scenarios": scenarios,
        "participants": participants(members, principals, roles),
        "correction_burden": correction_burden(members),
        "dispute_seconds": dispute_times(members),
        "export_completeness": {
            "parts_ok": parts_ok,
            "parts": len(PARTS),
            "part_status": status,
            "reconstruction": rebuilt,
        },
        "verdicts": verdicts,
    }


def collect(
    protocol_path: Path, trials_dir: Path, output_path: Path, source_sha: str
) -> dict[str, Any]:
    BASE._verify_source_checkout(source_sha)
    protocol = BASE._load_json(protocol_path)
    if protocol.get("baseline_status") != "not_established":
        raise EvidenceError(
            "protocol v1 freezes the method; the trials establish the baseline"
        )
    trials = [
        collect_trial(trials_dir / project["id"], protocol, source_sha)
        for project in protocol["projects"]
    ]
    stages = protocol["stages"]
    pairs = {s: sum(bool(t["stages"].get(s)) for t in trials) for s in stages}
    durations = [d for t in trials for d in t.get("dispute_seconds", [])]
    unscored = sum(
        t["verdicts"]["infrastructure"] == "failed"
        or t["verdicts"]["claim_correctness"] == "judge_unavailable"
        for t in trials
    )
    report = {
        "schema_version": "1.0",
        "protocol_id": protocol["id"],
        "baseline_status": protocol["baseline_status"],
        "source_sha": source_sha,
        "protocol_digest": BASE._file_digest(protocol_path),
        "harness_digest": BASE._file_digest(Path(__file__)),
        "corpus_digests": {
            name: BASE._file_digest(TASK_DIR / "corpora" / name)
            for name in ("known-answer.json", "known-answer.gold.json")
        },
        "completion": {
            "k": sum(pairs.values()),
            "denominator": len(protocol["projects"]) * len(stages),
            "per_stage": {
                s: {"k": pairs[s], "denominator": len(protocol["projects"])}
                for s in stages
            },
        },
        "disputed_claim_resolution_seconds": {
            "n": len(durations),
            "median": statistics.median(durations) if durations else None,
            "max": max(durations) if durations else None,
        },
        "dimensions": {
            name: dict(Counter(t["verdicts"][name] for t in trials))
            for name in VERDICTS
        },
        "sample_size": len(trials),
        "unscored": unscored,
        "not_run": [t["project"] for t in trials if t["status"] == "not_run"],
        "trials": trials,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=TASK_DIR / "protocol.json")
    parser.add_argument("--trials-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", default=None)
    args = parser.parse_args()
    try:
        report = collect(
            args.protocol.resolve(),
            args.trials_dir.resolve(),
            args.output.resolve(),
            args.source_sha or BASE._source_sha(),
        )
    except (EvidenceError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {k: report[k] for k in ("completion", "dimensions", "not_run")},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
