"""Contracts for scripts/verify/boot_local.sh (nous-verify local target)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "verify" / "boot_local.sh"


def test_boot_local_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_boot_local_uses_the_backend_venv_offline_frontend_and_health_polls() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"$BACKEND_PY" -m uvicorn src.main:app' in text
    assert "pnpm --dir frontend dev:offline" in text
    assert "/health/readiness" in text
    assert ".verify-artifacts/boot" in text
    assert "--git-common-dir" in text  # worktrees fall back to the main venv
    assert "/Users/" not in text  # no machine-specific paths


def test_boot_local_stops_only_the_processes_it_started() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "pkill" not in text and "killall" not in text
    assert 'kill -TERM -- "-$pid"' in text  # its own process group (set -m)
    assert 'kill "$pid"' in text


def test_boot_local_usage_exits_2_without_a_command() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "start|stop|status" in result.stderr


def test_boot_local_status_reports_stopped_without_pid_files(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "status"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "NOUS_VERIFY_BOOT_DIR": str(tmp_path)},
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "backend: stopped" in result.stdout
    assert "frontend: stopped" in result.stdout


def test_boot_local_refuses_ports_that_already_answer() -> None:
    # Health polls must prove *this* script's processes, not a server that
    # was already listening on the port.
    text = SCRIPT.read_text(encoding="utf-8")
    assert "already answering" in text
    assert "port_in_use" in text
    assert "died" in text
