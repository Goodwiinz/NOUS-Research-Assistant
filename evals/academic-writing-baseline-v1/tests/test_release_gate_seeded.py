"""Seeded failures from the frozen corpora block promotion (GOO-307 hook).

Uses only each task's *condition* (the failure shape), never a gold
judgment, and never edits a corpus. Each draft holds one supported sentence
and one seeded failure; the pure release gate must block the failure with
the expected code and pass the supported sentence. The live 14-trial run
stays GOO-293's job.

Mutation check (docs/testing/agent-orchestration-mutation-checks.md,
GOO-307): treating a model-only stance as accepted in
``release_rules._factual_code`` lets ``dev-unsupported-number`` promote.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

TASK_DIR = Path(__file__).resolve().parents[1]
RULES = TASK_DIR.parents[1] / "backend/src/services/research/release_rules.py"


def _rules() -> ModuleType:
    """Load the pure module by path: no backend settings or database."""
    spec = importlib.util.spec_from_file_location("goo307_release_rules", RULES)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


rr = _rules()


def _tasks() -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for name in ("development.json", "held-out.json"):
        corpus = json.loads((TASK_DIR / "corpora" / name).read_text("utf-8"))
        tasks.update({task["id"]: task for task in corpus["tasks"]})
    return tasks


TASKS = _tasks()


def _claim(content: str, text: str, **kw: Any) -> Any:
    start = content.index(text)
    return rr.ClaimIn(
        claim_version_id=uuid4(),
        kind=kw.pop("kind", "factual"),
        start=start,
        end=start + len(text),
        text=text,
        attributed_to=kw.pop("attributed_to", None),
        **kw,
    )


def _supported(content: str, text: str) -> Any:
    link = rr.LinkIn(id=uuid4(), kind="source_span", live=True)
    assessment = rr.AssessmentIn(id=uuid4(), stance="supporting", link_ids=(link.id,))
    return _claim(content, text, links=(link,), assessment=assessment)


def _seeded(condition: str, content: str, text: str) -> tuple[Any, str]:
    """The failure claim for one corpus condition and its expected code."""
    link = rr.LinkIn(id=uuid4(), kind="source_span", live=True, observed=True)
    if condition == "unsupported_numerical_claim":
        # An invented number: only the model's stance, no adjudicator.
        return _claim(content, text, links=(link,)), "model_only"
    if condition == "contradictory_sources":
        opposed = rr.AssessmentIn(id=uuid4(), stance="opposing", link_ids=(link.id,))
        return _claim(content, text, links=(link,), assessment=opposed), "opposed"
    if condition == "foreign_project_identifier":
        # The assessment cites a link outside this project's edge set.
        foreign = rr.AssessmentIn(id=uuid4(), stance="supporting", link_ids=(uuid4(),))
        return (
            _claim(content, text, links=(link,), assessment=foreign),
            "stale_evidence",
        )
    if condition == "failed_revision":
        return _claim(content, text, kind="interpretation"), (
            "unattributed_interpretation"
        )
    raise AssertionError(f"no seeded shape for {condition}")


@pytest.mark.parametrize(
    "task_id",
    [
        "dev-unsupported-number",
        "dev-invalid-revision",
        "held-contradictory-sources",
        "held-hijacked-id",
    ],
)
def test_seeded_failure_blocks_and_supported_sentence_passes(task_id: str) -> None:
    task = TASKS[task_id]
    title = task["documents"][0]["title"]
    supported = f"{title} reports the documented finding [Doc 1]."
    failure = {
        "unsupported_numerical_claim": "The effect was 37 percent larger [Doc 1].",
        "contradictory_sources": "Both sources agree the result is positive [Doc 1].",
        "foreign_project_identifier": "The identifier confirms the record [Doc 1].",
        "failed_revision": "This shows the intervention clearly works.",
    }[task["condition"]]
    content = f"## Findings\n{supported} {failure}\n"
    claim, code = _seeded(task["condition"], content, failure)
    gate = rr.check_release(content, [_supported(content, supported), claim], {}, set())
    assert [(b.code, b.claim_version_id) for b in gate.blockers] == [
        (code, claim.claim_version_id)
    ]
    alone = f"## Findings\n{supported}\n"
    assert (
        rr.check_release(alone, [_supported(alone, supported)], {}, set()).blockers
        == ()
    )
