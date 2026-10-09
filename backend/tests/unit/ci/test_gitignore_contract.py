"""Repository ignore rules must cover generated dependency links."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_nested_virtualenv_symlink_is_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    (repo / ".gitignore").write_text(
        (REPO_ROOT / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    backend = repo / "backend"
    backend.mkdir()
    (backend / ".gitignore").write_text(
        (REPO_ROOT / "backend" / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    target = tmp_path / "venv-target"
    target.mkdir()
    (backend / ".venv").symlink_to(target, target_is_directory=True)

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "backend/.venv"],
        cwd=repo,
        check=False,
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        ".verify-artifacts/run/trace.zip",
        ".verify-artifacts/run/checkpoints/smoke.login-availability--login.form.png",
        ".nous-qa-reports/x/nous-qa-report.json",
    ],
)
def test_verification_binaries_are_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, path
