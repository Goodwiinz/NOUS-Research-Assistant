"""Deterministic synthesis stays deterministic; narrative steps stay apart (GOO-311).

(a) ``synthesis_rules`` imports only the standard library.
(b) ``synthesis_rules`` and ``synthesis_service`` never import a model client
(``openai``, ``anthropic``, ``langchain*``), ``src.services.llm*``,
``src.services.agent*`` or the A/B experiment models.
(c) Workers (``src/tasks/``), the agent (``src/services/agent/``) and the
blueprint step executor never import ``synthesis_service`` or
``synthesis_rules`` and never construct ``SynthesisResult(``: the
``synthesize`` blueprint step and narrative drafts are untouched.
``# ponytail: LLM-generated analysis, A/B tables and relabelling narrative
synthesis are out of scope; a model never writes a synthesis_results row.``
"""

import ast
import sys
from pathlib import Path

import pytest

from src.schemas.research_engine import StepType

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
RULES = SRC / "services/research_engine/synthesis_rules.py"
SERVICE = SRC / "services/research_engine/synthesis_service.py"
STEP_EXECUTOR = SRC / "services/research_engine/step_executor.py"
_MODEL_PREFIXES = (
    "openai",
    "anthropic",
    "langchain",
    "src.services.llm",
    "src.services.agent",
    "src.models.ab_testing",
)
_SYNTHESIS = ("synthesis_service", "synthesis_rules")


def _imports(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            found.append((node.lineno, base))
            found += [(node.lineno, f"{base}.{a.name}") for a in node.names]
    return found


def test_rules_import_only_stdlib() -> None:
    for line, name in _imports(RULES):
        root = name.split(".")[0]
        assert root in sys.stdlib_module_names, f"synthesis_rules:{line} {name}"


def test_rules_and_service_import_no_model_client() -> None:
    for path in (RULES, SERVICE):
        for line, name in _imports(path):
            assert not name.startswith(_MODEL_PREFIXES), f"{path.name}:{line} {name}"


def _automation_modules() -> list[Path]:
    return [
        *sorted((SRC / "tasks").rglob("*.py")),
        *sorted((SRC / "services/agent").rglob("*.py")),
        STEP_EXECUTOR,
    ]


def test_automation_never_executes_synthesis() -> None:
    assert len(_automation_modules()) > 10  # the scan found the trees
    for path in _automation_modules():
        text = path.read_text()
        assert "SynthesisResult(" not in text, path
        for line, name in _imports(path):
            assert not any(
                part in _SYNTHESIS for part in name.split(".")
            ), f"{path.relative_to(SRC)}:{line} {name}"


def test_narrative_synthesize_step_untouched() -> None:
    assert StepType.SYNTHESIZE == "synthesize"
    text = STEP_EXECUTOR.read_text()
    assert not any(name in text for name in _SYNTHESIS)
