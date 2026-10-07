"""Gate 0a of the harness live-proof runbook says what the migration chain prints.

``docs/testing/harness-live-proof.md`` tells the operator that Gate 0a prints N
``Running upgrade`` lines, names the revisions, and repeats N for the hosted
Alembic step and for the evidence table. Merges kept changing the chain under
those sentences (Plan 06 Slices 2 and 3, #1784, then ``hb03_workspace_grants``)
and each one left a stale count behind, so an operator on a correct checkout
would read a correct log as a failure.

These tests run the runbook's own ``grep`` over the real chain and compare. They
pin the count and the revisions the runbook lists, which change only when a
revision that grep matches is added or removed. They do not pin the head the
runbook quotes: it moves with every migration, ``check_alembic.py`` is the
authority for it, and the runbook says to judge a checkout by it.

``alembic history`` runs in a child process. It reads the revision scripts and
opens no connection, and nothing here imports alembic in process (see
``test_migration_tests_collect_together.py`` for why).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = REPO_ROOT / "backend"
RUNBOOK = REPO_ROOT / "docs" / "testing" / "harness-live-proof.md"

NUMBER_WORDS = {
    word: number
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen "
        "twenty".split()
    )
}
# The three places the runbook states how many lines 0a prints.
COUNT_CLAIMS = {
    "the Expected paragraph": r"Expected: 0a prints (\w+) `Running upgrade` lines",
    "the hosted Alembic step": r"same `Running upgrade` lines as 0a: (\w+) ",
    "the evidence table row": r"^\| 0a \|[^\n]*?the (\w+) revision lines",
}


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _gate_0a_pattern() -> str:
    """The ``grep -E`` pattern of the 0a command, exactly as the runbook prints it."""
    block = _runbook().split("# 0a.", 1)[1].split("# 0b.", 1)[0]
    patterns: list[str] = re.findall(r"grep -E -- '([^']+)'", block)
    assert len(patterns) == 1, f"expected one grep in the 0a block, got {patterns}"
    return patterns[0]


def _alembic_history() -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    # Only the dialect is read from the URL; nothing connects.
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "history"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.fixture(scope="module")
def printed() -> set[str]:
    """The revisions whose ``Running upgrade X -> Y`` line the 0a grep keeps.

    ``alembic history`` prints ``X -> Y, message`` for each revision, the same
    arrow text ``upgrade head`` logs after ``Running upgrade``, so the runbook's
    pattern selects the same revisions from either.
    """
    pattern = _gate_0a_pattern()
    revisions: set[str] = set()
    for line in _alembic_history():
        if re.search(pattern, line):
            target = re.search(r"-> (\w+)", line)
            assert target is not None, line
            revisions.add(target.group(1))
    assert revisions, f"the 0a grep {pattern!r} matches no revision in the chain"
    return revisions


def test_expected_output_names_the_revisions_the_0a_grep_prints(
    printed: set[str],
) -> None:
    paragraph = re.search(r"^Expected: 0a prints .*$", _runbook(), re.MULTILINE)
    assert paragraph, "the runbook has no 'Expected: 0a prints' paragraph"
    named = re.search(r"one for each of (.*?), then `alembic current`", paragraph[0])
    assert named, "the Expected paragraph no longer lists one revision per line"
    listed = set(re.findall(r"`(\w+)`", named[1]))
    assert listed == printed, (
        f"the 0a grep prints a line for {sorted(printed)}, but the runbook lists "
        f"{sorted(listed)}: missing {sorted(printed - listed)}, "
        f"not printed {sorted(listed - printed)}"
    )


@pytest.mark.parametrize("where", COUNT_CLAIMS)
def test_every_count_the_runbook_states_for_0a_is_the_chain_count(
    where: str, printed: set[str]
) -> None:
    claim = re.search(COUNT_CLAIMS[where], _runbook(), re.MULTILINE)
    assert claim, f"{where} no longer states how many lines 0a prints"
    word = claim[1]
    stated = int(word) if word.isdigit() else NUMBER_WORDS.get(word)
    assert stated is not None, f"{where} counts with {word!r}: add it to NUMBER_WORDS"
    assert stated == len(printed), (
        f"{where} says {word}, but the 0a grep prints {len(printed)} lines "
        f"({sorted(printed)})"
    )
