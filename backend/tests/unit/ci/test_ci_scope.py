"""Exercise CI selection and the release decision with real Git and CLI inputs.

These regressions catch excluded runtime paths, lost rename/deletion evidence,
and a gate accepting an unplanned or unsuccessful required job.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
GATE = ROOT / "scripts/ci/assert_required_jobs.py"
SELECTOR = ROOT / "scripts/ci/select_ci_jobs.py"
JOBS = (
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
SELECTED = {
    "docs": (),
    "frontend": ("lint-frontend", "frontend-tests", "e2e-tests"),
    "backend": (
        "lint-backend",
        "migration-check",
        "openapi-contract",
        "unit-tests",
        "golden-replay",
        "integration-tests",
        "resilience-tests",
        "security-scan",
        "e2e-tests",
    ),
    "full": JOBS,
}
FLAGS = {
    "docs": ("false", "false", "false", "false"),
    "frontend": ("false", "true", "false", "true"),
    "backend": ("true", "false", "true", "true"),
    "full": ("true", "true", "true", "true"),
}


def _needs(profile: str) -> dict[str, Any]:
    backend, frontend, integration, e2e = FLAGS[profile]
    return {
        "ci-plan": {
            "result": "success",
            "outputs": {
                "profile": profile,
                "backend": backend,
                "frontend": frontend,
                "integration": integration,
                "e2e": e2e,
            },
        },
        "lightweight-checks": {"result": "success", "outputs": {}},
        **{
            job: {
                "result": "success" if job in SELECTED[profile] else "skipped",
                "outputs": {},
            }
            for job in JOBS
        },
    }


def _gate(
    profile: str, needs: dict[str, Any], *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--profile",
            profile,
            "--results-json",
            json.dumps(needs),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("profile", SELECTED)
def test_only_successful_selected_jobs_and_explicit_skips_pass(profile: str) -> None:
    result = _gate(profile, _needs(profile))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("profile", SELECTED)
@pytest.mark.parametrize(
    "result", ["failure", "cancelled", "skipped", "neutral", "", None]
)
def test_detector_and_lightweight_checks_cannot_be_skipped(
    profile: str, result: Any
) -> None:
    for job in ("ci-plan", "lightweight-checks"):
        needs = _needs(profile)
        needs[job]["result"] = result
        assert _gate(profile, needs).returncode != 0


@pytest.mark.parametrize(
    ("profile", "job"),
    [(profile, job) for profile, jobs in SELECTED.items() for job in jobs],
)
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_every_selected_job_still_requires_exact_success(
    profile: str, job: str, result: str
) -> None:
    needs = _needs(profile)
    needs[job]["result"] = result
    assert _gate(profile, needs).returncode != 0


@pytest.mark.parametrize("job", JOBS)
def test_docs_profile_does_not_hide_missing_or_failed_optional_jobs(job: str) -> None:
    needs = _needs("docs")
    needs[job]["result"] = "failure"
    assert _gate("docs", needs).returncode != 0
    del needs[job]
    assert _gate("docs", needs).returncode != 0


@pytest.mark.parametrize(
    "field", ["profile", "backend", "frontend", "integration", "e2e"]
)
def test_missing_or_inconsistent_plan_outputs_block(field: str) -> None:
    needs = _needs("frontend")
    del needs["ci-plan"]["outputs"][field]
    assert _gate("frontend", needs).returncode != 0
    needs["ci-plan"]["outputs"][field] = "docs" if field == "profile" else "false"
    if field == "backend":
        needs["ci-plan"]["outputs"][field] = "true"
    if field == "integration":
        needs["ci-plan"]["outputs"][field] = "true"
    assert _gate("frontend", needs).returncode != 0


def test_unknown_profile_and_new_unaccounted_dependencies_block() -> None:
    assert _gate("unknown", _needs("docs")).returncode != 0
    needs = _needs("docs")
    needs["new-required-check"] = {"result": "failure"}
    assert _gate("docs", needs).returncode != 0


def test_docs_summary_reports_skips_as_not_selected(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    result = _gate("docs", _needs("docs"), "--summary-file", str(summary))
    assert result.returncode == 0, result.stderr
    assert "not selected" in summary.read_text().lower()
    assert "skipped" in summary.read_text()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.name", "CI scope test")
    _git(repo, "config", "user.email", "ci-scope@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "README.md").write_text("Initial docs\n")
    (repo / "backend/src").mkdir(parents=True)
    (repo / "backend/src/runtime.py").write_text("def value():\n    return 1\n")
    (repo / "backend/openapi.json").write_text("{}\n")
    (repo / "frontend/src/types/generated").mkdir(parents=True)
    (repo / "frontend/src/types/generated/api.d.ts").write_text(
        "export interface paths {}\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feature/test")
    return repo, base


def _select(
    repo: Path, base: str, event: str = "pull_request", branch: str = "feature/test"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--event-name",
            event,
            "--base",
            base,
            "--head",
            _git(repo, "rev-parse", "HEAD"),
            "--head-branch",
            branch,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("paths", "profile"),
    [
        (["docs/engineering/testing.md", ".claude/commands/nous-loop.md"], "docs"),
        (["README.md", "backend/README.md", ".github/workflows/README.md"], "docs"),
        (["frontend/src/chat.tsx"], "frontend"),
        (["backend/src/api/search.py"], "backend"),
        (["backend/src/prompts/system.md"], "backend"),
        (["frontend/src/content/prompt.md"], "frontend"),
        (["docs/plan.md", "backend/src/api/search.py"], "backend"),
        (["frontend/src/chat.tsx", "backend/src/api/search.py"], "full"),
        ([".github/workflows/test-pipeline.yml"], "full"),
        (["scripts/ci/assert_required_jobs.py"], "full"),
        (["pnpm-lock.yaml"], "full"),
        (["backend/requirements.txt"], "full"),
        (["backend/constraints-ci.txt"], "full"),
        (["frontend/package.json"], "full"),
        (["frontend/src/package.json"], "full"),
        (["frontend/src/types/generated/api.d.ts"], "full"),
        (["backend/openapi.json"], "full"),
        (["backend/src/requirements.txt"], "full"),
        (["frontend/yarn.lock"], "full"),
        (["backend/src/prompts/README.md"], "backend"),
        (["infrastructure/helm/values-dev.yaml"], "full"),
        (["backend/docker/Dockerfile.prod"], "full"),
        (["unknown/runtime.md"], "full"),
        (["tests/e2e/chat.spec.ts"], "full"),
        (["docs/plan.md", "config/settings.json"], "full"),
    ],
)
def test_real_pr_diff_selects_affected_checks(
    repository: tuple[Path, str], paths: list[str], profile: str
) -> None:
    repo, base = repository
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Change")
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    assert outputs["profile"] == profile
    assert (
        tuple(outputs[flag] for flag in ("backend", "frontend", "integration", "e2e"))
        == FLAGS[profile]
    )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "merge_group"])
def test_release_and_unrecognized_events_keep_full_verification(
    repository: tuple[Path, str], event: str
) -> None:
    repo, base = repository
    result = _select(repo, base, event)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "full"


def test_stale_release_proposal_cannot_skip_the_existing_source_guard(
    repository: tuple[Path, str],
) -> None:
    repo, base = repository
    (repo / "README.md").write_text("Docs change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Docs")
    result = _select(repo, base, branch="codex/release-dev-" + "a" * 40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "full"


@pytest.mark.parametrize("rename", [False, True])
def test_deleted_or_renamed_runtime_code_is_never_docs_only(
    repository: tuple[Path, str], rename: bool
) -> None:
    repo, base = repository
    runtime = repo / "backend/src/runtime.py"
    if rename:
        (repo / "docs").mkdir()
        runtime.rename(repo / "docs/moved.md")
    else:
        runtime.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Remove runtime code")
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "backend"


def test_empty_diff_is_full_and_unresolved_base_blocks_detection(
    repository: tuple[Path, str],
) -> None:
    repo, base = repository
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "full"
    assert _select(repo, "does-not-exist").returncode != 0


def test_cross_surface_rename_selects_both_sides(repository: tuple[Path, str]) -> None:
    repo, base = repository
    (repo / "frontend/src").mkdir(parents=True, exist_ok=True)
    (repo / "backend/src/runtime.py").rename(repo / "frontend/src/runtime.ts")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Move across surfaces")
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "full"


@pytest.mark.parametrize(
    "name", ["backend/openapi.json", "frontend/src/types/generated/api.d.ts"]
)
def test_deleted_contract_artifacts_retain_regeneration_gate(
    repository: tuple[Path, str], name: str
) -> None:
    repo, base = repository
    (repo / name).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Remove shared contract")
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    assert outputs["profile"] == "full"
    assert outputs["backend"] == "true"


def test_large_diff_and_unusual_names_do_not_hide_runtime_changes(
    repository: tuple[Path, str],
) -> None:
    repo, base = repository
    docs = repo / "docs"
    docs.mkdir()
    for number in range(310):
        (docs / f"plan-{number}.md").write_text("Docs\n")
    (repo / "backend/src/runtime with\nnewline.py").write_text("value = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Large diff")
    result = _select(repo, base)
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    assert outputs["profile"] == "backend"
    assert outputs["changed_files"] == 311


def test_current_base_changes_are_not_charged_to_a_docs_pr(
    repository: tuple[Path, str],
) -> None:
    repo, _ = repository
    (repo / "README.md").write_text("Docs change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Docs")
    _git(repo, "checkout", "develop")
    (repo / "backend/src/runtime.py").write_text("value = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Unrelated base change")
    _git(repo, "checkout", "feature/test")
    result = _select(repo, "develop")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile"] == "docs"


def test_github_outputs_are_complete_and_ref_options_are_rejected(
    repository: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = repository
    (repo / "README.md").write_text("Docs change\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Docs")
    output_file = tmp_path / "outputs"
    result = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--event-name",
            "pull_request",
            "--base",
            base,
            "--output-file",
            str(output_file),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output_file.read_text().splitlines() == [
        "profile=docs",
        "backend=false",
        "frontend=false",
        "integration=false",
        "e2e=false",
    ]
    assert _select(repo, "--help").returncode != 0


def test_absent_head_or_merge_base_blocks_detection(
    repository: tuple[Path, str],
) -> None:
    repo, _ = repository
    result = subprocess.run(
        [sys.executable, str(SELECTOR), "--event-name", "pull_request"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "commit", "-m", "Unrelated root")
    assert _select(repo, "develop").returncode != 0
    result = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--event-name",
            "pull_request",
            "--base",
            "develop",
            "--head",
            "absent",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
