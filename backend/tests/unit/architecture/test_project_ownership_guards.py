"""Project ownership wrappers must delegate to ``resolve_project`` (audit B2).

Owner decision: wontfix-with-guard. Several modules keep a private
``_get_project_with_auth`` / ``_verify_project_ownership`` wrapper instead of
calling the canonical boundary directly. That is accepted design debt as long
as every wrapper still goes through
``services/research_engine/project_access.resolve_project`` (ownership,
membership, ancestor soft-delete and lifecycle checks). A wrapper that grows
its own ``Collection.owner_id == ...`` query instead fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
WRAPPER_NAMES = frozenset({"_get_project_with_auth", "_verify_project_ownership"})
CANONICAL = "resolve_project"
# Known on 2026-11-11: api/research/projects.py, api/research/project_chat.py,
# services/agent/tool_helpers.py. Guards against the scan silently matching
# nothing (renamed directory, broken glob).
MIN_WRAPPERS = 3

AnyFunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _calls_canonical(func: AnyFunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == CANONICAL:
            return True
        if isinstance(target, ast.Attribute) and target.attr == CANONICAL:
            return True
    return False


def _wrappers(root: Path) -> list[tuple[str, AnyFunctionDef]]:
    found: list[tuple[str, AnyFunctionDef]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in WRAPPER_NAMES
            ):
                found.append((path.relative_to(root).as_posix(), node))
    return found


def test_wrapper_scan_is_not_vacuous() -> None:
    found = _wrappers(SRC)
    assert len(found) >= MIN_WRAPPERS, (
        f"Expected at least {MIN_WRAPPERS} project ownership wrappers named "
        f"{sorted(WRAPPER_NAMES)} under {SRC}, found {len(found)}: "
        f"{[loc for loc, _ in found]}. If wrappers were removed in favour of "
        "calling resolve_project directly, lower MIN_WRAPPERS."
    )


def test_every_ownership_wrapper_calls_resolve_project() -> None:
    offenders = [
        f"{loc}:{func.lineno} {func.name}"
        for loc, func in _wrappers(SRC)
        if not _calls_canonical(func)
    ]
    assert not offenders, (
        "Project ownership wrappers must call resolve_project "
        "(src/services/research_engine/project_access.py) instead of "
        "re-implementing the ownership check (audit B2):\n  " + "\n  ".join(offenders)
    )
