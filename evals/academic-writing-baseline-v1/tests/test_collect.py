from __future__ import annotations

import importlib.util
import json
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
    state: dict[str, Any], task_id: str, source_sha: str, run_id: str
) -> dict[str, Any]:
    cohort = state["cohorts"][task_id]
    return {
        "run_id": run_id,
        "source_sha": source_sha,
        "provider": "test-provider",
        "model": "test-model",
        "runner_image": "academic-runner@sha256:" + "d" * 64,
        "verifier": "writing-verifier-v1",
        "fixture_digest": state["corpus_digests"][cohort],
        "harness_digest": COLLECT._harness_digest(),
        "tool_versions": {"harbor": "0.6.6", "python": "3.11.14"},
        "env_flags": {"network": "restricted", "judge_access": "verifier-only"},
        "configuration": {"temperature": 0},
        "authenticated": True,
        "real_provider": True,
    }


def _success_bundle(
    tmp_path: Path,
    *,
    bundle_name: str = "trial-success",
    run_id: str = "run-1",
) -> tuple[Path, dict[str, Any]]:
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
            "outcome": "passed",
            "candidate_content_hash": digest.removeprefix("sha256:"),
        },
    )
    citations = bundle / "citations.json"
    _write_json(
        citations,
        [
            {
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
        archive.writestr("draft.tex", r"Supported claim \cite{doc2}.")
        archive.writestr("references.bib", bibliography)
    task_digest = "sha256:" + "b" * 64
    fixture_digest = "sha256:" + "e" * 64
    calibration = bundle / "calibration.json"
    _write_json(
        calibration,
        {
            "id": f"calibration-{run_id}",
            "reviewer": "independent-reviewer",
            "verifier": "writing-verifier-v1",
            "fixture_digest": fixture_digest,
            "outcome": "passed",
            "correct_fixture_passed": True,
            "wrong_fixture_rejected": True,
        },
    )
    adjudication = bundle / "adjudication.json"
    _write_json(
        adjudication,
        {
            "id": f"adjudication-{run_id}",
            "run_id": run_id,
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
                "collaborator_download": 200,
                "foreign_project_download": 404,
            },
        },
    )
    blocked_review = bundle / "blocked-review.json"
    _write_json(
        blocked_review,
        {
            "id": f"review-blocked-{run_id}",
            "outcome": "blocked",
            "candidate_content_hash": "sha256:" + "c" * 64,
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
        },
        "artifacts": {
            "draft": {
                **_record(draft),
                "id": f"draft-{run_id}",
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
            "runtime": _runtime(state, task_id, source_sha, run_id),
        }
    )
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
        calibration["fixture_digest"] = trial["runtime"]["fixture_digest"]
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


def test_bibtex_metadata_must_match_canonical_citation_rows(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    latex_record = trial["artifacts"]["exports"]["latex"]
    latex_path = bundle_path.parent / latex_record["path"]
    with zipfile.ZipFile(latex_path, "w") as archive:
        archive.writestr("draft.tex", r"Supported claim \cite{doc2}.")
        archive.writestr("references.bib", "@article{doc2, title={Fabricated title}}")
    latex_record["sha256"] = COLLECT._file_digest(latex_path)

    with pytest.raises(COLLECT.EvidenceError, match="differs from canonical metadata"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


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

    with pytest.raises(COLLECT.EvidenceError, match="cannot produce a product pass"):
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
    del trial["artifacts"]["authorization"]
    verdicts = {
        "objective": "passed",
        "semantic": "judge_unavailable",
        "authorization": "passed",
        "infrastructure": "passed",
    }

    with pytest.raises(COLLECT.EvidenceError, match="artifact record"):
        COLLECT._verify_passed_artifacts(bundle_path, trial, verdicts)


def test_semantic_pass_requires_bound_calibration(tmp_path: Path) -> None:
    bundle_path, trial = _success_bundle(tmp_path)
    calibration_record = trial["artifacts"]["calibration"]
    calibration_path = bundle_path.parent / calibration_record["path"]
    calibration = COLLECT._load_json(calibration_path)
    calibration["wrong_fixture_rejected"] = False
    _write_json(calibration_path, calibration)
    calibration_record["sha256"] = COLLECT._file_digest(calibration_path)

    with pytest.raises(COLLECT.EvidenceError, match="calibration evidence"):
        COLLECT._verify_success_artifacts(bundle_path, trial)


def test_product_pass_requires_authenticated_real_provider(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle_path, trial = _success_bundle(tmp_path)
    task_id = "dev-canonical-comparison"
    _bind_trial_to_protocol(bundle_path, trial, state, task_id, 101, "a" * 40, "run-1")
    trial["runtime"]["authenticated"] = False
    trial["passed"] = True
    trial["verdicts"] = {
        "objective": "passed",
        "semantic": "passed",
        "authorization": "passed",
        "infrastructure": "passed",
    }

    with pytest.raises(COLLECT.EvidenceError, match="authenticated real-provider"):
        COLLECT.validate_trial(bundle_path, trial, state, "a" * 40)


def test_runtime_identity_and_retained_configuration_are_strict(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    task_id = "dev-canonical-comparison"
    runtime = _runtime(state, task_id, "a" * 40, "run-1")
    del runtime["runner_image"]
    with pytest.raises(COLLECT.EvidenceError, match="incomplete runtime identity"):
        COLLECT._validate_runtime_identity(
            tmp_path / "trial.json", runtime, state, "development", "a" * 40
        )

    runtime = _runtime(state, task_id, "a" * 40, "run-1")
    runtime["runner_image"] = "academic-runner:latest"
    with pytest.raises(COLLECT.EvidenceError, match="pinned by sha256"):
        COLLECT._validate_runtime_identity(
            tmp_path / "trial.json", runtime, state, "development", "a" * 40
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


def test_collect_rejects_reused_run_identity(tmp_path: Path) -> None:
    protocol_path = TASK_DIR / "protocol.json"
    protocol = COLLECT._load_json(protocol_path)
    state = COLLECT.validate_protocol(protocol)
    trials_dir = tmp_path / "trials"
    source_sha = "a" * 40
    for index, (task_id, seed) in enumerate(sorted(state["expected"])):
        run_id = "reused-run" if index < 2 else f"run-{index}"
        trial = {
            "task_id": task_id,
            "seed": seed,
            "cohort": state["cohorts"][task_id],
            "task_digest": COLLECT._canonical_digest(state["tasks"][task_id]),
            "runtime": _runtime(state, task_id, source_sha, run_id),
            "passed": False,
            "verdicts": {
                "objective": "failed",
                "semantic": "not_run",
                "authorization": "not_run",
                "infrastructure": "passed",
            },
            "failure_reason": "objective artifact check failed",
        }
        _write_json(trials_dir / f"trial-{index:02d}" / "trial.json", trial)

    with pytest.raises(COLLECT.EvidenceError, match="reused run evidence"):
        COLLECT.collect(protocol_path, trials_dir, tmp_path / "result.json", source_sha)


def test_collect_reports_every_denominator_without_scoring_unavailable_runtime(
    tmp_path: Path,
) -> None:
    protocol_path = TASK_DIR / "protocol.json"
    protocol = COLLECT._load_json(protocol_path)
    state = COLLECT.validate_protocol(protocol)
    trials_dir = tmp_path / "trials"
    source_sha = "a" * 40

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
                "failure_reason": "provider unavailable",
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
            trial["failure_reason"] = "judge unavailable"
        else:
            trial = {
                "verdicts": {
                    "objective": "failed",
                    "semantic": "not_run",
                    "authorization": "not_run",
                    "infrastructure": "passed",
                },
                "failure_reason": "objective artifact check failed",
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
    assert json.loads(output.read_text(encoding="utf-8"))["sample_size"] == 14
