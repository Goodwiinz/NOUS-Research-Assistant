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
        "- Hallucination Audit of a Hospital Formulary RAG Assistant | NOUS library\n"
        "- arXiv:2509.17001v1 | arXiv\n"
        "- DOI 10.5555/nouseval.2025.40517001 | PubMed\n"
    )
    return trial


def test_aliases_of_actual_subset_results_are_accepted(tmp_path: Path) -> None:
    code, report = verify_trial(subset_trial(), tmp_path)
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
