"""Contract tests for the fail-closed Test Pipeline release gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "assert_required_jobs.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"

EXPECTED_REQUIRED_JOBS = (
    "lint-backend",
    "lint-frontend",
    "migration-check",
    "openapi-contract",
    "unit-tests",
    "golden-replay",
    "integration-tests",
    "resilience-tests",
    "frontend-tests",
    "security-scan",
    "e2e-tests",
)
BLOCKING_RESULTS = (
    "failure",
    "skipped",
    "cancelled",
    "timed_out",
    "action_required",
    "neutral",
    "stale",
    "",
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assert_required_jobs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_script()


def _success_results(*, nested: bool = False) -> dict[str, Any]:
    if nested:
        return {
            job: {"result": "success", "outputs": {}} for job in EXPECTED_REQUIRED_JOBS
        }
    return {job: "success" for job in EXPECTED_REQUIRED_JOBS}


def _workflow() -> dict[str, Any]:
    return cast(
        "dict[str, Any]", yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    )


def test_required_job_contract_is_explicit() -> None:
    assert gate.REQUIRED_JOBS == EXPECTED_REQUIRED_JOBS


def test_all_success_is_the_only_passing_matrix() -> None:
    results = gate.normalize_results(_success_results())
    assert gate.blocking_results(results) == {}
    assert gate.main(["--results-json", json.dumps(_success_results())]) == 0


def test_github_needs_shape_is_supported() -> None:
    results = gate.normalize_results(_success_results(nested=True))
    assert gate.blocking_results(results) == {}


@pytest.mark.parametrize("job", EXPECTED_REQUIRED_JOBS)
@pytest.mark.parametrize("result", BLOCKING_RESULTS)
def test_every_non_success_result_blocks(job: str, result: str) -> None:
    payload = _success_results()
    payload[job] = result
    normalized = gate.normalize_results(payload)
    assert gate.blocking_results(normalized) == {job: result}


@pytest.mark.parametrize("job", EXPECTED_REQUIRED_JOBS)
def test_every_missing_result_blocks(job: str) -> None:
    payload = _success_results()
    del payload[job]
    normalized = gate.normalize_results(payload)
    assert gate.blocking_results(normalized) == {job: gate.MISSING_RESULT}


@pytest.mark.parametrize("value", [None, True, 0, [], {"outputs": {}}])
def test_malformed_result_blocks(value: object) -> None:
    payload = _success_results()
    payload["lint-backend"] = value
    normalized = gate.normalize_results(payload)
    assert normalized["lint-backend"] == gate.INVALID_RESULT
    assert gate.blocking_results(normalized) == {"lint-backend": gate.INVALID_RESULT}


def test_cli_fails_for_non_success_and_writes_summary(tmp_path: Path) -> None:
    payload = _success_results(nested=True)
    payload["e2e-tests"]["result"] = "cancelled"
    summary = tmp_path / "summary.md"

    assert (
        gate.main(
            [
                "--results-json",
                json.dumps(payload),
                "--summary-file",
                str(summary),
            ]
        )
        == 1
    )
    text = summary.read_text(encoding="utf-8")
    assert "## Release Gate" in text
    assert "E2E Tests | :x: `cancelled`" in text


@pytest.mark.parametrize("payload", ["", "[]", "null", "not-json"])
def test_cli_rejects_invalid_payloads(payload: str) -> None:
    assert gate.main(["--results-json", payload]) == 2


def test_workflow_has_one_named_fail_closed_release_gate() -> None:
    job = _workflow()["jobs"]["test-summary"]
    assert job["name"] == "Release Gate"
    assert job["if"] == "always()"
    assert (
        tuple(job["needs"])
        == ("ci-plan", "lightweight-checks") + EXPECTED_REQUIRED_JOBS
    )

    checkout_indexes = [
        index
        for index, step in enumerate(job["steps"])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assertion_steps = [
        (index, step)
        for index, step in enumerate(job["steps"])
        if "assert_required_jobs.py" in str(step.get("run", ""))
    ]
    assert checkout_indexes == [0]
    checkout = job["steps"][checkout_indexes[0]]["uses"]
    assert checkout == ("actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd")
    assert len(assertion_steps) == 1
    assertion_index, step = assertion_steps[0]
    assert checkout_indexes[0] < assertion_index
    assert "toJSON(needs)" in str(step.get("env", {}).get("REQUIRED_JOB_RESULTS"))
    assert "needs.ci-plan.outputs.profile" in str(step.get("env", {}).get("CI_PROFILE"))
    assert '--profile "$CI_PROFILE"' in str(step.get("run", ""))
    assert "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))
    assert not step.get("continue-on-error", False)


def test_docs_only_changes_still_receive_the_required_workflow() -> None:
    workflow = _workflow()
    # PyYAML follows YAML 1.1 and may deserialize the unquoted ``on`` key as True.
    triggers = workflow.get("on") or workflow.get(True)  # type: ignore[call-overload]
    assert isinstance(triggers, dict)
    assert "pull_request" in triggers
    assert "push" in triggers
    assert "paths-ignore" not in triggers["pull_request"]
    assert "paths-ignore" not in triggers["push"]
