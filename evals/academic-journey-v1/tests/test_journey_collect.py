"""GOO-308 journey collector: evidence refusal, bundle audit and measurements.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-308 section):
skipping ``verify_bundle``'s SHA256SUMS recomputation makes ``-k tampered``
accept a tampered bundle; skipping the required-evidence refusal makes
``-k refuses`` write a report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

TASK_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "academic_journey_collect", TASK_DIR / "collect.py"
)
assert SPEC and SPEC.loader
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)

PROTOCOL = json.loads((TASK_DIR / "protocol.json").read_text(encoding="utf-8"))
GOLD = json.loads(
    (TASK_DIR / "corpora" / "known-answer.gold.json").read_text(encoding="utf-8")
)
DRAFT = "d0000000-0000-4000-8000-000000000001"
CONTENT = "## Results\n" + " ".join(GOLD["claim_support"]) + "\n"
USERS = {name: f"user-{name}" for name in PROTOCOL["principals"]}
T0 = "2026-09-30T10:00:00+00:00"
T1 = "2026-09-30T11:00:00+00:00"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return _sha(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )


def _package(schema: str, body: Any) -> bytes:
    return json.dumps(
        {"schema": schema, "body_sha256": _canonical(body), "body": body}, indent=2
    ).encode()


def _bundle(stances: dict[str, str] | None = None) -> bytes:
    """A bundle shaped like ``audit_bundle.write_zip`` output."""
    support = {**GOLD["claim_support"], **(stances or {})}
    versions, assessments = [], []
    for index, (text, stance) in enumerate(support.items()):
        start = CONTENT.index(text)
        vid = f"cv-{index}"
        versions.append(
            {
                "id": vid,
                "draft_id": DRAFT,
                "start_char": start,
                "end_char": start + len(text),
                "text": text,
                "supersedes_claim_version_id": None,
            }
        )
        first = {
            "id": f"a-{index}",
            "claim_version_id": vid,
            "stance": stance,
            "assessed_by_id": USERS["adjudicator"],
            "actor_role": "adjudicator",
            "created_at": T0,
            "supersedes_assessment_id": None,
        }
        if stance == "opposing":  # first unresolved, then opposing an hour later
            first["stance"] = "unresolved"
            assessments.append(first)
            first = {
                **first,
                "id": f"a-{index}b",
                "stance": "opposing",
                "created_at": T1,
                "supersedes_assessment_id": f"a-{index}",
            }
        assessments.append(first)
    fields = [
        {"field_id": f"f-{a['field']}", "name": a["field"]}
        for a in GOLD["accepted_values"]
    ]
    tips = [
        {
            "id": f"av-{a['field']}",
            "field_id": f"f-{a['field']}",
            "value": a["value"],
            "observation_ids": [f"o-{a['field']}"],
            "accepted_by_id": USERS["adjudicator"],
        }
        for a in GOLD["accepted_values"]
    ]
    observations = [
        {"id": f"o-{a['field']}", "kind": "machine", "value": a["value"]}
        for a in GOLD["accepted_values"]
    ]
    plan: dict[str, Any] = {"steps": [], "parameters": {}}
    counts = {**GOLD["prisma_counts"]}
    source = CONTENT.encode()
    parts = {
        "corpus.json": _package(
            "corpus",
            {
                "identities": {
                    "decisions": [
                        {
                            "event_type": "identity.report_merged",
                            "actor_user_id": USERS["adjudicator"],
                            "actor_role": "adjudicator",
                        }
                    ]
                }
            },
        ),
        "prisma-flow.json": _package("prisma", {"counts": counts}),
        "claims.json": _package(
            "claims",
            {
                "drafts": [
                    {
                        "id": DRAFT,
                        "version": 1,
                        "content": CONTENT,
                        "content_hash": _sha(source),
                    }
                ],
                "claim_versions": versions,
                "assessments": assessments,
                "links": [],
                "stance_observations": [],
            },
        ),
        "methods.json": _package(
            "methods",
            {
                "protocol_versions": [
                    {"id": "pv-1", "content_hash": "c" * 64, "execution_plan": plan}
                ],
                "runs": [
                    {
                        "protocol_version_id": "pv-1",
                        "conformance_status": "plan_verified",
                        "effective_plan_hash": _canonical(
                            {"protocol_content_hash": "c" * 64, "execution_plan": plan}
                        ),
                    }
                ],
            },
        ),
        "extraction.json": _package(
            "extraction",
            [
                {
                    "form_versions": [{"fields": fields}],
                    "accepted_tips": tips,
                    "observations": observations,
                }
            ],
        ),
        "drafts/release-checks.json": _package(
            "checks",
            [
                {
                    "draft_id": DRAFT,
                    "draft_version": 1,
                    "content_hash": _sha(source),
                    "release_status": "verified",
                }
            ],
        ),
        f"drafts/{DRAFT}-v1.md": b"> Status: VERIFIED\n\n" + source,
        f"drafts/{DRAFT}-v1.source.md": source,
    }
    entries = []
    for path, data in sorted(parts.items()):
        body = json.loads(data)["body_sha256"] if path.endswith(".json") else _sha(data)
        entries.append(
            {
                "path": path,
                "schema": None,
                "sha256": _sha(data),
                "bytes": len(data),
                "body_sha256": body,
                "status": "ok",
            }
        )
    members = {
        **parts,
        "manifest.json": json.dumps(
            {"schema": "nous.academic.audit-bundle.v1", "parts": entries}
        ).encode(),
    }
    members["SHA256SUMS"] = "".join(
        f"{_sha(d)}  {p}\n" for p, d in sorted(members.items())
    ).encode()
    return _zip(members)


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in sorted(members.items()):
            archive.writestr(path, data)
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {n: archive.read(n) for n in archive.namelist()}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _trial(
    root: Path, project: str, *, review: str = "passed", bundle: bytes | None = None
) -> Path:
    trial_dir = root / project
    trial_dir.mkdir(parents=True)
    data = bundle or _bundle()
    (trial_dir / "audit-bundle.zip").write_bytes(data)
    (trial_dir / "trace.zip").write_bytes(b"trace")
    manifest_sha = _sha(_members(data)["manifest.json"])
    source_sha = COLLECT.BASE._source_sha()
    attestation = {
        "run_id": f"run-{project}",
        "seed": 1,
        "source_sha": source_sha,
        "git_tree": COLLECT.BASE._git_tree_sha(source_sha),
        "tracked_diff_sha256": f"sha256:{_sha(b'')}",
        "runner_image": "sha256:" + "a" * 64,
    }
    _write(trial_dir / "attestation.json", attestation)
    lines = [
        {
            "step": "claim",
            "principal": "author",
            "method": "POST",
            "path": "/claims",
            "status": 201,
            "ids": {"created": [DRAFT]},
            "versions": {"next_seq": {"claims": 2}},
            "hashes": {f"draft:{DRAFT}": _sha(CONTENT.encode())},
        },
        {
            "step": "stage:write",
            "principal": "author",
            "method": "GET",
            "path": f"/research-engine/projects/{project}/journey",
            "status": 200,
            "ids": {"referenced": [DRAFT]},
            "versions": {
                "next_seq": {"claims": 3},
                "stages": [f"{s}:complete" for s in PROTOCOL["stages"]],
            },
            "hashes": {
                f"draft:{DRAFT}": _sha(CONTENT.encode()),
                "response_sha256": "x",
            },
        },
    ]
    (trial_dir / "transitions.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    _write(
        trial_dir / "roles.json",
        [
            {"user_id": USERS[name], "role": role}
            for name, roles in PROTOCOL["principal_roles"].items()
            for role in roles
        ],
    )
    _write(
        trial_dir / "independent-review.json",
        {
            "manifest_sha256": manifest_sha,
            "reviewer_user_id": "user-independent",
            "claim_correctness": review,
        },
    )
    _write(
        trial_dir / "consent.json",
        {"consent_record_id": "c-1", "scope_digest": "sha256:" + "b" * 64},
    )
    scenarios: dict[str, dict[str, Any]] = {
        s["id"]: {"status": "not_run", "reason": "no safe induction on shared dev"}
        for s in PROTOCOL["scenarios"]
    }
    scenarios["S1"] = scenarios["S7"] = {
        "status": "observed",
        "evidence": ["transitions.jsonl"],
    }
    _write(
        trial_dir / "trial.json",
        {
            "principals": USERS,
            "scenarios": scenarios,
            "infrastructure": "passed",
            "runtime": {
                "run_id": f"run-{project}",
                "seed": 1,
                "runner_image": attestation["runner_image"],
                "source_attestation": {
                    "path": "attestation.json",
                    "sha256": COLLECT.BASE._file_digest(trial_dir / "attestation.json"),
                },
            },
        },
    )
    return trial_dir


@pytest.fixture
def trials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(COLLECT.BASE, "_verify_source_checkout", lambda sha: None)
    root = tmp_path / "trials"
    _trial(root, "known-answer")
    _trial(root, "real-1", review="judge_unavailable")
    _write(root / "real-2" / "trial.json", {"not_run_reason": "no signed consent yet"})
    return root


def _run(trials: Path, tmp_path: Path) -> dict[str, Any]:
    output = tmp_path / "out" / "result.json"
    report = COLLECT.collect(
        TASK_DIR / "protocol.json", trials, output, COLLECT.BASE._source_sha()
    )
    assert output.is_file()
    return dict(report)


def _trial_report(report: dict[str, Any], project: str) -> dict[str, Any]:
    return next(t for t in report["trials"] if t["project"] == project)


def test_protocol_declares_three_projects_five_principals_and_measurements() -> None:
    assert PROTOCOL["baseline_status"] == "not_established"
    assert [(p["id"], p["kind"]) for p in PROTOCOL["projects"]] == [
        ("real-1", "consenting_real"),
        ("real-2", "consenting_real"),
        ("known-answer", "held_out_known_answer"),
    ]
    decision = {k for k, v in PROTOCOL["principal_roles"].items() if v}
    assert decision | {"author"} == {
        "author",
        "supervisor",
        "reviewer_a",
        "reviewer_b",
        "adjudicator",
    }
    assert {"foreign", "viewer"} <= set(PROTOCOL["principals"])
    assert set(PROTOCOL["measurements"]) == {
        "participant_count",
        "completion",
        "correction_burden",
        "disputed_claim_resolution_time",
        "export_completeness",
    }
    assert [s["id"] for s in PROTOCOL["scenarios"]] == [f"S{i}" for i in range(1, 9)]
    corpus = json.loads((TASK_DIR / "corpora" / "known-answer.json").read_text())
    assert len(corpus["documents"]) == 10 and len(corpus["manuscript_claims"]) == 6
    assert "claim_support" not in json.dumps(corpus)  # gold stays with the collector


@pytest.mark.parametrize("missing", ["audit-bundle.zip", "trace.zip"])
def test_refuses_trial_without_bundle_or_trace(
    trials: Path, tmp_path: Path, missing: str
) -> None:
    (trials / "known-answer" / missing).unlink()
    output = tmp_path / "out" / "result.json"
    with pytest.raises(COLLECT.EvidenceError, match=f"required evidence {missing}"):
        COLLECT.collect(
            TASK_DIR / "protocol.json", trials, output, COLLECT.BASE._source_sha()
        )
    assert not output.exists()


def test_tampered_bundle_member_rejected(trials: Path, tmp_path: Path) -> None:
    members = _members((trials / "known-answer" / "audit-bundle.zip").read_bytes())
    members["claims.json"] = members["claims.json"].replace(
        b"supporting", b"supportinG", 1
    )
    (trials / "known-answer" / "audit-bundle.zip").write_bytes(_zip(members))
    with pytest.raises(
        COLLECT.EvidenceError, match="claims.json does not match SHA256SUMS"
    ):
        _run(trials, tmp_path)


def test_transition_hash_drift_rejected(trials: Path, tmp_path: Path) -> None:
    path = trials / "known-answer" / "transitions.jsonl"
    drift = {
        "step": "later",
        "principal": "author",
        "method": "GET",
        "path": "/x",
        "status": 200,
        "hashes": {f"draft:{DRAFT}": "0" * 64},
    }
    path.write_text(path.read_text() + json.dumps(drift) + "\n")
    with pytest.raises(COLLECT.EvidenceError, match="drifted"):
        _run(trials, tmp_path)


def test_secret_shaped_value_rejected(trials: Path, tmp_path: Path) -> None:
    path = trials / "known-answer" / "transitions.jsonl"
    leak = {
        "step": "leak",
        "principal": "author",
        "method": "GET",
        "path": "/x",
        "status": 200,
        "note": "Authorization: Bearer abc.def",
    }
    path.write_text(path.read_text() + json.dumps(leak) + "\n")
    with pytest.raises(COLLECT.EvidenceError, match="secret-bearing value"):
        _run(trials, tmp_path)


def test_completion_reports_k_of_15_and_per_stage(trials: Path, tmp_path: Path) -> None:
    report = _run(trials, tmp_path)
    assert report["completion"]["k"] == 10 and report["completion"]["denominator"] == 15
    assert report["completion"]["per_stage"]["write"] == {"k": 2, "denominator": 3}
    assert report["not_run"] == ["real-2"]


def test_correction_burden_and_dispute_time_from_bundle_only(
    trials: Path, tmp_path: Path
) -> None:
    known = _trial_report(_run(trials, tmp_path), "known-answer")
    burden = known["correction_burden"]
    assert burden["identity_merges_and_splits"] == 1
    assert burden["superseding_assessments"] == 1
    assert burden["overridden_accepted_values"] == 0
    assert burden["adjudicated_screening_resolutions"] is None
    assert burden["per_included_report"] == round(2 / 4, 3)
    # C4 unresolved (0 s, never superseded) and C5 unresolved -> opposing (1 h).
    assert sorted(known["dispute_seconds"]) == [0.0, 3600.0]
    assert known["participants"]["by_principal"] == {"adjudicator": 1}


def test_infrastructure_and_judge_unavailable_unscored(
    trials: Path, tmp_path: Path
) -> None:
    report = _run(trials, tmp_path)
    assert (
        _trial_report(report, "real-1")["verdicts"]["claim_correctness"]
        == "judge_unavailable"
    )
    assert report["unscored"] == 1
    assert report["sample_size"] == 3  # kept in the sample-size table


def test_known_answer_reconstruction_against_gold(trials: Path, tmp_path: Path) -> None:
    known = _trial_report(_run(trials, tmp_path), "known-answer")
    assert known["export_completeness"]["reconstruction"] == {
        "prisma_counts": True,
        "exclusion_reasons": True,
        "accepted_values": True,
        "claim_support": True,
    }
    assert known["export_completeness"]["parts_ok"] == 7
    assert known["verdicts"]["claim_correctness"] == "passed"

    wrong = tmp_path / "wrong"
    _trial(
        wrong,
        "known-answer",
        bundle=_bundle(
            {"Tai chi reduced depressive symptoms by 40 percent.": "supporting"}
        ),
    )
    _trial(wrong, "real-1")
    _write(wrong / "real-2" / "trial.json", {"not_run_reason": "no signed consent yet"})
    seeded = _trial_report(_run(wrong, tmp_path), "known-answer")
    assert seeded["export_completeness"]["reconstruction"]["claim_support"] is False
    assert seeded["verdicts"]["claim_correctness"] == "failed"


def test_not_run_scenario_requires_reason(trials: Path, tmp_path: Path) -> None:
    path = trials / "known-answer" / "trial.json"
    trial = json.loads(path.read_text())
    trial["scenarios"]["S3"] = {"status": "not_run"}
    _write(path, trial)
    with pytest.raises(COLLECT.EvidenceError, match="S3 not_run needs a reason"):
        _run(trials, tmp_path)
