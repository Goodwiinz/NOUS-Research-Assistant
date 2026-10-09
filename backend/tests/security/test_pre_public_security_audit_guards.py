import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_sensitive_terraform_and_local_secret_artifacts_are_ignored() -> None:
    gitignore = _read(".gitignore")

    required_patterns = [
        "*.tfstate",
        "*.tfstate.*",
        "*.tfvars",
        "!*.tfvars.example",
        ".terraform/",
        "**/.terraform/",
        ".claude/",
        "**/.claude/",
        ".mcp.json",
        ".opencode/",
        ".serena/",
        ".goodflows/",
        ".brv/",
    ]

    missing = [pattern for pattern in required_patterns if pattern not in gitignore]
    assert missing == []


def test_sensitive_terraform_and_local_secret_artifacts_are_not_tracked() -> None:
    tracked_files = subprocess.check_output(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True
    ).splitlines()

    forbidden_patterns = [
        re.compile(r"(^|/)terraform\.tfstate(\.|$)"),
        re.compile(r"(^|/)terraform\.tfvars$"),
        re.compile(r"(^|/)\.terraform/"),
        re.compile(r"(^|/)\.claude/"),
        re.compile(r"(^|/)\.mcp\.json$"),
        re.compile(r"(^|/)\.opencode/"),
        re.compile(r"(^|/)\.serena/"),
        re.compile(r"(^|/)\.goodflows/"),
        re.compile(r"(^|/)\.brv/"),
    ]

    # Deliberately-tracked exceptions to the blanket rules above. Every entry
    # must be an exact path, sanitized (no credentials, no user-specific
    # paths), and carry its own narrow .gitignore negation. Anything else
    # under these directories stays forbidden.
    allowlisted_tracked = {
        # /nous-loop self-improvement command — workflow doc only; restored
        # after the #969 purge with a scoped !.claude/commands/nous-loop.md
        # gitignore exception.
        ".claude/commands/nous-loop.md",
        # /nous-verify user-level verification command (nous-loop step 7b) —
        # workflow doc only, with its own scoped
        # !.claude/commands/nous-verify.md gitignore exception.
        ".claude/commands/nous-verify.md",
    }

    forbidden_tracked = [
        path
        for path in tracked_files
        if path not in allowlisted_tracked
        and any(pattern.search(path) for pattern in forbidden_patterns)
    ]
    assert forbidden_tracked == []


def test_static_kubernetes_secret_manifests_are_not_tracked() -> None:
    tracked_files = subprocess.check_output(
        ["git", "ls-files", "infrastructure/kubernetes"], cwd=REPO_ROOT, text=True
    ).splitlines()

    static_secret_manifests = []
    for path in tracked_files:
        candidate = REPO_ROOT / path
        if candidate.suffix not in {".yaml", ".yml"}:
            continue
        content = candidate.read_text(encoding="utf-8")
        if re.search(r"(?m)^kind:\s*Secret\s*$", content) and re.search(
            r"(?m)^data:\s*$", content
        ):
            static_secret_manifests.append(path)

    assert static_secret_manifests == []


def test_gitleaks_config_covers_pre_public_critical_secret_classes() -> None:
    config_path = REPO_ROOT / ".gitleaks.toml"
    assert config_path.exists()

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    rule_ids = {rule["id"] for rule in config["rules"]}

    assert {
        "digitalocean-api-token",
        "digitalocean-spaces-credential",
        "avns-database-password",
        "kubernetes-admin-credential",
    }.issubset(rule_ids)


def test_pre_commit_runs_gitleaks_against_staged_changes() -> None:
    pre_commit_config = _read(".pre-commit-config.yaml")

    assert "gitleaks" in pre_commit_config
    assert "git" in pre_commit_config
    assert "--staged" in pre_commit_config
    assert ".gitleaks.toml" in pre_commit_config


def test_ci_runs_secret_scan() -> None:
    workflow_path = REPO_ROOT / ".github/workflows/secret-scan.yml"
    assert workflow_path.exists()

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "gitleaks" in workflow
    assert "pull_request" in workflow
    assert ".gitleaks.toml" in workflow
