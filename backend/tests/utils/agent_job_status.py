"""Unit-test helpers for the durable agent job-status boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest


def stub_durable_status_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub external durability reads/writes while preserving status arbitration."""
    from src.services.agent.agent_run_service import RunStatusDecision
    from src.shared.enums import JobStatus

    async def record_job_status(
        job_id: str,
        data: dict[str, Any],
        *,
        raise_on_error: bool = False,
    ) -> RunStatusDecision:
        del raise_on_error
        status = JobStatus(data["status"])
        return RunStatusDecision(
            job_id=job_id,
            requested_status=status,
            effective_status=status,
            user_id=data.get("user_id"),
            organization_id=data.get("organization_id"),
            thread_id=data.get("thread_id"),
            error=data.get("error"),
            cancel_requested_at=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def cancellation_requested(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def no_redis() -> None:
        return None

    monkeypatch.setattr(
        "src.services.agent.agent_run_service.record_job_status", record_job_status
    )
    monkeypatch.setattr(
        "src.services.agent.agent_run_service.is_run_cancellation_requested",
        cancellation_requested,
    )
    monkeypatch.setattr("src.services.agent.job_store._get_redis", no_redis)
