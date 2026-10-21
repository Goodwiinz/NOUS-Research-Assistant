"""Source-integrity boundaries without provider calls or a live database."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

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

    declaration = {"declaration_id": uuid4().hex, "identity": probe.identity()}
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


@pytest.mark.parametrize("change", ["dirty", "new-head", "git-unavailable"])
def test_mid_run_source_change_invalidates_measurement_after_cleanup(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, change: str
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
        if change == "git-unavailable":

            def unavailable() -> dict[str, Any]:
                raise subprocess.CalledProcessError(128, ["git", "status"])

            monkeypatch.setattr(probe, "identity", unavailable)
        else:
            (probe.ROOT / UNLISTED).write_text("CHANGED_DURING_RUN = 3\n")
        if change == "new-head":
            git(probe.ROOT, "add", "--", UNLISTED)
            commit(probe.ROOT)
        return {"status": "completed"}

    declaration = {"declaration_id": uuid4().hex, "identity": probe.identity()}
    for key in (
        "DATABASE_URL",
        "SUPABASE_DB_URL",
        "ENVIRONMENT",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    ):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.syspath_prepend(str(probe.ROOT / "backend"))
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
    assert result["error_type"] == (
        "CalledProcessError" if change == "git-unavailable" else "SourceIdentityError"
    )
    assert result["source_integrity"] == (
        "unavailable" if change == "git-unavailable" else "failed"
    )
    assert result["scratch_database_removed"] is True
    assert any(sql.startswith("DROP DATABASE") for sql in statements)


@pytest.mark.parametrize("action", ["declare", "run"])
def test_cli_source_errors_exit_as_infrastructure(
    probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    artifacts = probe.ROOT / "evals/jobs/cli"
    artifacts.mkdir(parents=True)
    declaration = artifacts / "declaration.json"
    declaration.write_text(
        json.dumps({"declaration_id": uuid4().hex, "identity": probe.identity()})
    )
    output = artifacts / "result.json"
    (probe.ROOT / UNLISTED).write_text("UNDECLARED_INPUT = 4\n")
    args = (
        ["probe", "--declare", str(output)]
        if action == "declare"
        else ["probe", "--run", str(declaration), "--output", str(output)]
    )
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as raised:
        probe.main()
    assert raised.value.code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "infrastructure"
    assert error["error_type"] == "SourceIdentityError"
    assert not output.exists()


@pytest.mark.parametrize("outcome", ["completed", "infrastructure"])
def test_declaration_is_consumed_even_after_execution_failure(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    declaration = {"declaration_id": uuid4().hex, "identity": probe.identity()}
    calls: list[bool] = []

    def execute(_declaration: dict[str, Any], _artifacts: Path) -> dict[str, Any]:
        calls.append(True)
        return {"status": outcome}

    monkeypatch.setattr(probe, "_run_once", execute)
    assert probe.run(declaration, probe.ROOT)["status"] == outcome
    with pytest.raises(probe.SourceIdentityError, match="already consumed"):
        probe.run(declaration, probe.ROOT / "another-output")
    assert calls == [True]
    claim = (
        probe.ROOT
        / "evals/jobs/provider-attempt-claims"
        / (declaration["declaration_id"] + ".jsonl")
    )
    lifecycle = [json.loads(line) for line in claim.read_text().splitlines()]
    assert lifecycle[0]["status"] == "claimed"
    assert lifecycle[1]["terminal"]["status"] == outcome


def test_concurrent_declaration_claim_allows_one_execution(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = {"declaration_id": uuid4().hex, "identity": probe.identity()}
    entered, release = Event(), Event()
    calls: list[bool] = []

    def execute(_declaration: dict[str, Any], _artifacts: Path) -> dict[str, Any]:
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=10)
        return {"status": "completed"}

    monkeypatch.setattr(probe, "_run_once", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(probe.run, declaration, probe.ROOT)
        try:
            assert entered.wait(timeout=10)
            second = pool.submit(probe.run, declaration, probe.ROOT)
            with pytest.raises(probe.SourceIdentityError, match="already consumed"):
                second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "completed"
    assert calls == [True]


def test_claim_retains_an_exception_and_cannot_be_retried(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = {"declaration_id": uuid4().hex, "identity": probe.identity()}

    def failed(_declaration: dict[str, Any], _artifacts: Path) -> dict[str, Any]:
        raise RuntimeError("Synthetic interrupted provider attempt")

    monkeypatch.setattr(probe, "_run_once", failed)
    with pytest.raises(RuntimeError, match="Synthetic interrupted"):
        probe.run(declaration, probe.ROOT)
    with pytest.raises(probe.SourceIdentityError, match="already consumed"):
        probe.run(declaration, probe.ROOT)
    claim = (
        probe.ROOT
        / "evals/jobs/provider-attempt-claims"
        / (declaration["declaration_id"] + ".jsonl")
    )
    terminal = json.loads(claim.read_text().splitlines()[-1])["terminal"]
    assert terminal == {"status": "infrastructure", "error_type": "RuntimeError"}


def test_unlisted_installed_distribution_changes_identity(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = probe.identity()
    distributions = list(probe.importlib.metadata.distributions())
    distributions.append(
        SimpleNamespace(metadata={"Name": "synthetic-runtime-driver"}, version="2")
    )
    monkeypatch.setattr(
        probe.importlib.metadata, "distributions", lambda: distributions
    )
    assert probe.identity() != before


def test_prerequisites_survive_optimized_python() -> None:
    assert not any(
        isinstance(node, ast.Assert) for node in ast.walk(ast.parse(PROBE.read_text()))
    )
    script = (
        "import importlib.util; "
        f"s=importlib.util.spec_from_file_location('optimized_probe',{str(PROBE)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.require(False, 'Disabled provider must refuse')"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "ProofPrerequisiteError: Disabled provider must refuse" in result.stderr
