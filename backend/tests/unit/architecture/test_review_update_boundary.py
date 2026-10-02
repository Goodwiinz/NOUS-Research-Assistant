"""GOO-320 structural guards for superseding review versions.

(a) ``review_update_service`` never writes screening rows itself: it builds
    no ``ScreeningQueue``/``ScreeningAssignment``/``ScreeningObservation``/
    ``ScreeningResolution`` and mutates none; new work goes through
    ``screening_service.create_queue`` and ``assign`` only.
(b) The review version is its own row: no ``review_update_*`` module (nor
    the model) names ``ResearchRun`` or ``GeneratedDraft``.
(c) No module updates, deletes or merges a review version, a release link or
    a GOO-315 manuscript release row (insert-only, like the triggers).

Mutation verification: a ``ScreeningQueue(...)`` built in the service fails
``-k screening``; an ``update(ResearchReviewVersion)`` anywhere fails
``-k insert_only``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
ENGINE = SRC / "services/research_engine"
SERVICE = ENGINE / "review_update_service.py"
REVIEW_MODULES = (
    *sorted(ENGINE.glob("review_update_*.py")),
    SRC / "models/research_review_version.py",
    SRC / "api/research_engine/review_versions.py",
)
SCREENING_ROWS = {
    "ScreeningQueue",
    "ScreeningAssignment",
    "ScreeningObservation",
    "ScreeningResolution",
    "ScreeningSuggestion",
}
INSERT_ONLY = {
    "ResearchReviewVersion",
    "ResearchReviewReleaseLink",
    "ManuscriptRelease",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def test_service_writes_screening_only_through_screening_service() -> None:
    found = []
    for node in ast.walk(_tree(SERVICE)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in SCREENING_ROWS:
            found.append(f"builds {name}:{node.lineno}")
        if name in ("update", "delete", "insert") and any(
            isinstance(arg, ast.Name) and arg.id in SCREENING_ROWS for arg in node.args
        ):
            found.append(f"{name} screening row:{node.lineno}")
    assert found == []
    text = SERVICE.read_text()
    assert "screening_service.create_queue(" in text
    assert "screening_service.assign(" in text


def test_review_version_never_reuses_runs_or_draft_versions() -> None:
    assert len(REVIEW_MODULES) >= 4 and all(p.exists() for p in REVIEW_MODULES)
    offenders = []
    for path in REVIEW_MODULES:
        for node in ast.walk(_tree(path)):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute) else None
            )
            if isinstance(node, ast.ImportFrom):
                if any(a.name in ("ResearchRun", "GeneratedDraft") for a in node.names):
                    offenders.append(f"{path.name}:import:{node.lineno}")
            elif name in ("ResearchRun", "GeneratedDraft"):
                offenders.append(f"{path.name}:{name}:{node.lineno}")
    assert offenders == []


def test_no_module_mutates_insert_only_rows() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        if not any(name in text for name in INSERT_ONLY):
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in ("update", "delete", "merge") and any(
                isinstance(arg, ast.Name) and arg.id in INSERT_ONLY | {"V", "L"}
                for arg in node.args
            ):
                found.append(f"{path.relative_to(SRC)}:{name}:{node.lineno}")
    for node in ast.walk(_tree(SERVICE)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("merge", "delete"):
                found.append(f"service session.{node.func.attr}:{node.lineno}")
    assert found == []
