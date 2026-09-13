"""Exercise the real migration script against a stateful GitHub CLI double."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts/ci/migrate-develop-protection-to-ruleset.sh"
)
CLI = r"""
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
endpoint = next(a for a in args if a.startswith("repos/")).split("?")[0]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
def fail():
    print(json.dumps({"message": "API failed", "status": "403"}))
    sys.exit(1)
if method != "GET":
    state["writes"].append(method)
    path.write_text(json.dumps(state))
    if state.get("write_error"):
        fail()
    if method == "DELETE":
        state["classic"] = None
    else:
        state["ruleset"] = dict(json.load(sys.stdin), id=42)
    path.write_text(json.dumps(state))
    if method != "DELETE":
        print(json.dumps(state["ruleset"]))
elif endpoint.endswith("/protection"):
    if state.get("read_error"):
        fail()
    protection = {"url": "https://api.github.com/" + endpoint,
                  "enforce_admins": {"enabled": False},
                  "allow_force_pushes": {"enabled": False},
                  "allow_deletions": {"enabled": False}}
    if state["classic"] is not None or state.get("explicit_null"):
        protection["required_status_checks"] = state["classic"]
    print(json.dumps(protection))
elif endpoint.endswith("/required_status_checks"):
    if state.get("read_error") or state["classic"] is None:
        fail()
    print(json.dumps(state["classic"]))
elif endpoint.endswith("/rulesets"):
    rows = [state["ruleset"]] if state.get("ruleset") else []
    print(json.dumps([rows] if "--slurp" in args else rows))
elif endpoint.endswith("/rulesets/42"):
    value = state["ruleset"]
    if state.get("bad_readback") and state["writes"]:
        value["rules"] = []
    print(json.dumps(value))
else:
    raise AssertionError(args)
"""


def run(
    tmp_path: Path, state: dict[str, Any], apply: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    cli = tmp_path / "gh"
    cli.write_text(f"#!{sys.executable}\n" + CLI)
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), "4513412", *(["--apply"] if apply else [])],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_GH_STATE": str(state_path),
        },
        text=True,
        capture_output=True,
    )
    return result, json.loads(state_path.read_text())


def classic_state() -> dict[str, Any]:
    return {
        "classic": {
            "contexts": ["Custom check"],
            "strict": True,
            "checks": [{"context": "Custom check", "app_id": 987}],
        },
        "writes": [],
    }


def migrated_state() -> dict[str, Any]:
    return {
        "classic": None,
        "writes": [],
        "ruleset": {
            "id": 42,
            "name": "develop required checks",
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {"include": ["refs/heads/develop"], "exclude": []}
            },
            "bypass_actors": [
                {
                    "actor_id": 4513412,
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [
                            {"context": "Extra live check", "integration_id": 987}
                        ],
                    },
                },
                {"type": "deletion"},
            ],
        },
    }


@pytest.mark.parametrize("explicit_null", [False, True])
def test_repeat_apply_is_noop_and_preserves_live_rules(
    tmp_path: Path, explicit_null: bool
) -> None:
    state = migrated_state()
    state["explicit_null"] = explicit_null
    result, after = run(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert after == state


def test_initial_migration_preserves_check_provider_and_strictness(
    tmp_path: Path,
) -> None:
    result, after = run(tmp_path, classic_state())
    assert result.returncode == 0, result.stderr
    assert after["writes"] == ["POST", "DELETE"]
    assert after["classic"] is None
    assert after["ruleset"]["rules"][0]["parameters"] == {
        "strict_required_status_checks_policy": True,
        "required_status_checks": [{"context": "Custom check", "integration_id": 987}],
    }
    result, repeated = run(tmp_path, {**after, "writes": []})
    assert result.returncode == 0, result.stderr
    assert repeated["writes"] == []


def test_partial_migration_retains_both_check_sets_and_other_rules(
    tmp_path: Path,
) -> None:
    state = migrated_state()
    state["classic"] = classic_state()["classic"]
    result, after = run(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert after["writes"] == ["PUT", "DELETE"]
    assert after["ruleset"]["rules"][1] == {"type": "deletion"}
    assert after["ruleset"]["rules"][0]["parameters"]["required_status_checks"] == [
        {"context": "Custom check", "integration_id": 987},
        {"context": "Extra live check", "integration_id": 987},
    ]


@pytest.mark.parametrize("failure", ["read_error", "write_error"])
def test_api_failure_never_deletes_classic_protection(
    tmp_path: Path, failure: str
) -> None:
    state = {**classic_state(), failure: True}
    result, after = run(tmp_path, state)
    assert result.returncode != 0
    assert after["classic"] == state["classic"]
    assert "DELETE" not in after["writes"]
    if failure == "read_error":
        assert after["writes"] == []


@pytest.mark.parametrize("classic", [None, {}, {"contexts": [], "checks": []}])
def test_missing_or_empty_source_does_not_invent_checks(
    tmp_path: Path, classic: dict[str, Any] | None
) -> None:
    result, after = run(tmp_path, {"classic": classic, "writes": []})
    assert result.returncode != 0
    assert after["writes"] == []


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    state = classic_state()
    result, after = run(tmp_path, state, apply=False)
    assert result.returncode == 0, result.stderr
    assert state == after


def test_failed_readback_keeps_classic_checks(tmp_path: Path) -> None:
    state = {**classic_state(), "bad_readback": True}
    result, after = run(tmp_path, state)
    assert result.returncode != 0
    assert after["classic"] == state["classic"]
    assert after["writes"] == ["POST"]
