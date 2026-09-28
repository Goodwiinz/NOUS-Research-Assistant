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


def _success_bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    bundle = tmp_path / "trial-success"
    bundle.mkdir()
    draft = bundle / "draft.md"
    draft.write_text("## Result\n\nSupported claim [Doc 2].", encoding="utf-8")
    digest = COLLECT._file_digest(draft)
    review = bundle / "review.json"
    _write_json(
        review,
        {
            "id": "review-1",
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
            }
        ],
    )
    markdown = bundle / "download.md"
    markdown.write_text(
        "## Result\n\nSupported claim [Doc 2].\n\n## References\n\n"
        "[Doc 2] Canonical source.",
        encoding="utf-8",
    )
    latex = bundle / "download.zip"
    with zipfile.ZipFile(latex, "w") as archive:
        archive.writestr("draft.tex", r"Supported claim \cite{doc2}.")
        archive.writestr(
            "references.bib", "@article{doc2,\n  title = {Canonical source}\n}\n"
        )
    adjudication = bundle / "adjudication.json"
    task_digest = "sha256:" + "b" * 64
    _write_json(
        adjudication,
        {
            "id": "adjudication-1",
            "reviewer": "independent-reviewer",
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
            "draft_id": "draft-1",
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
            "id": "review-blocked",
            "outcome": "blocked",
            "candidate_content_hash": "sha256:" + "c" * 64,
        },
    )
    trial = {
        "task_digest": task_digest,
        "artifacts": {
            "draft": {
                **_record(draft),
                "id": "draft-1",
                "version": 3,
                "content_sha256": digest,
            },
            "review": _record(review),
            "adjudication": _record(adjudication),
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
            "review_id": "review-blocked",
            "review": _record(blocked_review),
            "before": {
                "draft_id": "draft-1",
                "version": 3,
                "content_sha256": digest,
            },
            "after": {
                "draft_id": "draft-1",
                "version": 3,
                "content_sha256": digest,
            },
        },
    }
    return bundle / "trial.json", trial


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


def test_infrastructure_and_unavailable_judge_cannot_be_product_passes(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "trial.json"
    with pytest.raises(COLLECT.EvidenceError, match="infrastructure failure"):
        COLLECT._validate_verdicts(
            bundle,
            {
                "verdicts": {
                    "objective": "passed",
                    "semantic": "not_run",
                    "authorization": "not_run",
                    "infrastructure": "failed",
                }
            },
        )

    with pytest.raises(COLLECT.EvidenceError, match="unavailable judge"):
        COLLECT._validate_verdicts(
            bundle,
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


def test_product_pass_requires_authenticated_real_provider(tmp_path: Path) -> None:
    protocol = COLLECT._load_json(TASK_DIR / "protocol.json")
    state = COLLECT.validate_protocol(protocol)
    bundle_path, trial = _success_bundle(tmp_path)
    task_id = "dev-canonical-comparison"
    trial.update(
        {
            "task_id": task_id,
            "seed": 101,
            "cohort": "development",
            "task_digest": COLLECT._canonical_digest(state["tasks"][task_id]),
            "runtime": {
                "source_sha": "a" * 40,
                "provider": "provider",
                "model": "model",
                "configuration": {"temperature": 0},
                "authenticated": False,
                "real_provider": True,
            },
            "verdicts": {
                "objective": "passed",
                "semantic": "passed",
                "authorization": "passed",
                "infrastructure": "passed",
            },
        }
    )
    _write_json(bundle_path, trial)

    with pytest.raises(COLLECT.EvidenceError, match="authenticated real-provider"):
        COLLECT.validate_trial(bundle_path, trial, state, "a" * 40)


def test_retained_configuration_rejects_secrets() -> None:
    with pytest.raises(COLLECT.EvidenceError, match="sensitive field"):
        COLLECT._reject_sensitive_configuration(
            {"temperature": 0, "provider": {"api_key": "must-not-be-retained"}}
        )

    COLLECT._reject_sensitive_configuration(
        {"temperature": 0, "provider": {"api_key": "", "region": "eastus"}}
    )


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
        if index == 0:
            verdicts = {
                "objective": "not_run",
                "semantic": "not_run",
                "authorization": "not_run",
                "infrastructure": "failed",
            }
            reason = "provider unavailable"
        elif index == 1:
            verdicts = {
                "objective": "passed",
                "semantic": "judge_unavailable",
                "authorization": "passed",
                "infrastructure": "passed",
            }
            reason = "judge unavailable"
        else:
            verdicts = {
                "objective": "failed",
                "semantic": "not_run",
                "authorization": "passed",
                "infrastructure": "passed",
            }
            reason = "objective artifact check failed"
        trial = {
            "task_id": task_id,
            "seed": seed,
            "cohort": state["cohorts"][task_id],
            "task_digest": COLLECT._canonical_digest(state["tasks"][task_id]),
            "runtime": {
                "source_sha": source_sha,
                "provider": "test-provider",
                "model": "test-model",
                "configuration": {"temperature": 0},
            },
            "verdicts": verdicts,
            "failure_reason": reason,
        }
        _write_json(trials_dir / f"trial-{index:02d}" / "trial.json", trial)

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
