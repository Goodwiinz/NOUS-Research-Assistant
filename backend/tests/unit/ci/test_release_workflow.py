"""Contracts for exact-source image builds and protected-branch GitOps PRs."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DOCKER_PATH = WORKFLOWS / "docker-build.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1 and may deserialize the unquoted ``on`` key as True.
    return cast("dict[str, Any]", workflow.get("on") or workflow.get(True))  # type: ignore[call-overload]


def _steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", workflow["jobs"][job_name]["steps"])


def _step_with_run(
    workflow: dict[str, Any], job_name: str, fragment: str
) -> dict[str, Any]:
    for step in _steps(workflow, job_name):
        if fragment in str(step.get("run", "")):
            return step
    raise AssertionError(f"{job_name!r} has no step running {fragment!r}")


def _step_with_action(
    workflow: dict[str, Any], job_name: str, action: str
) -> dict[str, Any]:
    for step in _steps(workflow, job_name):
        if str(step.get("uses", "")).startswith(action):
            return step
    raise AssertionError(f"{job_name!r} has no step using {action!r}")


def _condition_value(node: ast.AST, context: dict[str, Any]) -> Any:
    """Evaluate the release condition's Boolean/equality subset without eval.

    This uses the actual workflow expression and rejects unsupported syntax;
    the independent acceptance cases below define the release authority.
    """
    if isinstance(node, ast.Expression):
        return _condition_value(node.body, context)
    if isinstance(node, ast.BoolOp):
        values = [_condition_value(value, context) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
    ):
        return _condition_value(node.left, context) == _condition_value(
            node.comparators[0], context
        )
    if isinstance(node, ast.Attribute):
        return _condition_value(node.value, context)[node.attr]
    if isinstance(node, ast.Name):
        return context[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    raise AssertionError(f"Unsupported release condition syntax: {ast.dump(node)}")


@pytest.mark.parametrize(
    ("event", "conclusion", "branch", "repository", "expected"),
    [
        ("push", "success", "develop", "owner/repo", True),
        ("pull_request", "success", "develop", "owner/repo", False),
        ("workflow_dispatch", "success", "develop", "owner/repo", False),
        ("push", "failure", "develop", "owner/repo", False),
        ("push", "cancelled", "develop", "owner/repo", False),
        ("push", "success", "main", "owner/repo", False),
        ("push", "success", "develop", "fork/repo", False),
    ],
)
def test_release_requires_successful_protected_push(
    event: str, conclusion: str, branch: str, repository: str, expected: bool
) -> None:
    release = _load_workflow(WORKFLOWS / "release-dev.yml")
    condition = release["jobs"]["prepare"]["if"]
    parsed = ast.parse(
        condition.replace("&&", " and ").replace("||", " or "), mode="eval"
    )
    context = {
        "github": {
            "repository": "owner/repo",
            "event": {
                "workflow_run": {
                    "event": event,
                    "conclusion": conclusion,
                    "head_branch": branch,
                    "head_repository": {"full_name": repository},
                }
            },
        }
    }
    assert _condition_value(parsed, context) is expected


def test_reusable_docker_build_checks_out_and_asserts_a_full_sha() -> None:
    docker = _load_workflow(DOCKER_PATH)
    triggers = _triggers(docker)
    source_input = triggers["workflow_call"]["inputs"]["source_sha"]
    identity = _step_with_run(docker, "build-backend", "40-character commit SHA")
    checkout = _step_with_action(docker, "build-backend", "actions/checkout@")
    assertion = _step_with_run(docker, "build-backend", "git rev-parse HEAD")

    assert source_input["required"] is True
    assert source_input["type"] == "string"
    assert "[0-9a-f]{40}" in str(identity["run"])
    assert checkout["with"]["ref"] == "${{ steps.identity.outputs.source_sha }}"
    assert checkout["with"]["fetch-depth"] == 1
    assert checkout["with"]["persist-credentials"] is False
    assert assertion["env"]["SOURCE_SHA"] == (
        "${{ steps.identity.outputs.source_sha }}"
    )
    assert '[ "$ACTUAL_SHA" != "$SOURCE_SHA" ]' in str(assertion["run"])


def test_docker_outputs_full_sha_trace_tag_and_immutable_digest() -> None:
    docker = _load_workflow(DOCKER_PATH)
    call_outputs = _triggers(docker)["workflow_call"]["outputs"]
    job = docker["jobs"]["build-backend"]
    identity = _step_with_run(
        docker, "build-backend", 'IMAGE_TAG="$REGISTRY/nous/backend'
    )
    build = _step_with_action(docker, "build-backend", "docker/build-push-action@")
    verify = _step_with_run(docker, "build-backend", "valid registry digest")

    assert call_outputs["source_sha"]["value"] == (
        "${{ jobs.build-backend.outputs.source_sha }}"
    )
    assert call_outputs["image_tag"]["value"] == (
        "${{ jobs.build-backend.outputs.image_tag }}"
    )
    assert call_outputs["digest"]["value"] == (
        "${{ jobs.build-backend.outputs.digest }}"
    )
    assert job["outputs"]["digest"] == "${{ steps.verify-image.outputs.digest }}"
    assert 'IMAGE_TAG="$REGISTRY/nous/backend:$SOURCE_SHA"' in str(identity["run"])
    assert build["with"]["tags"] == "${{ steps.identity.outputs.image_tag }}"
    assert "GIT_SHA=${{ steps.identity.outputs.source_sha }}" in str(
        build["with"]["build-args"]
    )
    assert ":latest" not in str(build["with"]["tags"])
    assert "cache-from" not in build["with"]
    assert "cache-to" not in build["with"]
    assert "sha256:[0-9a-f]{64}" in str(verify["run"])
    assert 'echo "digest=$DIGEST" >> "$GITHUB_OUTPUT"' in str(verify["run"])


def test_dev_release_uses_builtin_token_and_preserves_branch_checks() -> None:
    release = _load_workflow(WORKFLOWS / "release-dev.yml")
    promote = release["jobs"]["promote"]
    proposal = _step_with_run(release, "promote", "gh pr create")
    shell = str(proposal["run"])

    assert proposal["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert proposal["env"]["PR_TOKEN"] == "${{ secrets.RELEASE_PR_TOKEN }}"
    assert 'GH_TOKEN="${PR_TOKEN:-$GH_TOKEN}" gh pr create' in shell
    assert 'if [ -z "$PR_TOKEN" ]; then' in shell
    assert (
        "::warning::RELEASE_PR_TOKEN is not set: approve this PR's workflow "
        "runs or it cannot merge."
    ) in shell
    assert promote["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert "create-github-app-token" not in str(release)
    assert "CLAUDE_APP" not in str(release)
    assert not re.search(r"git\s+push[^\n]*(?:HEAD:|origin\s+)develop", shell)
    assert "[skip ci]" not in shell
    assert "gh workflow run" not in shell
    assert "gh pr list --base develop --state open --limit 100" in shell
    assert 'startswith("codex/release-dev-")' in shell
    assert '[ "$head" != "$BRANCH" ]' in shell
    assert 'gh pr close "$number" --delete-branch --comment' in shell
    assert (
        shell.index("develop moved; the newer source run owns promotion")
        < (shell.index('gh pr close "$number"'))
        < shell.index('gh pr list --base develop --head "$BRANCH"')
    )
    assert release["jobs"]["build"]["needs"] == "prepare"
    assert release["jobs"]["build"]["if"] == "needs.prepare.outputs.needed == 'true'"


def test_release_source_gate_skips_image_bumps_and_stale_runs(tmp_path: Path) -> None:
    """Execute the actual gate against Git; a values-only merge must not loop.

    Mutation-verified: replacing either guard at release-dev.yml:39 or :43
    with `if false` fails the corresponding assertion below. The proposal
    guard at test-pipeline.yml:59 is likewise mutation-verified. Run with:
    pytest --noconftest -c /dev/null backend/tests/unit/ci/test_release_workflow.py
    """
    remote = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repo)], check=True, capture_output=True
    )

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("checkout", "-b", "develop")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release-test@example.invalid")
    (repo / "source.txt").write_text("initial")
    git("add", ".")
    git("commit", "-m", "initial")
    (repo / "source.txt").write_text("new source")
    git("commit", "-am", "source change")
    git("push", "origin", "develop")
    source_sha = git("rev-parse", "HEAD")
    release = _load_workflow(WORKFLOWS / "release-dev.yml")
    step = _step_with_run(release, "prepare", "needed=true")
    output = tmp_path / "output"

    def needed(sha: str) -> bool:
        output.write_text("")
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", str(step["run"])],
            cwd=repo,
            env={**os.environ, "SOURCE_SHA": sha, "GITHUB_OUTPUT": str(output)},
            check=True,
            capture_output=True,
            text=True,
        )
        return "needed=true" in output.read_text()

    assert needed(source_sha), "A fresh source change must build"
    proposal_check = _step_with_run(
        _load_workflow(WORKFLOWS / "test-pipeline.yml"),
        "lint-backend",
        "Release proposal is stale",
    )

    def proposal_status(branch: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", str(proposal_check["run"])],
            cwd=repo,
            env={**os.environ, "RELEASE_BRANCH": branch},
            capture_output=True,
            text=True,
        )

    proposal_branch = f"codex/release-dev-{source_sha}"
    assert proposal_status(proposal_branch).returncode == 0
    values = repo / "infrastructure/helm/knowledge-graph-analytics/values-aws.yaml"
    values.parent.mkdir(parents=True)
    values.write_text("backend: {image: {tag: tested}}")
    git("add", ".")
    git("commit", "-m", "promote image")
    git("push", "origin", "develop")
    assert not needed(
        git("rev-parse", "HEAD")
    ), "Image promotion must not rebuild itself"
    stale = proposal_status(proposal_branch)
    assert stale.returncode != 0, "A stale release PR must fail a required check"
    assert "Release proposal is stale" in stale.stdout
    assert proposal_status("ordinary-feature").returncode == 0
    git("checkout", "--detach", source_sha)
    assert not needed(source_sha), "A stale pipeline must not propose an old image"


def test_release_protection_preflight_fails_closed() -> None:
    validator = REPO_ROOT / "scripts/ci/check_release_protection.py"
    passing = {
        "requiresStatusChecks": True,
        "requiresStrictStatusChecks": True,
        "requiredStatusChecks": [
            {"context": "Release Gate", "app": {"databaseId": 15368}}
        ],
    }
    cases = [
        (passing, 0),
        (None, 1),
        ({**passing, "requiresStatusChecks": False}, 1),
        ({**passing, "requiresStrictStatusChecks": False}, 0),
        ({**passing, "requiredStatusChecks": []}, 1),
        (
            {
                **passing,
                "requiredStatusChecks": [{"context": "Release Gate", "app": None}],
            },
            1,
        ),
    ]
    for payload, expected in cases:
        result = subprocess.run(
            ["python3", str(validator)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )
        assert result.returncode == expected, result.stderr
    malformed = subprocess.run(
        ["python3", str(validator)], input="not-json", text=True, capture_output=True
    )
    assert malformed.returncode == 1
    release = _load_workflow(WORKFLOWS / "release-dev.yml")
    for job in ["prepare", "promote"]:
        step = _step_with_run(release, job, "check_release_protection.py")
        assert "gh api graphql" in step["run"]
        assert (
            step["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_PR_TOKEN || github.token }}"
        )
    assert (
        _step_with_run(release, "prepare", "check_release_protection.py")["if"]
        == "steps.source.outputs.needed == 'true'"
    )
