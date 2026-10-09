"""Contracts for scripts/verify/boot_local.sh (nous-verify local target)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_boot_local_stop_signals_the_group_even_when_the_leader_is_gone(
    tmp_path: Path,
) -> None:
    # A leader (like pnpm) can exit while its child server keeps running in
    # the same process group; stop must still reach the child.
    leader = subprocess.Popen(
        ["bash", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    child = int(leader.stdout.readline().strip())
    try:
        os.kill(leader.pid, signal.SIGKILL)
        leader.wait(timeout=5)
        assert _alive(child)
        (tmp_path / "frontend.pid").write_text(f"{leader.pid}\n", encoding="utf-8")
        env = {
            **os.environ,
            "NOUS_VERIFY_BOOT_DIR": str(tmp_path),
            "NOUS_VERIFY_BACKEND_PORT": str(_free_port()),
            "NOUS_VERIFY_FRONTEND_PORT": str(_free_port()),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), "stop"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child)
        assert not (tmp_path / "frontend.pid").exists()
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


def test_boot_local_stop_waits_for_the_ports_to_free() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "wait_port_free" in text
