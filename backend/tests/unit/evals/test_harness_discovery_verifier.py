"""Calibrate the real discovery CLI against successful and adversarial trials."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
TASK = ROOT / "evals/harness-mcp-discovery-v1/tests"


def verify_trial(trial: dict[str, Any], tmp_path: Path) -> tuple[int, dict[str, Any]]:
    fixture = tmp_path / "trial.json"
    fixture.write_text(json.dumps(trial))
    report = tmp_path / "report.json"
    process = subprocess.run(
        [sys.executable, str(TASK / "verify.py")],
        env={
            **os.environ,
            "BENCHMARK_CALIBRATION_FIXTURE": str(fixture),
            "VERIFIER_REPORT_PATH": str(report),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.exists(), process.stderr
    return process.returncode, json.loads(report.read_text())


@pytest.mark.parametrize(
    "fixture", sorted((TASK / "calibration").glob("*.json")), ids=lambda p: p.stem
)
def test_registered_calibration_exits(fixture: Path, tmp_path: Path) -> None:
    expected = (
        2
        if fixture.stem.startswith("infra-")
        else 10 if fixture.stem.startswith("wrong-") else 0
    )
    code, report = verify_trial(json.loads(fixture.read_text()), tmp_path)
    assert code == expected, report
    if expected == 2:
        assert "InfrastructureFailure" in report.get("verifier_error", ""), report
    else:
        assert report["passed"] is (expected == 0)


def subset_trial() -> dict[str, Any]:
    trial: dict[str, Any] = json.loads((TASK / "calibration/pass.json").read_text())
    for event in trial["state"]["events"]:
        if event.get("returned_ids"):
            event["returned_ids"] = event["returned_ids"][:1]
    trial["evidence"]["answer"] = (
        "- Hallucination Audit of a Hospital Formulary RAG Assistant | "
        "9b3d1f68-2e7c-4a05-b4d9-0f8e6c2a5d73 | NOUS library\n"
        "- Chart-Aware Retrieval-Augmented Generation for Clinical Question "
        "Answering over Electronic Health Records | arXiv:2509.17001v1 | arXiv\n"
        "- Retrieval-augmented large language models for answering clinicians' "
        "drug-dosing questions: a prospective evaluation | "
        "DOI 10.5555/nouseval.2025.40517001 | PubMed\n"
    )
    return trial


def test_aliases_of_actual_subset_results_are_accepted(tmp_path: Path) -> None:
    code, report = verify_trial(subset_trial(), tmp_path)
    assert code == 0, report


def test_method_notes_in_prose_preserve_a_valid_answer(tmp_path: Path) -> None:
    trial = subset_trial()
    trial["evidence"][
        "answer"
    ] += "\nMethods: Searched the three NOUS sources for clinical question answering.\n"
    code, report = verify_trial(trial, tmp_path)
    assert code == 0, report


@pytest.mark.parametrize(
    "unreturned",
    [
        "Ward-Round Notes on Citation-Grounded RAG for Clinical Question Answering",
        "arXiv:2509.17002v1",
        "DOI 10.5555/nouseval.2025.40517002",
    ],
)
def test_unreturned_paper_alias_cannot_ride_on_valid_citations(
    unreturned: str, tmp_path: Path
) -> None:
    trial = subset_trial()
    trial["evidence"]["answer"] += "- " + unreturned + "\n"
    code, report = verify_trial(trial, tmp_path)
    assert code == 10, report
    assert report["answer_audit"]["uncited_paper_rows"], report


def test_error_frame_ids_cannot_supply_citations_after_a_retry(tmp_path: Path) -> None:
    trial = subset_trial()
    event = next(
        e for e in trial["state"]["events"] if e.get("tool_name") == "search_documents"
    )
    failed = copy.deepcopy(event)
    failed.update(seq=0, is_error=True)
    event["returned_ids"] = ["4c0e7a52-91d3-4b6f-8e2a-6d5f0b7c3a19"]
    trial["state"]["events"].append(failed)
    code, report = verify_trial(trial, tmp_path)
    assert code == 2, report
    assert "gateway/application error" in report.get("verifier_error", ""), report


@pytest.mark.parametrize("status", [200, 503])
def test_service_failure_cannot_be_selected_away_by_a_successful_retry(
    status: int, tmp_path: Path
) -> None:
    trial = subset_trial()
    failed = copy.deepcopy(
        next(
            e for e in trial["state"]["events"] if e.get("tool_name") == "search_arxiv"
        )
    )
    failed.update(seq=0, status=status, is_error=True, returned_ids=[])
    trial["state"]["events"].append(failed)
    code, report = verify_trial(trial, tmp_path)
    assert code == 2, report
    assert "gateway/application error" in report.get("verifier_error", ""), report


@pytest.mark.parametrize("status", [200, 503])
def test_unrecovered_gateway_failures_are_infrastructure(
    status: int, tmp_path: Path
) -> None:
    trial = subset_trial()
    event = next(
        e for e in trial["state"]["events"] if e.get("tool_name") == "search_arxiv"
    )
    event.update(status=status, is_error=True, returned_ids=[])
    code, report = verify_trial(trial, tmp_path)
    assert code == 2, report
    assert "gateway/application error" in report.get("verifier_error", ""), report


def load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "discovery_verifier_capture", TASK / "verify.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_oversized_answer_capture_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_verifier()
    trial = subset_trial()
    answer = tmp_path / "answer.md"
    answer.write_text(trial["evidence"]["answer"] + "x" * module.MAX_ANSWER_BYTES)
    monkeypatch.setattr(module, "ANSWER_PATH", answer)
    evidence = module.capture_answer()
    assert evidence["answer_truncated"] is True
    assert len(evidence["answer"].encode()) == module.MAX_ANSWER_BYTES
    assert "capture limit" in " ".join(
        module.objective_failures(evidence, trial["state"])
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "Ward-Round Notes on Citation-Grounded RAG for Clinical Question Answering",
            "Hallucination Audit of a Hospital Formulary RAG Assistant",
        ),
        ("arXiv:2509.17001v1 | arXiv", "arXiv:2509.17002v1 | arXiv"),
        ("arXiv:2509.17001v1 | arXiv", "arXiv:2509.17001v1 | NOUS library"),
        (
            "arXiv:2509.17001v1 | arXiv",
            "https://arxiv.org/abs/2509.17001v1 | NOUS library",
        ),
        ("40517001 | PubMed", "40517001 | arXiv"),
    ],
    ids=[
        "wrong-title",
        "wrong-returned-id",
        "wrong-source",
        "url-is-not-source",
        "wrong-pubmed-source",
    ],
)
def test_returned_tokens_do_not_validate_mismatched_paper_fields(
    old: str, new: str, tmp_path: Path
) -> None:
    trial = json.loads((TASK / "calibration/pass.json").read_text())
    assert old in trial["evidence"]["answer"]
    trial["evidence"]["answer"] = trial["evidence"]["answer"].replace(old, new, 1)
    code, report = verify_trial(trial, tmp_path)
    assert code == 10, report
    assert report["answer_audit"]["uncited_paper_rows"], report


def test_identifiers_in_prose_do_not_replace_required_paper_rows(
    tmp_path: Path,
) -> None:
    trial = subset_trial()
    trial["evidence"]["answer"] = trial["evidence"]["answer"].replace("- ", "")
    code, report = verify_trial(trial, tmp_path)
    assert code == 10, report
    assert report["answer_audit"]["matched_search_tools"] == [], report


def test_multiple_identifiers_must_belong_to_the_same_paper(tmp_path: Path) -> None:
    trial = json.loads((TASK / "calibration/pass.json").read_text())
    trial["evidence"]["answer"] = trial["evidence"]["answer"].replace(
        "PMID 40517001, DOI 10.5555/nouseval.2025.40517001",
        "PMID 40517001, DOI 10.5555/nouseval.2025.40517002",
        1,
    )
    code, report = verify_trial(trial, tmp_path)
    assert code == 10, report
    assert report["answer_audit"]["uncited_paper_rows"], report


def test_same_paper_arxiv_doi_and_version_aliases_remain_valid(tmp_path: Path) -> None:
    trial = subset_trial()
    trial["evidence"]["answer"] = trial["evidence"]["answer"].replace(
        "arXiv:2509.17001v1", "DOI 10.48550/arXiv.2509.17001v1"
    )
    code, report = verify_trial(trial, tmp_path)
    assert code == 0, report


def test_title_field_must_equal_one_returned_title(tmp_path: Path) -> None:
    trial = subset_trial()
    trial["evidence"]["answer"] = trial["evidence"]["answer"].replace(
        "Hallucination Audit", "Invented paper borrowing Hallucination Audit", 1
    )
    code, report = verify_trial(trial, tmp_path)
    assert code == 10, report
    assert report["answer_audit"]["uncited_paper_rows"], report


def test_displayed_markdown_title_and_source_fields_remain_valid(
    tmp_path: Path,
) -> None:
    trial = subset_trial()
    trial["evidence"]["answer"] = (
        trial["evidence"]["answer"]
        .replace(
            "Hallucination Audit of a Hospital Formulary RAG Assistant",
            "**Hallucination Audit of a Hospital Formulary RAG Assistant**",
            1,
        )
        .replace("| NOUS library", "| **NOUS library**", 1)
    )
    code, report = verify_trial(trial, tmp_path)
    assert code == 0, report
