"""Certainty, evidence agreement and model confidence stay apart (GOO-310).

(a) ``evidence_rules`` never names ``confidence`` or ``stance`` outside
``stance_groups``: GRADE's arithmetic and the contradiction status cannot
read a model signal.
(b) The stance meter (``src/services/evidence/``) and workers (``src/tasks/``)
never name ``evidence_service``, the contradiction or certainty models or the
three ``evidence.*`` events: a classifier cannot write a decision.
(c) ``evidence_service`` never merges, updates or deletes; its rows are
insert-only, and the database triggers enforce the same.
(d) The three writers require a resolved ``ProjectContext``.
``# ponytail: promote stance rows into decisions only through a person's
opened contradiction, which already snapshots them.``
"""

import ast
from pathlib import Path

import pytest

from tests.unit.architecture.test_appraisal_boundary import _names

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
RULES = SRC / "services/research_engine/evidence_rules.py"
SERVICE = SRC / "services/research_engine/evidence_service.py"
_MODEL_WORDS = ("confidence", "stance")
_DECISIONS = {
    "evidence_service",
    "EvidenceContradiction",
    "OutcomeCertaintyAssessment",
    "evidence.table_versioned",
    "evidence.contradiction_recorded",
    "evidence.certainty_assessed",
}
_MUTATORS = {"merge", "update", "delete"}


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of docstring constants (prose may explain the rule it obeys)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _identifiers(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.keyword) and node.arg:
        return [node.arg]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name for a in node.names]
    return []


def test_certainty_rules_never_name_model_signals() -> None:
    tree = ast.parse(RULES.read_text(), filename=str(RULES))
    exempt = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "stance_groups"
        for inner in ast.walk(node)
    }
    assert exempt, "stance_groups must exist"  # not vacuous
    docstrings = _docstrings(tree)
    violations = []
    for node in ast.walk(tree):
        if id(node) in exempt or id(node) in docstrings:
            continue
        words = _identifiers(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.append(node.value)
        violations += [
            f"{RULES.name}:{getattr(node, 'lineno', '?')} names {word!r}"
            for word in words
            if any(signal in word.lower() for signal in _MODEL_WORDS)
        ]
    assert violations == []


def test_stance_meter_and_workers_cannot_write_decisions() -> None:
    modules = [
        *sorted((SRC / "services/evidence").rglob("*.py")),
        *sorted((SRC / "tasks").rglob("*.py")),
    ]
    assert len(modules) > 10  # not vacuous
    violations = [
        f"{path}:{line} names {name}"
        for path in modules
        for line, name in _names(path)
        if name in _DECISIONS
    ]
    assert violations == []


def test_evidence_service_never_mutates_rows() -> None:
    tree = ast.parse(SERVICE.read_text(), filename=str(SERVICE))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if name in _MUTATORS:
                violations.append(f"{SERVICE.name}:{node.lineno} calls {name}(")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            violations += [
                f"{SERVICE.name}:{node.lineno} imports {a.name}"
                for a in node.names
                if a.name in _MUTATORS
            ]
    assert violations == []


def test_writes_require_a_resolved_project_context() -> None:
    tree = ast.parse(SERVICE.read_text())
    writers = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name in {"create_table", "record_contradiction", "assess_certainty"}
    }
    assert len(writers) == 3
    for function in writers.values():
        annotations = {
            a.arg: ast.unparse(a.annotation) for a in function.args.args if a.annotation
        }
        assert annotations.get("context") == "ProjectContext", function.name
