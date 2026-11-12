"""Freeze the set of tables mapped by more than one model class (audit D4).

Owner decision: wontfix-with-guard. Seven table names are each declared by two
SQLAlchemy classes in different modules (legacy and newer model files that
never merged). Consolidating them is accepted design debt; a new duplicate
``__tablename__`` is not, because two mappings of one table drift apart
silently (columns, constraints, enum types).

``FROZEN_DUPLICATES`` may only shrink.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"

# Frozen 2026-11-11 against origin/develop.
FROZEN_DUPLICATES: frozenset[str] = frozenset(
    {
        "ab_experiments",
        "ab_user_segments",
        "ab_variants",
        "document_quality_metrics",
        "entities",
        "metric_aggregations",
        "system_metrics",
    }
)


def _tablename(stmt: ast.stmt) -> str | None:
    targets: list[ast.expr]
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets, value = [stmt.target], stmt.value
    else:
        return None
    if not any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in targets):
        return None
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _table_mappings(root: Path) -> dict[str, list[str]]:
    mappings: dict[str, list[str]] = defaultdict(list)
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                name = _tablename(stmt)
                if name is not None:
                    mappings[name].append(f"{rel}:{node.name}")
    return mappings


def _duplicates(root: Path) -> dict[str, list[str]]:
    return {t: c for t, c in _table_mappings(root).items() if len(c) > 1}


def test_table_scan_is_not_vacuous() -> None:
    assert len(_table_mappings(SRC)) > 50, f"__tablename__ scan of {SRC} found too few"


def test_no_new_duplicate_table_mappings() -> None:
    dupes = _duplicates(SRC)
    new = sorted(set(dupes) - FROZEN_DUPLICATES)
    assert not new, (
        "These table names are now mapped by more than one model class. Reuse "
        "the existing model instead of declaring a second __tablename__ "
        "(audit D4):\n  " + "\n  ".join(f"{t}: {dupes[t]}" for t in new)
    )


def test_frozen_duplicates_have_no_stale_entries() -> None:
    stale = sorted(FROZEN_DUPLICATES - set(_duplicates(SRC)))
    assert not stale, (
        "These tables are no longer mapped twice. Remove them from the frozen "
        "set FROZEN_DUPLICATES so it only shrinks:\n  "
        + "\n  ".join(f"remove {t} from frozen set" for t in stale)
    )
