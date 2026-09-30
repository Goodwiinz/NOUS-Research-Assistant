"""Automation cannot verify a draft (GOO-307).

Workers (``src/tasks/``), the agent (``src/services/agent/``) and the draft
generation service (the agent's ``create_draft``/``revise_draft`` path) may
not name ``promote``, construct ``DraftRelease`` or spell
``release.promoted``. ``promote`` requires a resolved ``ProjectContext``, and
its only caller is the route that resolves ``ResearchAction.RELEASE``. Ledger
replay rejecting a non-adjudicator, non-supervisor promotion is the third
enforcement (``test_release_replay_rejects_reviewer_promotion``).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
SERVICE = SRC / "services/research/draft_release_service.py"
ROUTE = SRC / "api/research/drafts.py"


def _automation_modules() -> list[Path]:
    return [
        *sorted((SRC / "tasks").rglob("*.py")),
        *sorted((SRC / "services/agent").rglob("*.py")),
        SRC / "services/research/draft_generation_service.py",
    ]


def _violations(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.rsplit(".", 1)[-1] for alias in node.names]
            names += [a.asname for a in node.names if a.asname]
        found += [f"{path}:{node.lineno} names {n}" for n in names if n == "promote"]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "DraftRelease"
        ):
            found.append(f"{path}:{node.lineno} constructs DraftRelease")
        if isinstance(node, ast.Constant) and node.value == "release.promoted":
            found.append(f"{path}:{node.lineno} spells release.promoted")
    return found


def test_workers_agents_and_generation_cannot_promote() -> None:
    modules = _automation_modules()
    assert all(m.exists() for m in modules) and len(modules) > 10  # not vacuous
    assert [v for path in modules for v in _violations(path)] == []


def test_promote_requires_a_resolved_project_context() -> None:
    tree = ast.parse(SERVICE.read_text())
    (promote,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "promote"
    ]
    annotations = {
        a.arg: ast.unparse(a.annotation) for a in promote.args.args if a.annotation
    }
    assert annotations.get("context") == "ProjectContext"


def test_only_the_release_route_calls_promote() -> None:
    callers = [
        path
        for path in sorted(SRC.rglob("*.py"))
        if path != SERVICE
        and any(
            isinstance(n, ast.Attribute) and n.attr == "promote"
            for n in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert callers == [ROUTE]
    tree = ast.parse(ROUTE.read_text())
    (route,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and any(
            isinstance(c, ast.Attribute) and c.attr == "promote" for c in ast.walk(n)
        )
    ]
    assert "ResearchAction.RELEASE" in ast.unparse(route)
