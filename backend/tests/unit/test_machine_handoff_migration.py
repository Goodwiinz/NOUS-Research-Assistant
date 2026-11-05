"""mh01_machine_handoff's literal SQL stays equal to the model it migrates to.

Migrations never import models, so the revision repeats the active-thread
predicate and the status list. This reads them with ``ast`` (never importing
alembic in process: tests/unit/ci/test_migration_tests_collect_together.py).
Executing the revision is tests/integration/test_machine_handoff_migration_postgres.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.models.agent_run import (
    AGENT_RUN_ACTIVE_THREAD_PREDICATE,
    AGENT_RUN_HANDOFF_CALL_PREDICATE,
    AgentRun,
)
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[2] / "alembic/versions/mh01_machine_handoff.py"


def _constants() -> dict[str, str]:
    tree = ast.parse(_PATH.read_text("utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    found[target.id] = node.value.value
    return found


def test_active_thread_predicate_matches_the_model() -> None:
    assert _constants()["_ACTIVE_PREDICATE"] == AGENT_RUN_ACTIVE_THREAD_PREDICATE


def test_handoff_call_index_matches_the_model() -> None:
    assert _constants()["_HANDOFF_CALL_PREDICATE"] == AGENT_RUN_HANDOFF_CALL_PREDICATE
    (index,) = (
        i for i in AgentRun.__table__.indexes if i.name == "uq_agent_runs_handoff_call"
    )
    assert index.unique
    assert [c.name for c in index.columns] == ["parent_run_id", "handoff_tool_call_id"]
    column = AgentRun.__table__.c.handoff_tool_call_id
    assert column.nullable and column.type.length == 255
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS handoff_tool_call_id VARCHAR\(255\);",
        _PATH.read_text("utf-8"),
    )


def test_upgraded_status_check_is_the_job_status_domain() -> None:
    statuses = set(re.findall(r"'([a-z_]+)'", _constants()["_STATUSES"]))
    assert statuses | {"delegated"} == {status.value for status in JobStatus}


def test_parent_fk_name_matches_the_model() -> None:
    (fk,) = AgentRun.__table__.c.parent_run_id.foreign_keys
    assert fk.name == "fk_agent_runs_parent_run_id"
    assert fk.ondelete == "SET NULL"
    assert re.search(
        r"ADD CONSTRAINT fk_agent_runs_parent_run_id\s+FOREIGN KEY \(parent_run_id\)"
        r"\s+REFERENCES agent_runs\(job_id\)\s+ON DELETE SET NULL",
        _PATH.read_text("utf-8"),
    )
