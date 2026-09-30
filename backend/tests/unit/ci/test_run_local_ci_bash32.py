"""Contracts that keep run_local_ci.sh honest on macOS Bash 3.2 (GOO-330)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCAL_CI_PATH = REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh"


def _script() -> str:
    return LOCAL_CI_PATH.read_text(encoding="utf-8")


def test_no_bash4_only_builtins() -> None:
    # macOS ships Bash 3.2, where these fail at runtime ("command not found",
    # "invalid option") while the script keeps going and can still exit 0.
    script = _script()
    for construct in ("mapfile", "readarray", "declare -A", "local -n"):
        assert construct not in script, construct


def test_enumerator_exit_status_is_never_swallowed() -> None:
    # `done < <(cmd)` discards cmd's exit status, so a crashed changed-file
    # enumerator reads as "no changed files" and the gates it arms pass.
    script = _script()
    assert "done < <(" not in script
    assert script.count('|| check 1 "list ') == script.count('done <<< "$LIST"')
