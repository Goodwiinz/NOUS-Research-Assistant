"""Atomic external acceptance and provider observations.

Binding joins the caller's acceptance transaction. Observation recording owns
one transaction and serializes on the run row, like cancellation/finalization.
Unknown execution is never a terminal outcome or permission to start again.
"""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.models.bridge_device import WorkspaceBinding
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.schemas.integration_context import IntegrationContext
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.integrations.context import (
    IntegrationAccessDenied,
    _validate_grant,
    validate_binding,
)
from src.shared.enums import JobStatus

Observation = Literal["unknown", "running", "completed", "failed", "interrupted"]


class HarnessRunDTO(BaseModel):
    status: str
    workspace_locked: bool
    cancel_requested: bool


async def authorize_external_submission(
    db: AsyncSession,
    *,
    context: IntegrationContext,
    device_id: UUID,
    workspace_id: UUID,
    thread_id: UUID,
) -> None:
    """Recheck authority even for idempotent acceptance replays; no writes."""
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.id == context.grant_id)
        .execution_options(populate_existing=True)
    )
    grant = await _validate_grant(db, grant)
    if (
        grant.user_id != context.user_id
        or grant.organization_id != context.organization_id
        or grant.project_id != context.project_id
        or grant.thread_id != context.thread_id
        or grant.run_id != (str(context.run_id) if context.run_id else None)
        or grant.device_id != device_id
        or "harness:execute" not in grant.scopes
    ):
        raise IntegrationAccessDenied()
    if context.thread_id is not None and context.thread_id != thread_id:
        raise IntegrationAccessDenied()
    await validate_binding(
        db,
        user_id=context.user_id,
        organization_id=context.organization_id,
        project_id=context.project_id,
        thread_id=thread_id,
        device_id=device_id,
    )
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            WorkspaceBinding.project_id == context.project_id,
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        raise IntegrationAccessDenied()


async def bind_external_submission(
    db: AsyncSession,
    *,
    run_id: UUID,
    context: IntegrationContext,
    device_id: UUID,
    workspace_id: UUID,
) -> None:
    """Validate current authority and reserve a local writer; never commit."""
    # AgentRun uses legacy Column declarations rather than typed Mapped fields.
    run: Any = await db.scalar(
        select(AgentRun)
        .where(AgentRun.job_id == str(run_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or run.user_id != context.user_id
        or run.organization_id != context.organization_id
        or run.thread_id is None
        or (context.thread_id is not None and run.thread_id != context.thread_id)
        or (context.run_id is not None and run_id != context.run_id)
        or run.status != JobStatus.QUEUED.value
        or run.execution_provider != "nous"
    ):
        raise IntegrationAccessDenied()
    await authorize_external_submission(
        db,
        context=context,
        device_id=device_id,
        workspace_id=workspace_id,
        thread_id=run.thread_id,
    )
    existing = await db.scalar(
        select(HarnessSession.id).where(
            HarnessSession.device_id == device_id,
            HarnessSession.workspace_id == workspace_id,
            HarnessSession.workspace_locked.is_(True),
        )
    )
    if existing is not None:
        raise ActiveRunConflict("A response already owns this local workspace.")
    run.execution_provider = "codex"
    run.project_id = context.project_id
    db.add(
        HarnessSession(
            run_id=str(run_id),
            grant_id=context.grant_id,
            device_id=device_id,
            workspace_id=workspace_id,
        )
    )
    await db.flush()


async def record_observation(
    db: AsyncSession, *, run_id: UUID, observation: Observation
) -> HarnessRunDTO:
    """Record trusted native evidence; transport authenticates its exact run.

    Cancellation records intent; only interrupted evidence means cancelled.
    Completion racing Stop remains completion. Terminal outcomes are absorbing,
    including the native observation, so late packets cannot reopen a writer.
    """
    if observation not in {"unknown", "running", "completed", "failed", "interrupted"}:
        raise ValueError("Invalid harness observation")
    try:
        run: Any = await db.scalar(
            select(AgentRun)
            .where(
                AgentRun.job_id == str(run_id), AgentRun.execution_provider == "codex"
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        session = await db.scalar(
            select(HarnessSession)
            .where(HarnessSession.run_id == str(run_id))
            .execution_options(populate_existing=True)
        )
        if run is None or session is None:
            raise IntegrationAccessDenied()
        if not JobStatus(run.status).is_terminal:
            now = datetime.now(timezone.utc)
            session.observation = observation
            session.observed_at = now
            terminal = {
                "completed": (JobStatus.COMPLETED, RunEventType.RUN_COMPLETED),
                "failed": (JobStatus.FAILED, RunEventType.RUN_FAILED),
                "interrupted": (JobStatus.CANCELLED, RunEventType.RUN_CANCELLED),
            }
            if observation in terminal:
                status, event = terminal[observation]
                run.status = status.value
                run.completed_at = now
                session.workspace_locked = False
                await append_event(
                    db,
                    run_id=str(run_id),
                    event_type=event,
                    payload=(
                        {
                            "code": "harness_failed",
                            "message": "External execution failed.",
                        }
                        if observation == "failed"
                        else {}
                    ),
                    organization_id=run.organization_id,
                )
            else:
                run.status = (
                    JobStatus.RECOVERING
                    if observation == "unknown"
                    else (
                        JobStatus.STOPPING
                        if run.cancel_requested_at is not None
                        else JobStatus.RUNNING
                    )
                ).value
                if observation == "running" and run.started_at is None:
                    run.started_at = now
            run.updated_at = now
        result = HarnessRunDTO(
            status=run.status,
            workspace_locked=session.workspace_locked,
            cancel_requested=run.cancel_requested_at is not None,
        )
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
