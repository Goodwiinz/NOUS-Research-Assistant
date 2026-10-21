"""Host verification does not trust state or file links from the agent."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "evals/verify_discovery_trial.py"
PASS = ROOT / "evals/harness-mcp-discovery-v1/tests/calibration/pass.json"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("real-mock", 0),
        ("forged-main-state", 2),
        ("main-container", 2),
        ("foreign-project", 2),
        ("malformed-private-state", 2),
        ("answer-symlink", 10),
    ],
)
def test_authoritative_state_and_answer_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected: int,
) -> None:
    spec = importlib.util.spec_from_file_location("host_discovery_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    trial = json.loads(PASS.read_text())
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    answer = artifacts / "answer.md"
    secret = tmp_path / "private.txt"
    secret.write_text("private-file-must-never-be-read")
    if scenario == "answer-symlink":
        answer.symlink_to(secret)
    else:
        answer.write_text(trial["evidence"]["answer"])
    container = "a" * 64
    metadata = {
        "Id": container,
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "com.docker.compose.service": (
                    "main" if scenario == "main-container" else "mock-nous"
                ),
                "com.docker.compose.project": (
                    "foreign" if scenario == "foreign-project" else "registered"
                ),
            }
        },
    }
    calls: list[list[str]] = []

    def docker(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1] == "inspect":
            payload: Any = [metadata]
        else:
            assert args[1:4] == ["exec", "--user", "10001"]
            assert "http://127.0.0.1:8080/events" in args[-1]
            payload = (
                []
                if scenario == "forged-main-state"
                else (
                    {}
                    if scenario == "malformed-private-state"
                    else trial["state"]["events"]
                )
            )
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(module.subprocess, "run", docker)
    result = module.evaluate(container, "registered", artifacts)
    assert result["exit_code"] == expected, result
    assert result["passed"] is (expected == 0)
    assert secret.read_text() not in json.dumps(result)
    if scenario in ("main-container", "foreign-project"):
        assert len(calls) == 1
