"""GOO-319 structural guards for scheduled search updates.

(a) Classification never reads a workspace withdrawal: ``search_update_rules``
    names no ``is_retracted``/``retracted_source*`` and imports no models,
    so no ``Document`` field can reach ``classify``.
(b) No module updates, deletes or merges a schedule, execution, attempt or
    result row (insert-only, like the database trigger).
(c) ``search_update_service`` writes no staleness: it imports nothing from
    ``draft_release_service`` (GOO-320 decides what a delta invalidates).
(d) Provider calls cross one boundary: none of the GOO-319 modules imports
    an HTTP client; they reach providers only through ``discovery`` and the
    connectors, which tests replace (no test reaches the network).

Mutation verification: ``if record.get("is_retracted")`` in
``search_update_rules`` fails ``-k withdrawal``; an
``update(ResearchSearchExecution)`` in the service fails ``-k insert_only``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
RULES = SRC / "services/research_engine/search_update_rules.py"
SERVICE = SRC / "services/research_engine/search_update_service.py"
MODULES = (
    RULES,
    SERVICE,
    SRC / "api/research_engine/search_updates.py",
    SRC / "tasks/search_update_tasks.py",
    SRC / "models/research_search_update.py",
)
INSERT_ONLY = {
    "ResearchSearchSchedule",
    "ResearchSearchExecution",
    "ResearchSearchExecutionAttempt",
    "ResearchSearchExecutionResult",
}
HTTP_CLIENTS = {"httpx", "requests", "aiohttp", "urllib3", "http"}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_classification_never_reads_workspace_withdrawal() -> None:
    tree = _tree(RULES)
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        or (isinstance(node, ast.Constant) and isinstance(node.value, str))
    }
    assert not {n for n in names if "is_retracted" in n or "retracted_source" in n}
    assert not any(name.startswith("src.models") for name in _imports(tree))
    assert not any("document" in name.lower() for name in _imports(tree))


def test_no_module_mutates_insert_only_rows() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        if not any(name in text for name in INSERT_ONLY):
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in ("update", "delete", "merge") and any(
                isinstance(arg, ast.Name)
                and arg.id in INSERT_ONLY | {"S", "E", "A", "R"}
                for arg in node.args
            ):
                found.append(f"{path.relative_to(SRC)}:{name}:{node.lineno}")
    for node in ast.walk(_tree(SERVICE)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("merge", "delete"):
                found.append(f"service session.{node.func.attr}:{node.lineno}")
    assert found == []


def test_service_writes_no_staleness() -> None:
    imports = _imports(_tree(SERVICE))
    assert not any("draft_release_service" in name for name in imports)
    assert not any("release_rules" in name for name in imports)


def test_provider_calls_cross_one_boundary() -> None:
    assert all(path.exists() for path in MODULES)
    talkers = [
        path.name
        for path in MODULES
        if any(name.split(".")[0] in HTTP_CLIENTS for name in _imports(_tree(path)))
    ]
    assert talkers == []
    service = _imports(_tree(SERVICE))
    assert "src.services.research_engine.discovery.search_sources" in service
