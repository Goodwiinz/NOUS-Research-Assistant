"""The offline migration tests collect together, stub-installers first.

Six of the migration tests replace ``alembic`` in ``sys.modules`` with a stub
when they are imported before the real package is. A test that then imports the
real ``alembic.config`` at module level fails to collect:

    ModuleNotFoundError: No module named 'alembic.config'; 'alembic' is not a package

The full CI run never sees it, because other modules import the real package
first. Running the migration tests on their own does, and so did
``test_workspace_grants_migration.py`` until it stopped importing alembic in
process. Collect them the worst way round in a fresh process (the stubs would
otherwise leak into this one), so the next module that imports alembic at module
level is caught here and not by whoever runs the migration tests together.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]
UNIT = BACKEND_ROOT / "tests" / "unit"
# How a module installs its stub: sys.modules["alembic"] = ... or a module
# object built with types.ModuleType("alembic").
STUBS_ALEMBIC = re.compile(
    r"""sys\.modules\[\s*["']alembic["']\s*\]\s*=|ModuleType\(\s*["']alembic["']"""
)


def test_migration_tests_collect_in_one_process_with_the_stubbing_ones_first() -> None:
    files = sorted(UNIT.glob("test_*migration*.py"))
    assert len(files) > 1, "the migration tests moved: fix the glob"
    stubbing = [f for f in files if STUBS_ALEMBIC.search(f.read_text("utf-8"))]
    others = [f for f in files if f not in stubbing]
    # Not under the parent's coverage or xdist: only collection is asked of it.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE", "PYTEST_XDIST"))
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:cov",
            "-o",
            "addopts=",
            "-o",
            "log_cli=false",
            *map(str, stubbing + others),
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
