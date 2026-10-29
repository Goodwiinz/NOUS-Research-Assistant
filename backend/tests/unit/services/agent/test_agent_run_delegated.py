"""``delegated``: a parent run paused while a child Codex run works."""

from __future__ import annotations

import pytest

from src.models.agent_run import AGENT_RUN_STATUS_CHECK
from src.services.agent import agent_run_service, agent_submission_service
from src.shared.enums import JobStatus

pytestmark = pytest.mark.unit


def test_delegated_is_active_and_constrained() -> None:
    assert JobStatus("delegated") is JobStatus.DELEGATED
    assert not JobStatus.DELEGATED.is_terminal
    assert "'delegated'" in AGENT_RUN_STATUS_CHECK
    assert "delegated" in agent_submission_service._ACTIVE_RUN_STATUSES
    assert "delegated" in agent_run_service._ACTIVE_RUN_STATUSES
