"""Automation cannot accept an extraction value (GOO-304).

Workers (``src/tasks/``), the agent (``src/services/agent/``) and the matrix
extraction worker may not name ``accept_value`` or ``ExtractionAcceptedValue``,
and may import from ``extraction_forms_service`` only the machine-observation
helpers. ``accept_value`` itself must check ``ResearchProjectRole.ADJUDICATOR``.
Ledger replay rejects a non-adjudicator ``extraction.accepted`` as the third
enforcement (``test_extraction_replay_rejects_worker_acceptance``).
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
FORMS_MODULE = "src.services.research.extraction_forms_service"
FORBIDDEN_NAMES = {"accept_value", "ExtractionAcceptedValue"}
ALLOWED_IMPORTS = {
    "append_machine_observations",
    "extraction_task_kwargs",
    "document_source_hash",
}
WORKER = SRC / "services/research/extraction_matrix_service.py"


def _automation_modules() -> list[Path]:
    return [
        *sorted((SRC / "tasks").rglob("*.py")),
        *sorted((SRC / "services/agent").rglob("*.py")),
        WORKER,
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
        found += [
            f"{path}:{node.lineno} names {n}" for n in names if n in FORBIDDEN_NAMES
        ]
        if isinstance(node, ast.ImportFrom) and node.module == FORMS_MODULE:
            found += [
                f"{path}:{node.lineno} imports {a.name}"
                for a in node.names
                if a.name not in ALLOWED_IMPORTS
            ]
        elif (
            isinstance(node, ast.ImportFrom) and node.module == "src.services.research"
        ):
            found += [
                f"{path}:{node.lineno} imports the whole forms service"
                for a in node.names
                if a.name == "extraction_forms_service"
            ]
        elif isinstance(node, ast.Import):
            found += [
                f"{path}:{node.lineno} imports the whole forms service"
                for a in node.names
                if a.name == FORMS_MODULE
            ]
    return found


def test_workers_and_agents_cannot_reach_acceptance() -> None:
    modules = _automation_modules()
    assert WORKER.exists() and len(modules) > 10  # the scan is not vacuous
    violations = [v for path in modules for v in _violations(path)]
    assert violations == []


def test_worker_writes_only_through_machine_observations() -> None:
    tree = ast.parse(WORKER.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == FORMS_MODULE
        for alias in node.names
    }
    assert "append_machine_observations" in imported


def test_accept_value_requires_the_adjudicator_role() -> None:
    tree = ast.parse(
        (SRC / "services/research/extraction_forms_service.py").read_text()
    )
    (accept,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "accept_value"
    ]
    checks = [
        node
        for node in ast.walk(accept)
        if isinstance(node, ast.Compare)
        and isinstance(node.ops[0], ast.NotIn)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "ADJUDICATOR"
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "ResearchProjectRole"
    ]
    assert checks, "accept_value must check ResearchProjectRole.ADJUDICATOR"
