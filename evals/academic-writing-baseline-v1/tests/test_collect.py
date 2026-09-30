from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

TASK_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "academic_writing_collect", TASK_DIR / "collect.py"
)
assert SPEC and SPEC.loader
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _record(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": COLLECT._file_digest(path)}


def _runtime(
    state: dict[str, Any], task_id: str, source_sha: str, run_id: str, seed: int = 101
) -> dict[str, Any]:
    cohort = state["cohorts"][task_id]
    return {
        "run_id": run_id,
        "seed": seed,
        "source_sha": source_sha,
        "provider": "test-provider",
        "model": "test-model",
        "runner_image": "academic-runner@sha256:" + "d" * 64,
        "verifier": "writing-verifier-v1",
        "fixture_digest": state["corpus_digests"][cohort],
        "calibration_digest": state["calibration_digest"],
        "harness_digest": COLLECT._harness_digest(),
        "tool_versions": {"harbor": "0.6.6", "python": "3.11.14"},
        "env_flags": {"network": "restricted", "judge_access": "verifier-only"},
        "configuration": {"temperature": 0},
        "authenticated": True,
        "real_provider": True,
        "source_attestation": {
            "path": "source-attestation.json",
            "sha256": "sha256:" + "0" * 64,
        },
    }


def _success_bundle(
    tmp_path: Path,
    *,
    bundle_name: str = "trial-success",
    run_id: str = "run-1",
) -> tuple[Path, dict[str, Any]]:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle = tmp_path / bundle_name
    bundle.mkdir(parents=True)
    draft = bundle / "draft.md"
    draft.write_text("## Result\n\nSupported claim [Doc 2].", encoding="utf-8")
    digest = COLLECT._file_digest(draft)
    review = bundle / "review.json"
    _write_json(
        review,
        {
            "id": f"review-{run_id}",
            "run_id": run_id,
            "draft_id": f"draft-{run_id}",
            "project_id": f"project-{run_id}",
            "outcome": "passed",
            "candidate_content": draft.read_text(encoding="utf-8"),
            "candidate_content_hash": digest.removeprefix("sha256:"),
        },
    )
    citations = bundle / "citations.json"
    _write_json(
        citations,
        [
            {
                "id": f"draft-citation-{run_id}",
                "draft_id": f"draft-{run_id}",
                "citation_index": 2,
                "document_id": "document-2",
                "citation_id": "citation-2",
                "metadata_source": "citation",
                "canonical_metadata": {
                    "title": "Canonical source",
                    "authors": ["Ada Lovelace"],
                    "year": 2026,
                    "venue": "Journal of Evidence",
                    "doi": "10.1000/canonical",
                    "arxiv_id": None,
                },
            }
        ],
    )
    bibliography = (
        "@article{doc2,\n"
        "  title = {Canonical source},\n"
        "  author = {Ada Lovelace},\n"
        "  year = {2026},\n"
        "  journal = {Journal of Evidence},\n"
        "  doi = {10.1000/canonical}\n"
        "}\n"
    )
    markdown = bundle / "download.md"
    markdown.write_text(
        draft.read_text(encoding="utf-8")
        + "\n\n## References\n\n```bibtex\n"
        + bibliography.rstrip()
        + "\n```",
        encoding="utf-8",
    )
    latex = bundle / "download.zip"
    with zipfile.ZipFile(latex, "w") as archive:
        archive.writestr(
            "draft.tex",
            COLLECT._expected_latex_document(draft.read_text(encoding="utf-8")),
        )
        archive.writestr("references.bib", bibliography)
    task_digest = "sha256:" + "b" * 64
    fixture_digest = "sha256:" + "e" * 64
    calibration_results = [
        {
            "fixture_id": fixture["id"],
            "input_digest": COLLECT._canonical_digest(fixture["input"]),
            "expected": fixture["expected"],
            "observed": fixture["expected"],
        }
        for fixture in state["calibration"]["fixtures"]
    ]
    calibration = bundle / "calibration.json"
    _write_json(
        calibration,
        {
            "id": f"calibration-{run_id}",
            "run_id": run_id,
            "reviewer": "independent-reviewer",
            "verifier": "writing-verifier-v1",
            "fixture_digest": state["calibration_digest"],
            "outcome": "passed",
            "results": calibration_results,
        },
    )
    adjudication = bundle / "adjudication.json"
    _write_json(
        adjudication,
        {
            "id": f"adjudication-{run_id}",
            "run_id": run_id,
            "draft_id": f"draft-{run_id}",
            "project_id": f"project-{run_id}",
            "reviewer": "independent-reviewer",
            "verifier": "writing-verifier-v1",
            "calibration_id": f"calibration-{run_id}",
            "independent": True,
            "verdict": "passed",
            "task_digest": task_digest,
            "draft_content_sha256": digest,
        },
    )
    authorization = bundle / "authorization.json"
    _write_json(
        authorization,
        {
            "id": f"authorization-{run_id}",
            "run_id": run_id,
            "draft_id": f"draft-{run_id}",
            "checks": {
                "collaborator_download": {
                    "status": 200,
                    "principal_id": f"collaborator-{run_id}",
                    "project_id": f"project-{run_id}",
                    "resource_type": "draft_export",
                    "resource_id": f"draft-{run_id}",
                },
                "foreign_project_download": {
                    "status": 404,
                    "principal_id": f"outsider-{run_id}",
                    "project_id": f"foreign-project-{run_id}",
                    "resource_type": "draft_export",
                    "resource_id": f"draft-{run_id}",
                },
                "foreign_project_access": {
                    "status": 200,
                    "principal_id": f"outsider-{run_id}",
                    "project_id": f"foreign-project-{run_id}",
                    "resource_type": "project",
                    "resource_id": f"foreign-project-{run_id}",
                },
            },
        },
    )
    blocked_review = bundle / "blocked-review.json"
    blocked_candidate = "Unsupported revision without source support."
    _write_json(
        blocked_review,
        {
            "id": f"review-blocked-{run_id}",
            "outcome": "blocked",
            "project_id": f"project-{run_id}",
            "base_draft_id": f"draft-{run_id}",
            "candidate_content": blocked_candidate,
            "candidate_content_hash": hashlib.sha256(
                blocked_candidate.encode("utf-8")
            ).hexdigest(),
        },
    )
    trial = {
        "task_digest": task_digest,
        "runtime": {
            "run_id": run_id,
            "provider": "provider",
            "model": "model",
            "verifier": "writing-verifier-v1",
            "fixture_digest": fixture_digest,
            "calibration_digest": state["calibration_digest"],
        },
        "artifacts": {
            "draft": {
                **_record(draft),
                "id": f"draft-{run_id}",
                "project_id": f"project-{run_id}",
                "version": 3,
                "content_sha256": digest,
            },
            "review": _record(review),
            "adjudication": _record(adjudication),
            "calibration": _record(calibration),
            "authorization": _record(authorization),
            "citations": _record(citations),
            "exports": {
                "markdown": _record(markdown),
                "latex": _record(latex),
            },
        },
        "invalid_revision": {
            "attempted": True,
            "outcome": "blocked",
            "review_id": f"review-blocked-{run_id}",
            "review": _record(blocked_review),
            "before": {
                "draft_id": f"draft-{run_id}",
                "version": 3,
                "content_sha256": digest,
            },
            "after": {
                "draft_id": f"draft-{run_id}",
                "version": 3,
                "content_sha256": digest,
            },
        },
    }
    return bundle / "trial.json", trial


def _bind_trial_to_protocol(
    bundle_path: Path,
    trial: dict[str, Any],
    state: dict[str, Any],
    task_id: str,
    seed: int,
    source_sha: str,
    run_id: str,
) -> None:
    trial.update(
        {
            "task_id": task_id,
            "seed": seed,
            "cohort": state["cohorts"][task_id],
            "task_digest": COLLECT._canonical_digest(state["tasks"][task_id]),
            "runtime": _runtime(state, task_id, source_sha, run_id, seed),
        }
    )
    attestation_path = bundle_path.parent / "source-attestation.json"
    _write_json(
        attestation_path,
        {
            "run_id": run_id,
            "seed": seed,
            "source_sha": source_sha,
            "git_tree": COLLECT._git_tree_sha(source_sha),
            "tracked_diff_sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
            "runner_image": trial["runtime"]["runner_image"],
        },
    )
    trial["runtime"]["source_attestation"] = _record(attestation_path)
    artifacts = trial.get("artifacts") or {}
    adjudication_record = artifacts.get("adjudication")
    if isinstance(adjudication_record, dict):
        adjudication_path = bundle_path.parent / adjudication_record["path"]
        adjudication = COLLECT._load_json(adjudication_path)
        adjudication["task_digest"] = trial["task_digest"]
        _write_json(adjudication_path, adjudication)
        adjudication_record["sha256"] = COLLECT._file_digest(adjudication_path)
    calibration_record = artifacts.get("calibration")
    if isinstance(calibration_record, dict):
        calibration_path = bundle_path.parent / calibration_record["path"]
        calibration = COLLECT._load_json(calibration_path)
        calibration["fixture_digest"] = trial["runtime"]["calibration_digest"]
        _write_json(calibration_path, calibration)
        calibration_record["sha256"] = COLLECT._file_digest(calibration_path)


def test_protocol_freezes_five_canonical_trials_and_separate_corpora() -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)

    assert len(state["expected"]) == 14
    assert (
        sum(
            len(entry["seeds"])
            for entry in protocol["trial_plan"]
            if entry["kind"] == "canonical"
        )
        == 5
    )
    assert set(state["cohorts"].values()) == {"development", "held_out"}
    assert state["calibration_digest"].startswith("sha256:")
    assert protocol["baseline_status"]["status"] == "not_established"
    assert "historical_comparison" not in protocol

    held_out = COLLECT._load_json(TASK_DIR / protocol["corpora"]["held_out"])
    late = next(
        task for task in held_out["tasks"] if task["id"] == "held-late-page-evidence"
    )
    assert late["documents"][0]["content"].index("Page 20 reports") > 32_000
    sparse = next(
        task for task in held_out["tasks"] if task["id"] == "held-sparse-citations"
    )
    assert len(sparse["documents"]) == 11
    assert sparse["documents"][1]["title"] == "Quoted {Result} α"
    assert sparse["documents"][10]["title"] == "Boundary Record β"
    hijacked = next(
        task for task in held_out["tasks"] if task["id"] == "held-hijacked-id"
    )
    assert hijacked["forbidden_values"] == ["10.1000/foreign-project-only"]
    assert hijacked["forbidden_values"][0] in hijacked["instruction"]


def test_corpus_overlap_fingerprint_ignores_task_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = {
        "condition": "copied",
        "instruction": "Identical substantive task.",
        "documents": [{"title": "Same", "content": "Same evidence."}],
    }
    corpora = {
        "development": {"tasks": [{"id": "development-id", **shared}]},
        "held_out": {"tasks": [{"id": "renamed-held-id", **shared}]},
    }

    def fake_corpus(
        _protocol: dict[str, Any], name: str
    ) -> tuple[Path, dict[str, Any]]:
        return tmp_path / f"{name}.json", corpora[name]

    monkeypatch.setattr(COLLECT, "_corpus", fake_corpus)
    with pytest.raises(COLLECT.EvidenceError, match="tasks overlap"):
        COLLECT._task_index({})


def test_protocol_rejects_undeclared_trial_kind() -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    protocol["trial_plan"][1]["kind"] = "boundary_typo"

    with pytest.raises(COLLECT.EvidenceError, match="unsupported trial kinds"):
        COLLECT.validate_protocol(protocol)


def test_success_requires_reconciled_persisted_and_downloaded_artifacts(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    _write_json(bundle_path, trial)

    COLLECT._verify_success_artifacts(bundle_path, trial)

    trial["invalid_revision"]["after"]["version"] = 4
    with pytest.raises(
        COLLECT.EvidenceError, match="replaced the current valid artifact"
    ):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_equal_revision_snapshots_must_match_retained_draft(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    for snapshot in ("before", "after"):
        trial["invalid_revision"][snapshot]["draft_id"] = "fabricated-draft"

    with pytest.raises(COLLECT.EvidenceError, match="bound to the retained draft"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_blocked_review_binds_base_project_and_candidate_content(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["invalid_revision"]["review"]
    path = bundle_path.parent / record["path"]
    review = COLLECT._load_json(path)
    review["base_draft_id"] = "unrelated-draft"
    _write_json(path, review)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="attempted revision"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_markdown_body_and_bibliography_must_match_retained_evidence(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    markdown_record = trial["artifacts"]["exports"]["markdown"]
    markdown_path = bundle_path.parent / markdown_record["path"]
    markdown_path.write_text(
        markdown_path.read_text(encoding="utf-8").replace(
            "Supported claim", "Unrelated stale body", 1
        ),
        encoding="utf-8",
    )
    markdown_record["sha256"] = COLLECT._file_digest(markdown_path)

    with pytest.raises(COLLECT.EvidenceError, match="not the retained draft"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_markdown_references_reject_unparsed_text_outside_bibtex(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    markdown_record = trial["artifacts"]["exports"]["markdown"]
    markdown_path = bundle_path.parent / markdown_record["path"]
    markdown_path.write_text(
        markdown_path.read_text(encoding="utf-8")
        + "\n\nFabricated reference outside the canonical fence.",
        encoding="utf-8",
    )
    markdown_record["sha256"] = COLLECT._file_digest(markdown_path)

    with pytest.raises(COLLECT.EvidenceError, match="parseable BibTeX"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_passing_review_recomputes_candidate_and_project(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["artifacts"]["review"]
    path = bundle_path.parent / record["path"]
    review = COLLECT._load_json(path)
    review["candidate_content"] = "Fabricated reviewed text."
    _write_json(path, review)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="not bound to the saved draft"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_bibtex_metadata_must_match_canonical_citation_rows(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    latex_record = trial["artifacts"]["exports"]["latex"]
    latex_path = bundle_path.parent / latex_record["path"]
    with zipfile.ZipFile(latex_path, "w") as archive:
        archive.writestr(
            "draft.tex",
            COLLECT._expected_latex_document(
                (bundle_path.parent / "draft.md").read_text(encoding="utf-8")
            ),
        )
        archive.writestr("references.bib", "@article{doc2, title={Fabricated title}}")
    latex_record["sha256"] = COLLECT._file_digest(latex_path)

    with pytest.raises(COLLECT.EvidenceError, match="differs from canonical metadata"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_fabricated_identifier_fixture_rejects_the_declared_value(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    fake_id = "10.9999/fabricated-do-not-use"
    trial["task_id"] = "dev-fabricated-id"

    citations_record = trial["artifacts"]["citations"]
    citations_path = bundle_path.parent / citations_record["path"]
    citations = COLLECT._load_json(citations_path)
    citations[0]["canonical_metadata"]["doi"] = fake_id
    _write_json(citations_path, citations)
    citations_record["sha256"] = COLLECT._file_digest(citations_path)

    markdown_record = trial["artifacts"]["exports"]["markdown"]
    markdown_path = bundle_path.parent / markdown_record["path"]
    markdown_path.write_text(
        markdown_path.read_text(encoding="utf-8").replace("10.1000/canonical", fake_id),
        encoding="utf-8",
    )
    markdown_record["sha256"] = COLLECT._file_digest(markdown_path)

    latex_record = trial["artifacts"]["exports"]["latex"]
    latex_path = bundle_path.parent / latex_record["path"]
    with zipfile.ZipFile(latex_path, "w") as archive:
        archive.writestr(
            "draft.tex",
            COLLECT._expected_latex_document(
                (bundle_path.parent / "draft.md").read_text(encoding="utf-8")
            ),
        )
        archive.writestr(
            "references.bib",
            (bundle_path.parent / "download.md")
            .read_text(encoding="utf-8")
            .split("```bibtex\n", 1)[1]
            .rsplit("\n```", 1)[0],
        )
    latex_record["sha256"] = COLLECT._file_digest(latex_path)

    with pytest.raises(COLLECT.EvidenceError, match="forbidden task value"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_latex_body_must_match_retained_draft(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    latex_record = trial["artifacts"]["exports"]["latex"]
    latex_path = bundle_path.parent / latex_record["path"]
    with zipfile.ZipFile(latex_path, "w") as archive:
        archive.writestr(
            "draft.tex",
            COLLECT._expected_latex_document(
                "## Result\n\nUnrelated stale claim [Doc 2]."
            ),
        )
        archive.writestr(
            "references.bib",
            (bundle_path.parent / "download.md")
            .read_text(encoding="utf-8")
            .split("```bibtex\n", 1)[1]
            .rsplit("\n```", 1)[0],
        )
    latex_record["sha256"] = COLLECT._file_digest(latex_path)

    with pytest.raises(COLLECT.EvidenceError, match="not the retained draft"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_duplicate_citation_indices_are_rejected(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["artifacts"]["citations"]
    path = bundle_path.parent / record["path"]
    rows = COLLECT._load_json(path)
    rows.append({**rows[0], "document_id": "document-duplicate"})
    _write_json(path, rows)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="duplicate persisted"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_citation_rows_bind_to_retained_draft(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["artifacts"]["citations"]
    path = bundle_path.parent / record["path"]
    rows = COLLECT._load_json(path)
    rows[0]["draft_id"] = "unrelated-draft"
    _write_json(path, rows)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="not bound to the retained draft"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_bibtex_escape_normalization_preserves_canonical_metadata() -> None:
    assert COLLECT._normalized_text(r"Journal of R\&D") == "Journal of R&D"


def test_infrastructure_failure_remains_independent_of_completed_gates(
    tmp_path: Path,
) -> None:
    verdicts = COLLECT._validate_verdicts(
        tmp_path / "trial.json",
        {
            "passed": False,
            "verdicts": {
                "objective": "passed",
                "semantic": "not_run",
                "authorization": "passed",
                "infrastructure": "failed",
            },
        },
    )
    assert verdicts["objective"] == "passed"
    assert verdicts["infrastructure"] == "failed"

    with pytest.raises(COLLECT.EvidenceError, match="passed flag must equal"):
        COLLECT._validate_verdicts(
            tmp_path / "trial.json",
            {
                "passed": True,
                "verdicts": {
                    "objective": "passed",
                    "semantic": "judge_unavailable",
                    "authorization": "passed",
                    "infrastructure": "passed",
                },
            },
        )


def test_each_passed_dimension_requires_its_own_evidence(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    state = COLLECT.validate_protocol(COLLECT._load_json(TASK_DIR / "protocol.json"))
    del trial["artifacts"]["authorization"]
    verdicts = {
        "objective": "passed",
        "semantic": "judge_unavailable",
        "authorization": "passed",
        "infrastructure": "passed",
    }

    with pytest.raises(COLLECT.EvidenceError, match="artifact record"):
        COLLECT._verify_passed_artifacts(bundle_path, trial, verdicts, state)


def test_authorization_probes_bind_exact_principals_and_projects(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["artifacts"]["authorization"]
    path = bundle_path.parent / record["path"]
    authorization = COLLECT._load_json(path)
    authorization["checks"]["foreign_project_download"]["project_id"] = "project-run-1"
    authorization["checks"]["foreign_project_access"]["project_id"] = "project-run-1"
    authorization["checks"]["foreign_project_access"]["resource_id"] = "project-run-1"
    _write_json(path, authorization)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="foreign-project denial"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_foreign_project_denial_requires_project_existence_proof(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    record = trial["artifacts"]["authorization"]
    path = bundle_path.parent / record["path"]
    authorization = COLLECT._load_json(path)
    del authorization["checks"]["foreign_project_access"]
    _write_json(path, authorization)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="existence is unproven"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_semantic_pass_requires_bound_calibration(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    calibration_record = trial["artifacts"]["calibration"]
    calibration_path = bundle_path.parent / calibration_record["path"]
    calibration = COLLECT._load_json(calibration_path)
    wrong = next(
        result
        for result in calibration["results"]
        if result["fixture_id"] == "known-wrong"
    )
    wrong["observed"] = "accept"
    _write_json(calibration_path, calibration)
    calibration_record["sha256"] = COLLECT._file_digest(calibration_path)

    with pytest.raises(COLLECT.EvidenceError, match="calibration evidence"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_semantic_evidence_rejects_duplicate_results_and_wrong_verifier(
    tmp_path: Path,
) -> None:
    bundle_path, trial = _success_bundle(tmp_path, bundle_name="duplicate-result")
    record = trial["artifacts"]["calibration"]
    path = bundle_path.parent / record["path"]
    calibration = COLLECT._load_json(path)
    calibration["results"].append(dict(calibration["results"][-1]))
    _write_json(path, calibration)
    record["sha256"] = COLLECT._file_digest(path)
    with pytest.raises(COLLECT.EvidenceError, match="calibration evidence"):
        COLLECT._verify_success_artifacts(bundle_path, trial)

    bundle_path, trial = _success_bundle(tmp_path, bundle_name="wrong-verifier")
    record = trial["artifacts"]["adjudication"]
    path = bundle_path.parent / record["path"]
    adjudication = COLLECT._load_json(path)
    adjudication["verifier"] = "other-verifier"
    _write_json(path, adjudication)
    record["sha256"] = COLLECT._file_digest(path)
    calibration_record = trial["artifacts"]["calibration"]
    calibration_path = bundle_path.parent / calibration_record["path"]
    calibration = COLLECT._load_json(calibration_path)
    calibration["verifier"] = "other-verifier"
    _write_json(calibration_path, calibration)
    calibration_record["sha256"] = COLLECT._file_digest(calibration_path)
    with pytest.raises(COLLECT.EvidenceError, match="calibration evidence"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_product_pass_requires_authenticated_real_provider(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle_path, trial = _success_bundle(tmp_path)
    task_id = "dev-canonical-comparison"
    source_sha = COLLECT._source_sha()
    _bind_trial_to_protocol(
        bundle_path, trial, state, task_id, 101, source_sha, "run-1"
    )
    trial["runtime"]["authenticated"] = False
    trial["passed"] = True
    trial["verdicts"] = {
        "objective": "passed",
        "semantic": "passed",
        "authorization": "passed",
        "infrastructure": "passed",
    }

    with pytest.raises(COLLECT.EvidenceError, match="authenticated real-provider"):
        COLLECT.validate_trial(bundle_path, trial, state, source_sha)


def test_runner_source_attestation_binds_commit_tree(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle_path, trial = _success_bundle(tmp_path)
    source_sha = COLLECT._source_sha()
    _bind_trial_to_protocol(
        bundle_path,
        trial,
        state,
        "dev-canonical-comparison",
        101,
        source_sha,
        "run-1",
    )
    record = trial["runtime"]["source_attestation"]
    path = bundle_path.parent / record["path"]
    attestation = COLLECT._load_json(path)
    attestation["git_tree"] = "0" * 40
    _write_json(path, attestation)
    record["sha256"] = COLLECT._file_digest(path)

    with pytest.raises(COLLECT.EvidenceError, match="source attestation"):
        COLLECT._verify_source_attestation(bundle_path, trial["runtime"], source_sha)


def test_declared_seed_must_match_runtime_and_source_attestation(
    tmp_path: Path,
) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle_path, trial = _success_bundle(tmp_path)
    source_sha = COLLECT._source_sha()
    _bind_trial_to_protocol(
        bundle_path,
        trial,
        state,
        "dev-canonical-comparison",
        101,
        source_sha,
        "run-1",
    )
    trial["seed"] = 103
    trial["passed"] = False
    trial["verdicts"] = {
        "objective": "failed",
        "semantic": "not_run",
        "authorization": "not_run",
        "infrastructure": "passed",
    }
    trial["failure_class"] = "objective_failed"

    with pytest.raises(COLLECT.EvidenceError, match="runtime seed"):
        COLLECT.validate_trial(bundle_path, trial, state, source_sha)


def test_runtime_identity_and_retained_configuration_are_strict(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    task_id = "dev-canonical-comparison"
    runtime = _runtime(state, task_id, "a" * 40, "run-1")
    del runtime["runner_image"]
    with pytest.raises(COLLECT.EvidenceError, match="incomplete runtime identity"):
        COLLECT._validate_runtime_identity(
            tmp_path / "trial.json", runtime, state, "development", "a" * 40, 101
        )

    for invalid_image in ("academic-runner:latest", "academic-runner@sha256:x"):
        runtime = _runtime(state, task_id, "a" * 40, "run-1")
        runtime["runner_image"] = invalid_image
        with pytest.raises(COLLECT.EvidenceError, match="pinned by sha256"):
            COLLECT._validate_runtime_identity(
                tmp_path / "trial.json", runtime, state, "development", "a" * 40, 101
            )

    for configuration in (
        {"headers": {"Authorization": "Bearer must-not-be-retained"}},
        {"headers": {"Cookie": "session=must-not-be-retained"}},
        {"provider": {"api_key": "must-not-be-retained"}},
    ):
        with pytest.raises(COLLECT.EvidenceError, match="sensitive field"):
            COLLECT._reject_sensitive_configuration(configuration)

    COLLECT._reject_sensitive_configuration(
        {"temperature": 0, "provider": {"api_key": "", "region": "eastus"}}
    )

    runtime = _runtime(state, task_id, "a" * 40, "run-1")
    runtime["tool_versions"]["api_key"] = "must-not-be-retained"
    with pytest.raises(COLLECT.EvidenceError, match="runtime.tool_versions.api_key"):
        COLLECT._validate_runtime_identity(
            tmp_path / "trial.json", runtime, state, "development", "a" * 40, 101
        )

    for configuration in (
        {"headers": ["Authorization: Bearer sk-abc123def456"]},
        {"headers": ["Cookie: session=abc"]},
        {"proxy": "https://user:hunter2@proxy.internal/"},
    ):
        with pytest.raises(COLLECT.EvidenceError, match="secret-bearing value"):
            COLLECT._reject_sensitive_configuration(configuration)

    runtime = _runtime(state, task_id, "a" * 40, "run-1")
    runtime["tool_versions"]["harbor"] = "sk-proj-abcdefgh12345678"
    with pytest.raises(COLLECT.EvidenceError, match="runtime.tool_versions.harbor"):
        COLLECT._validate_runtime_identity(
            tmp_path / "trial.json", runtime, state, "development", "a" * 40, 101
        )

    bundle_path, trial = _success_bundle(tmp_path)
    trial["runtime"] = _runtime(state, task_id, "a" * 40, "run-1")
    trial["runtime"]["request_headers"] = {"Authorization": "Bearer secret"}
    trial.update(
        {
            "task_id": task_id,
            "seed": 101,
            "cohort": state["cohorts"][task_id],
            "task_digest": COLLECT._canonical_digest(state["tasks"][task_id]),
            "passed": False,
            "verdicts": {
                "objective": "failed",
                "semantic": "not_run",
                "authorization": "not_run",
                "infrastructure": "passed",
            },
            "failure_class": "objective_failed",
        }
    )
    with pytest.raises(COLLECT.EvidenceError, match="unsupported retained field"):
        COLLECT.validate_trial(bundle_path, trial, state, "a" * 40)


def test_whole_trial_scan_checks_values_but_not_legitimate_keys(
    tmp_path: Path,
) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    task_id, seed = "dev-canonical-comparison", 101
    source_sha = COLLECT._source_sha()
    bundle_path = tmp_path / "trial" / "trial.json"
    trial: dict[str, Any] = {
        "passed": False,
        "verdicts": {
            "objective": "failed",
            "semantic": "not_run",
            "authorization": "not_run",
            "infrastructure": "passed",
        },
        "failure_class": "objective_failed",
    }
    _bind_trial_to_protocol(bundle_path, trial, state, task_id, seed, source_sha, "r1")
    trial["runtime"]["configuration"]["endpoint"] = "http://host:8000/v1"
    assert "authorization" in trial["verdicts"]

    key, _ = COLLECT.validate_trial(bundle_path, trial, state, source_sha)
    assert key == (task_id, seed)

    trial["runtime"]["model"] = "Authorization: Bearer sk-abc123def456"
    with pytest.raises(COLLECT.EvidenceError, match="trial.runtime.model"):
        COLLECT.validate_trial(bundle_path, trial, state, source_sha)


def test_secret_value_pattern_ignores_urls_with_ports() -> None:
    for benign in ("http://host:8000/", "https://api.example.com:443/v1?a=b@c"):
        COLLECT._reject_sensitive_configuration({"endpoint": benign})


def test_reused_citation_row_identity_is_rejected(tmp_path: Path) -> None:
    first_path, first = _success_bundle(tmp_path, bundle_name="first", run_id="run-1")
    second_path, second = _success_bundle(
        tmp_path, bundle_name="second", run_id="run-2"
    )
    record = second["artifacts"]["citations"]
    path = second_path.parent / record["path"]
    rows = COLLECT._load_json(path)
    shared_id = COLLECT._load_json(first_path.parent / "citations.json")[0]["id"]
    rows[0]["id"] = shared_id
    _write_json(path, rows)
    record["sha256"] = COLLECT._file_digest(path)
    verdicts = {"objective": "passed", "semantic": "passed", "authorization": "passed"}

    first_ids = COLLECT._evidence_identities(first_path, first, verdicts)
    second_ids = COLLECT._evidence_identities(second_path, second, verdicts)
    assert ("citation_row", shared_id) in (first_ids & second_ids)
    assert ("citations_artifact", record["sha256"]) not in first_ids


def test_collect_rejects_reused_run_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(COLLECT, "_verify_source_checkout", lambda _: None)
    protocol_path = TASK_DIR / "protocol.json"
    protocol = COLLECT._load_json(protocol_path)
    state = COLLECT.validate_protocol(protocol)
    trials_dir = tmp_path / "trials"
    source_sha = COLLECT._source_sha()
    for index, (task_id, seed) in enumerate(sorted(state["expected"])):
        run_id = "reused-run" if index < 2 else f"run-{index}"
        trial: dict[str, Any] = {
            "passed": False,
            "verdicts": {
                "objective": "failed",
                "semantic": "not_run",
                "authorization": "not_run",
                "infrastructure": "passed",
            },
            "failure_class": "objective_failed",
        }
        bundle_path = trials_dir / f"trial-{index:02d}" / "trial.json"
        _bind_trial_to_protocol(
            bundle_path, trial, state, task_id, seed, source_sha, run_id
        )
        _write_json(bundle_path, trial)

    with pytest.raises(
        COLLECT.EvidenceError, match=r"reused (run|source_attestation) evidence"
    ):
        COLLECT.collect(protocol_path, trials_dir, tmp_path / "result.json", source_sha)


def test_collect_rejects_mixed_runtime_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(COLLECT, "_verify_source_checkout", lambda _: None)
    protocol_path = TASK_DIR / "protocol.json"
    protocol = COLLECT._load_json(protocol_path)
    state = COLLECT.validate_protocol(protocol)
    trials_dir = tmp_path / "trials"
    source_sha = COLLECT._source_sha()
    for index, (task_id, seed) in enumerate(sorted(state["expected"])):
        run_id = f"run-{index}"
        trial: dict[str, Any] = {
            "verdicts": {
                "objective": "failed",
                "semantic": "not_run",
                "authorization": "not_run",
                "infrastructure": "passed",
            },
            "failure_class": "objective_failed",
            "passed": False,
        }
        bundle_path = trials_dir / f"trial-{index:02d}" / "trial.json"
        _bind_trial_to_protocol(
            bundle_path, trial, state, task_id, seed, source_sha, run_id
        )
        if index == 1:
            trial["runtime"]["configuration"]["temperature"] = 0.5
        _write_json(bundle_path, trial)

    with pytest.raises(COLLECT.EvidenceError, match="runtime manifest differs"):
        COLLECT.collect(protocol_path, trials_dir, tmp_path / "result.json", source_sha)


def test_reused_download_artifact_is_rejected(tmp_path: Path) -> None:
    first_path, first = _success_bundle(tmp_path, bundle_name="first", run_id="run-1")
    second_path, second = _success_bundle(
        tmp_path, bundle_name="second", run_id="run-2"
    )
    second["artifacts"]["exports"]["markdown"]["sha256"] = first["artifacts"][
        "exports"
    ]["markdown"]["sha256"]
    first_ids = COLLECT._evidence_identities(
        first_path,
        first,
        {"objective": "passed", "semantic": "passed", "authorization": "passed"},
    )
    second_ids = COLLECT._evidence_identities(
        second_path,
        second,
        {"objective": "passed", "semantic": "passed", "authorization": "passed"},
    )
    assert (
        "markdown_artifact",
        first["artifacts"]["exports"]["markdown"]["sha256"],
    ) in (first_ids & second_ids)


def test_collect_reports_every_denominator_without_scoring_unavailable_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(COLLECT, "_verify_source_checkout", lambda _: None)
    protocol_path = TASK_DIR / "protocol.json"
    protocol = COLLECT._load_json(protocol_path)
    state = COLLECT.validate_protocol(protocol)
    trials_dir = tmp_path / "trials"
    source_sha = COLLECT._source_sha()

    expected = sorted(state["expected"])
    for index, (task_id, seed) in enumerate(expected):
        run_id = f"run-{index}"
        trial: dict[str, Any]
        if index == 0:
            trial = {
                "verdicts": {
                    "objective": "not_run",
                    "semantic": "not_run",
                    "authorization": "not_run",
                    "infrastructure": "failed",
                },
                "failure_class": "provider_unavailable",
            }
            bundle_path = trials_dir / f"trial-{index:02d}" / "trial.json"
        elif index == 1:
            bundle_path, trial = _success_bundle(
                trials_dir, bundle_name=f"trial-{index:02d}", run_id=run_id
            )
            trial["verdicts"] = {
                "objective": "passed",
                "semantic": "judge_unavailable",
                "authorization": "passed",
                "infrastructure": "passed",
            }
            trial["failure_class"] = "judge_unavailable"
        else:
            trial = {
                "verdicts": {
                    "objective": "failed",
                    "semantic": "not_run",
                    "authorization": "not_run",
                    "infrastructure": "passed",
                },
                "failure_class": "objective_failed",
            }
            bundle_path = trials_dir / f"trial-{index:02d}" / "trial.json"
        _bind_trial_to_protocol(
            bundle_path, trial, state, task_id, seed, source_sha, run_id
        )
        trial["passed"] = False
        _write_json(bundle_path, trial)

    output = tmp_path / "result.json"
    report = COLLECT.collect(protocol_path, trials_dir, output, source_sha)

    assert report["sample_size"] == 14
    assert report["sample_sizes"] == {
        "canonical": 5,
        "near_boundary": 9,
        "development": 9,
        "held_out": 5,
    }
    assert report["product_passes"] == 0
    assert report["product_failures"] == 12
    assert report["unscored"] == 2
    assert report["dimensions"]["infrastructure"] == {"failed": 1, "passed": 13}
    assert report["dimensions"]["semantic"] == {
        "judge_unavailable": 1,
        "not_run": 13,
    }
    assert report["by_kind"]["canonical"]["sample_size"] == 5
    assert report["by_kind"]["near_boundary"]["sample_size"] == 9
    assert report["canonical_semantic_acceptance"] == {
        "passes": 0,
        "required": 4,
        "met": False,
    }
    assert json.loads(output.read_text(encoding="utf-8"))["sample_size"] == 14


def test_source_checkout_requires_exact_clean_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "collector@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Collector Test"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(COLLECT, "ROOT", repository)

    COLLECT._verify_source_checkout(source_sha)
    tracked.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(COLLECT.EvidenceError, match="tracked modifications"):
        COLLECT._verify_source_checkout(source_sha)
