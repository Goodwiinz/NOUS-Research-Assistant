"""Static contracts for the repository-native NOUS improvement workflow.

Agent behavior is pressure-tested separately.  These tests guard the small,
machine-checkable part of the contract: one canonical document, thin runtime
adapters, portable language, and explicit terminal outcomes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_PATH = REPO_ROOT / "docs" / "engineering" / "nous-loop.md"
CLAUDE_ADAPTER_PATH = REPO_ROOT / ".claude" / "commands" / "nous-loop.md"
AGENT_ADAPTER_PATH = REPO_ROOT / "AGENTS.md"


def _canonical_text() -> str:
    return CANONICAL_PATH.read_text(encoding="utf-8")


def _local_ci_text() -> str:
    return (REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh").read_text(
        encoding="utf-8"
    )


def test_repository_native_workflow_is_canonical_and_discoverable() -> None:
    workflow = _canonical_text()
    engineering_index = (REPO_ROOT / "docs" / "engineering" / "README.md").read_text(
        encoding="utf-8"
    )

    assert workflow.startswith("# NOUS self-improvement loop\n")
    assert "[nous-loop.md](nous-loop.md)" in engineering_index


@pytest.mark.parametrize("adapter_path", [CLAUDE_ADAPTER_PATH, AGENT_ADAPTER_PATH])
def test_runtime_entrypoints_are_thin_adapters_to_the_canonical_workflow(
    adapter_path: Path,
) -> None:
    adapter = adapter_path.read_text(encoding="utf-8")

    assert "docs/engineering/nous-loop.md" in adapter
    assert len(adapter.splitlines()) <= 60
    assert re.search(
        r"read .*docs/engineering/nous-loop\.md.* completely", adapter, re.I
    )
    assert re.search(r"canonical", adapter, re.I)
    assert re.search(r"(?:may|must) not weaken|cannot weaken", adapter, re.I)


def test_canonical_workflow_is_runtime_neutral_and_has_no_stale_incidents() -> None:
    workflow = _canonical_text()
    runtime_specific_terms = {
        "Claude",
        "Codex",
        "Fable",
        "Opus",
        "ScheduleWakeup",
        "pr-review-toolkit",
    }

    for term in runtime_specific_terms:
        assert not re.search(rf"\b{re.escape(term)}\b", workflow, re.I)
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", workflow)
    assert "/home/" not in workflow
    assert "GitHub Actions has been dead" not in workflow


def test_canonical_workflow_requires_live_preflight_and_honest_outcomes() -> None:
    workflow = _canonical_text()
    normalized = " ".join(workflow.split())

    assert "## Live preflight" in workflow
    completed_outcomes = re.findall(r"^- `([a-z-]+)` —", workflow, re.MULTILINE)
    assert completed_outcomes == ["merged", "ready-for-human", "dry"]
    assert (
        "user cancellation stops the workflow outside this outcome protocol"
        in normalized
    )
    assert "Never report a stronger outcome" in workflow


def test_canonical_workflow_enforces_core_evidence_gates() -> None:
    workflow = _canonical_text()
    normalized = " ".join(workflow.split())

    required_clauses = [
        "current task authorizes each remote mutation",
        "claim before editing",
        "Heartbeat before the claim expires",
        "separate review context that did not implement the change",
        "Capture its expected failure before changing production code",
        "repeat independent review whenever the diff changes",
        "repeat security review when applicable",
        "LOOP_BRIDGE_DIR",
        "shared and writable",
    ]

    for clause in required_clauses:
        assert clause in normalized


def test_canonical_workflow_bounds_review_and_gates_human_only_changes() -> None:
    workflow = _canonical_text()
    normalized = " ".join(workflow.split())

    required_clauses = [
        "VERDICT: PASS",
        "VERDICT: CHANGES",
        "HEAD: <full 40-character head SHA reviewed>",
        "BASE: <full 40-character base SHA reviewed>",
        "When round 3 ends with any `VERDICT: CHANGES`",
        "If a fourth round would be needed for any reason",
        "write `handoff.md` in the review directory",
        "An open PR whose review directory (step 6) contains `handoff.md` belongs to a human",
        "### 6b. Check for human-gated changes",
        "it ends as `ready-for-human` instead of merging",
        "A tick must never narrow the checks that judge it",
        "required review whose last `HEAD:` line is that head, no step 6b match",
        "A local claim expires 45 minutes after its last claim or heartbeat",
        "python3 scripts/loop_bridge.py heartbeat --branch",
    ]

    for clause in required_clauses:
        assert clause in normalized, clause

    human_gate = workflow[
        workflow.index("### 6b.") : workflow.index("### 7. Run local gates")
    ]
    for gated_path in [
        "backend/alembic/versions/",
        "supabase/migrations/",
        "pnpm-lock.yaml",
        ".github/workflows/",
        "scripts/ci/",
        "docs/engineering/feature-map.yaml",
        "scripts/loop_bridge.py",
        "scripts/verify/",
        "tests/e2e/qa/",
        "api-breaking-approved",
    ]:
        assert gated_path in human_gate, gated_path


def test_workflow_contract_tests_run_in_blocking_local_and_hosted_gates() -> None:
    local_ci = _local_ci_text()
    hosted_ci = (REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml").read_text(
        encoding="utf-8"
    )

    for ci_config in (local_ci, hosted_ci):
        command_start = ci_config.index("pytest tests/unit/scripts/")
        command = ci_config[command_start : command_start + 200]
        assert "--no-cov" in command

    nous_gate_start = local_ci.index('  step "NOUS workflow contract tests (blocking)"')
    backend_gate_start = local_ci.index('  step "Unit tests (blocking)"')
    nous_gate = local_ci[nous_gate_start:backend_gate_start]
    backend_gate = local_ci[backend_gate_start:]

    assert '"$PY" -m pytest tests/unit/scripts/' in nous_gate
    assert "cd backend" not in nous_gate
    assert re.search(
        r'\(\s*cd backend\s*&&\s*"\$PY"\s+-m\s+pytest\s+tests/\s+-c\s+pytest\.ini\s+\\\s*'
        r'-m\s+"unit or not \(integration or e2e or slow\)"\s+\\\s*'
        r"-q\s+-p\s+no:cacheprovider\s+--no-cov\s*\)\s*"
        r'check\s+\$\?\s+"pytest"',
        backend_gate,
    )


def test_local_ci_normalizes_python_before_any_directory_change() -> None:
    local_ci = _local_ci_text()
    python_assignment = local_ci.index('PY="${PYTHON:-python3}"')
    root_change = local_ci.index('cd "$ROOT"')
    normalization = local_ci[python_assignment:root_change]

    assert python_assignment < root_change
    assert re.search(
        r'case\s+"\$PY"\s+in\s+\*/\*\)\s+'
        r'case\s+"\$PY"\s+in\s+/\*\)\s*;;\s*\*\)\s*'
        r'PY="\$ROOT/\$PY"\s*;;\s*esac\s*;;\s*esac',
        normalization,
    )


@pytest.mark.parametrize(
    ("configured_python", "expected_python"),
    [
        (".venv/bin/python", str(REPO_ROOT / ".venv" / "bin" / "python")),
        (
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            str(REPO_ROOT / ".venv" / "bin" / "python"),
        ),
        ("python3", "python3"),
    ],
)
def test_configured_python_resolution_is_stable_across_local_ci_gates(
    configured_python: str, expected_python: str
) -> None:
    bootstrap_end = _local_ci_text().index("FAILED=()")
    bootstrap = _local_ci_text()[:bootstrap_end]
    probe = (
        f"{bootstrap}\n"
        "printf 'root=%s\\n' \"$PY\"\n"
        "cd backend\n"
        "printf 'backend=%s\\n' \"$PY\"\n"
    )
    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHON": configured_python},
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.splitlines()[-2:] == [
        f"root={expected_python}",
        f"backend={expected_python}",
    ]


def test_local_ci_executes_configured_python_from_space_containing_root(
    tmp_path: Path,
) -> None:
    """A root-relative interpreter remains executable after normalization."""

    fake_root = tmp_path / "repository root with spaces"
    command_dir = tmp_path / "commands"
    interpreter = fake_root / ".venv" / "bin" / "python"
    log_path = tmp_path / "python-invocations.log"
    (fake_root / "backend").mkdir(parents=True)
    (fake_root / "frontend").mkdir()
    interpreter.parent.mkdir(parents=True)
    (fake_root / "node_modules" / ".bin").mkdir(parents=True)
    command_dir.mkdir()
    for command in ("bash", "mktemp", "rm", "sort", "tail"):
        executable = shutil.which(command)
        assert executable is not None
        (command_dir / command).symlink_to(executable)

    def write_executable(path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    write_executable(
        interpreter,
        '#!/usr/bin/env bash\nprintf \'%s|%s\\n\' "$PWD" "$*" >> "$CI_PY_LOG"\n',
    )
    write_executable(
        fake_root / "node_modules" / ".bin" / "openapi-typescript",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    write_executable(
        command_dir / "git",
        """#!/usr/bin/env bash
case "$1" in
  rev-parse) printf '%s\\n' "$FAKE_ROOT" ;;
  merge-base) printf 'base\\n' ;;
  diff) printf 'backend/alembic/versions/fixture.py\\n' ;;
esac
""",
    )
    for command in ("ruff", "black", "isort", "mypy", "node", "pnpm"):
        write_executable(command_dir / command, "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        command_dir / "docker",
        """#!/usr/bin/env bash
case "$1" in
  info) exit 0 ;;
  run) exit 0 ;;
esac
exit 1
""",
    )

    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh"),
            "--base",
            "origin/develop",
            "--frontend",
        ],
        cwd=fake_root,
        env={
            **os.environ,
            "CI_PY_LOG": str(log_path),
            "FAKE_ROOT": str(fake_root),
            "PATH": str(command_dir),
            "PYTHON": ".venv/bin/python",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = log_path.read_text(encoding="utf-8").splitlines()
    assert all(str(fake_root) in invocation for invocation in invocations)
    for argument in (
        "scripts/docs/check_dir_docs.py",
        "scripts/ci/changed_source_files.py --base origin/develop --kind python",
        "scripts/ci/changed_source_files.py --base origin/develop --kind python-added",
        "scripts/ci/generate_openapi.py --check",
        "../scripts/ci/check_alembic.py",
        "import socket,sys;",
        "import socket; s=socket.socket();",
        "tests/unit/scripts/ --confcutdir=tests/unit/scripts",
        "tests/ -c pytest.ini",
        "scripts/ci/check_tsconfig_exclusions.py --base origin/develop",
    ):
        assert any(argument in invocation for invocation in invocations), invocations
