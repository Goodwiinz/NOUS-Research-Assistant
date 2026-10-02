"""Explicit identity-service double for unrelated transport unit tests.

Persistence, tracing and cancellation tests already stub the durable claim.
They import this fixture to isolate that same service boundary. Approval
conformance tests must use the real receipt service and a saved interrupt.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from src.services.agent.confirmation_service import PendingApproval


@pytest.fixture(autouse=True)
def isolated_confirmation_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.agent import streaming
    from src.services.agent import agent_execution_service

    def approved_snapshot(
        *args: Any, approval_id: str, **kwargs: Any
    ) -> PendingApproval:
        assert approval_id == "a" * 64
        return PendingApproval(approval_id, "test-interrupt", {})

    monkeypatch.setattr(streaming, "require_approval", approved_snapshot)
    monkeypatch.setattr(agent_execution_service, "require_approval", approved_snapshot)


def claimed_run(job_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        thread_id=None,
        user_message_id=None,
        client_message_id=None,
        status="running",
        run_metadata={"approval_id": "a" * 64},
    )


@pytest.fixture(autouse=True)
def isolated_claimed_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.agent import agent_execution_service

    async def get_claimed_run(_db: Any, job_id: str, **_scope: Any) -> SimpleNamespace:
        return claimed_run(job_id)

    monkeypatch.setattr(agent_execution_service, "get_run", get_claimed_run)
