"""Source-integrity boundaries without provider calls or a live database."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
PROBE = ROOT / "evals/discovery-provider-smoke-v1/probe.py"
UNLISTED = "backend/src/models/document.py"


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return process.stdout.strip()


def commit(root: Path) -> None:
    git(
        root,
        "-c",
        "user.name=Source identity fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "Freeze synthetic source inputs",
    )


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("provider_source_identity", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in module.SOURCE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    target = tmp_path / UNLISTED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("MODEL = 1\n")
    (tmp_path / ".gitignore").write_text("evals/jobs/\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "--", *module.SOURCE_FILES, UNLISTED, ".gitignore")
    commit(tmp_path)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module, "FIXTURE", tmp_path / "evals/discovery-provider-smoke-v1/fixture.json"
    )
    monkeypatch.setattr(
        module.importlib.metadata, "version", lambda _name: "fixture-version"
    )
    return module


def test_clean_checkout_binds_complete_tree(probe: ModuleType) -> None:
    identity = probe.identity()
    assert identity["source_dirty"] is False
    assert identity["source_tree"] == git(probe.ROOT, "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize("change", ["listed", "unlisted", "staged", "untracked"])
def test_undeclared_git_visible_changes_are_rejected(
    probe: ModuleType, change: str
) -> None:
    name = (
        probe.SOURCE_FILES[0]
        if change == "listed"
        else "untracked.py" if change == "untracked" else UNLISTED
    )
    (probe.ROOT / name).write_text("UNDECLARED = 2\n")
    if change == "staged":
        git(probe.ROOT, "add", "--", name)
    with pytest.raises(probe.SourceIdentityError, match="clean Git-visible checkout"):
        probe.identity()


def test_ignored_raw_artifacts_do_not_change_source_identity(probe: ModuleType) -> None:
    before = probe.identity()
    raw = probe.ROOT / "evals/jobs/raw.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    assert probe.identity() == before


def test_clean_but_different_commit_refuses_execution_before_database_access(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy

    declaration = {"identity": probe.identity()}
    (probe.ROOT / UNLISTED).write_text("NEW_COMMITTED_INPUT = 2\n")
    git(probe.ROOT, "add", "--", UNLISTED)
    commit(probe.ROOT)
    called: list[bool] = []
    monkeypatch.setattr(
        sqlalchemy, "create_engine", lambda *_args, **_kwargs: called.append(True)
    )
    with pytest.raises(
        probe.SourceIdentityError, match="Inputs changed after declaration"
    ):
        probe.run(declaration, probe.ROOT)
    assert called == []


@pytest.mark.parametrize("commit_change", [False, True], ids=["dirty", "new-head"])
def test_mid_run_source_change_invalidates_measurement_after_cleanup(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, commit_change: bool
) -> None:
    import sqlalchemy

    statements: list[str] = []

    class Admin:
        def connect(self) -> Admin:
            return self

        def __enter__(self) -> Admin:
            return self

        def __exit__(self, _type: Any, _error: Any, _traceback: Any) -> None:
            return None

        def exec_driver_sql(self, statement: str) -> None:
            statements.append(statement)

        def dispose(self) -> None:
            return None

    def engine(*_args: Any, **_kwargs: Any) -> Admin:
        return Admin()

    original_run = subprocess.run

    def command(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args[1:4] == ["-m", "alembic", "upgrade"]:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return original_run(args, **kwargs)

    async def exercise(
        _fixture: dict[str, Any], _progress: dict[str, Any]
    ) -> dict[str, Any]:
        (probe.ROOT / UNLISTED).write_text("CHANGED_DURING_RUN = 3\n")
        if commit_change:
            git(probe.ROOT, "add", "--", UNLISTED)
            commit(probe.ROOT)
        return {"status": "completed"}

    declaration = {"identity": probe.identity()}
    for key in (
        "DATABASE_URL",
        "SUPABASE_DB_URL",
        "ENVIRONMENT",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    ):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setenv(
        "NOUS_PROVIDER_PROOF_DATABASE_URL", "postgresql://postgres@127.0.0.1/proof"
    )
    monkeypatch.setattr(sqlalchemy, "create_engine", engine)
    monkeypatch.setattr(probe.subprocess, "run", command)
    monkeypatch.setattr(probe, "exercise", exercise)
    artifacts = probe.ROOT / "evals/jobs/run"
    artifacts.mkdir(parents=True)
    result = probe.run(declaration, artifacts)
    assert result["status"] == "infrastructure"
    assert result["error_type"] == "SourceIdentityError"
    assert result["source_integrity"] == "failed"
    assert result["scratch_database_removed"] is True
    assert any(sql.startswith("DROP DATABASE") for sql in statements)
