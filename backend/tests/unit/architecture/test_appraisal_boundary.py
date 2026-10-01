"""Automation and model signals cannot appraise (GOO-309).

(a) Workers (``src/tasks/``), the agent (``src/services/agent/``), the
extraction worker service and the draft generation service never name
``appraisal_service``, ``AppraisalAssessment`` or ``appraisal.submitted``.
(b) The appraisal rules and service never name model confidence, citation
popularity, the stance classifier or the legacy ``QualityMark`` checks.
(c) ``submit`` and ``adjudicate`` require a resolved ``ProjectContext``.
Ledger replay refusing a non-reviewer submission and a non-adjudicator (or
self-) adjudication is the database-side enforcement.
``# ponytail: add an appraisal_suggestions table beside, never inside,
appraisal_assessments when a model proposes answers.``
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
RULES = SRC / "services/research_engine/appraisal_rules.py"
SERVICE = SRC / "services/research_engine/appraisal_service.py"
_WRITERS = {"appraisal_service", "AppraisalAssessment", "appraisal.submitted"}
_SIGNALS = {
    "confidence",
    "citation_count",
    "cited_by",
    "StanceClassificationModel",
    "QualityMark",
    "verification",
}


def _names(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Name):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.arg):
            found.append((node.lineno, node.arg))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [node.module] if isinstance(node, ast.ImportFrom) else []
            for name in [*modules, *(a.name for a in node.names)]:
                found += [(node.lineno, part) for part in (name or "").split(".")]
            found += [(node.lineno, a.asname) for a in node.names if a.asname]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def _automation_modules() -> list[Path]:
    return [
        *sorted((SRC / "tasks").rglob("*.py")),
        *sorted((SRC / "services/agent").rglob("*.py")),
        SRC / "services/research/extraction_matrix_service.py",
        SRC / "services/research/draft_generation_service.py",
    ]


def test_automation_cannot_write_appraisals() -> None:
    modules = _automation_modules()
    assert all(m.exists() for m in modules) and len(modules) > 10  # not vacuous
    violations = [
        f"{path}:{line} names {name}"
        for path in modules
        for line, name in _names(path)
        if name in _WRITERS
    ]
    assert violations == []


def test_appraisal_never_reads_model_or_popularity_signals() -> None:
    violations = [
        f"{path}:{line} names {name}"
        for path in (RULES, SERVICE)
        for line, name in _names(path)
        if name in _SIGNALS
    ]
    assert violations == []


def test_writes_require_a_resolved_project_context() -> None:
    tree = ast.parse(SERVICE.read_text())
    writers = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name in {"submit", "adjudicate"}
    }
    assert set(writers) == {"submit", "adjudicate"}
    for function in writers.values():
        annotations = {
            a.arg: ast.unparse(a.annotation) for a in function.args.args if a.annotation
        }
        assert annotations.get("context") == "ProjectContext", function.name
