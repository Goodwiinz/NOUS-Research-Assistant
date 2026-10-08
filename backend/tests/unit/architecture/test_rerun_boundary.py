"""GOO-313 structural guards: a fresh rerun never reaches the conversation
sandbox, conversation-stateful ``execute_code`` is byte-for-byte the same
code, admission goes through the retained-plan conformance contract, the
rules stay pure, and one service writes rerun rows.

Mutation verification: removing ``require_run_conformance`` from
``rerun_service.admit`` fails ``-k conformance``; editing
``_tool_execute_code`` fails ``-k execute_code``.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
SERVICE = SRC / "services/research_engine/rerun_service.py"
RULES = SRC / "services/research_engine/rerun_rules.py"
TOOLS = SRC / "services/agent/tools_impl.py"
MODELS = {"ExperimentRerun", "ExperimentRerunAttempt"}
THREAD_CACHE = {"get_or_create_sandbox", "install_packages", "cleanup", "cleanup_all"}
# sha256 of ast.dump(_tool_execute_code) on Python 3.11 (CI's version). A
# deliberate change to the conversation tool updates this in its own PR.
EXECUTE_CODE_AST_SHA256 = (
    "085ee6c93a369d638c32ccfb386f833e18d10991c093f6313db5d0c6c902aeb2"
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _function(path: Path, name: str) -> ast.AST:
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    raise AssertionError(f"{name} not found in {path}")


def _calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _is_manager(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _called_name(node) == "get_sandbox_manager"


def test_rerun_never_uses_the_thread_sandbox() -> None:
    tree = _tree(SERVICE)
    managers = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_manager(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    offenders = []
    isolated = []
    for call in _calls(tree):
        name = _called_name(call)
        if name in THREAD_CACHE or name == "_sandboxes":
            offenders.append(f"{name}:{call.lineno}")
        if isinstance(call.func, ast.Attribute):
            receiver = call.func.value
            on_manager = _is_manager(receiver) or (
                isinstance(receiver, ast.Name) and receiver.id in managers
            )
            if on_manager and name != "run_isolated":
                offenders.append(f"{name}:{call.lineno}")
            if on_manager and name == "run_isolated":
                isolated.append(call)
    assert not offenders, offenders
    assert isolated, "rerun_service must execute through run_isolated"
    assert all(
        any(k.arg == "restore" for k in call.keywords) for call in isolated
    ), "every rerun execution runs in verified restore mode"


def test_execute_code_tool_unchanged() -> None:
    dump = ast.dump(_function(TOOLS, "_tool_execute_code"))
    assert hashlib.sha256(dump.encode()).hexdigest() == EXECUTE_CODE_AST_SHA256


def test_admission_goes_through_retained_plan_conformance() -> None:
    names = {_called_name(call) for call in _calls(_function(SERVICE, "admit"))}
    assert "require_run_conformance" in names
    assert "run_context" in names  # REVIEW resolution before anything else


def test_rerun_rules_import_only_stdlib_and_manifest_rules() -> None:
    imported: set[str] = set()
    for node in ast.walk(_tree(RULES)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "rerun_rules must not use relative imports"
            module = node.module or ""
            if module == "src.services.research_engine":
                assert [a.name for a in node.names] == ["manifest_rules"]
                continue
            imported.add(module)
    roots = {name.split(".")[0] for name in imported}
    assert roots <= set(sys.stdlib_module_names), roots


def test_single_writer_of_rerun_rows() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == SERVICE:
            continue
        for call in _calls(_tree(path)):
            if isinstance(call.func, ast.Name) and call.func.id in MODELS:
                offenders.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert not offenders, offenders
