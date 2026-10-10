"""GOO-312 structural guards: isolated execution never touches the
conversation sandbox cache, the manifest rules stay stdlib-only, and one
service is the only writer of manifests, run artifacts and figures."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
SANDBOX = SRC / "services/sandbox/e2b_sandbox_manager.py"
TOOLS = SRC / "services/agent/tools_impl.py"
RULES = SRC / "services/research_engine/manifest_rules.py"
WRITER = SRC / "services/research_engine/experiment_service.py"
MODELS = {"ResearchRunManifest", "ResearchRunArtifact", "ResearchFigure"}


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"{name} not found in {path}")


def _names(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found


def test_isolated_execution_never_meets_the_thread_cache() -> None:
    assert "run_isolated" not in _names(_function(TOOLS, "_tool_execute_code"))
    assert "run_isolated" not in _names(_function(SANDBOX, "get_or_create_sandbox"))
    isolated = _names(_function(SANDBOX, "run_isolated"))
    assert not isolated & {"_sandboxes", "get_or_create_sandbox"}


def test_manifest_rules_import_only_stdlib() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(RULES.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "manifest_rules must not use relative imports"
            imported.add((node.module or "").split(".")[0])
    assert imported and imported <= set(sys.stdlib_module_names), imported


def test_single_writer_of_manifest_artifact_and_figure_rows() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == WRITER:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in MODELS
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, offenders
