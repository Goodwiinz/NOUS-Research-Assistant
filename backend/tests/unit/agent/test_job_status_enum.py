"""JobStatus wire-contract truth table (audit finding C7).

The backend used to write six untyped status strings (running, completed,
awaiting_confirmation, failed, error, cancelled) while the frontend typed
four. JobStatus is now the single source of truth: seven canonical members,
with "error" collapsed into FAILED (accepted as an inbound alias for one
release, never written). These tests pin the exact value set, the alias
mapping, the terminal classification, and JSON transparency — the properties
every poller, the agent_runs projection, and the frontend mirror rely on.

Deliberately imports ONLY src.shared.enums so the contract is testable
without the agent stack (no langgraph / DB deps).
"""

import json

import pytest

from src.shared.enums import TERMINAL_JOB_STATUSES, JobStatus

pytestmark = pytest.mark.unit


def test_exact_wire_value_set():
    """The canonical wire strings — 'error' is intentionally NOT a member."""
    assert {s.value for s in JobStatus} == {
        "queued",
        "running",
        "awaiting_confirmation",
        "stopping",
        "recovering",
        "completed",
        "failed",
        "cancelled",
    }


def test_error_alias_normalizes_to_failed():
    """One-release inbound alias: legacy records read back as FAILED."""
    assert JobStatus("error") is JobStatus.FAILED
    assert JobStatus("ERROR") is JobStatus.FAILED  # defensive case-fold


def test_unknown_status_still_raises():
    """Only the documented alias is tolerated — corruption stays loud."""
    with pytest.raises(ValueError):
        JobStatus("exploded")


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (JobStatus.QUEUED, False),
        (JobStatus.RUNNING, False),
        (JobStatus.AWAITING_CONFIRMATION, False),
        (JobStatus.STOPPING, False),
        (JobStatus.COMPLETED, True),
        (JobStatus.FAILED, True),
        (JobStatus.CANCELLED, True),
    ],
)
def test_terminal_truth_table(status: JobStatus, terminal: bool):
    assert status.is_terminal is terminal


def test_truth_table_is_exhaustive():
    """Every member is classified — a new status cannot dodge the table."""
    classified = {s for s in JobStatus if s.is_terminal} | {
        s for s in JobStatus if not s.is_terminal
    }
    assert classified == set(JobStatus)
    assert TERMINAL_JOB_STATUSES < set(JobStatus)  # strict subset — never all


def test_members_are_wire_transparent():
    """StrEnum members compare/serialize as their raw strings.

    The job store JSON-round-trips records through Redis and compares raw
    dict values against members (CAS, ownership checks) — both directions
    must be transparent.
    """
    assert JobStatus.RUNNING == "running"
    assert str(JobStatus.AWAITING_CONFIRMATION) == "awaiting_confirmation"
    assert json.dumps({"status": JobStatus.COMPLETED}) == '{"status": "completed"}'
    assert JobStatus(json.loads('"failed"')) is JobStatus.FAILED
