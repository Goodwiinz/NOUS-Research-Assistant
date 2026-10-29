"""Machine handoff: a NOUS run hands a task brief to Codex on a paired machine.

The child is an ordinary Codex run (``execution_provider="codex"``) with
``parent_run_id`` set. It shares the parent's thread but never its
single-writer slot (the parent holds that as ``delegated``) and never writes
an assistant row: its reply is stored on the run for the parent's graph.

Authority is the chat path's: ``create_chat_context`` mints a short-lived
run grant from prior browser consent (project or account), and
``reserve_workspace`` locks the folder. One transaction per call; commits.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import insert_outbox
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.harness.chat_context import create_chat_context
from src.services.harness.runs import reserve_workspace
from src.services.integrations.context import IntegrationAccessDenied
from src.services.threads.workspace_access import get_thread_lean
from src.shared.enums import JobStatus

MAX_BRIEF_CHARS = 8000


async def existing_handoff_run(
    db: AsyncSession, *, parent_run_id: str, tool_call_id: str
) -> str | None:
    """The child this tool call already created, if any (node replay).

    Not an authority check: callers pass a parent run they already own.
    """
    found: str | None = await db.scalar(
        select(AgentRun.job_id).where(
            AgentRun.parent_run_id == parent_run_id,
            AgentRun.run_metadata["handoff_tool_call_id"].as_string() == tool_call_id,
        )
    )
    return found


async def create_handoff_run(
    db: AsyncSession,
    *,
    parent_run_id: str,
    current_user: Any,
    tool_call_id: str,
    brief: str,
    device_id: UUID,
    workspace_id: UUID,
) -> str:
    """Create (or find) the child Codex run for one ``run_on_machine`` call.

    Raises IntegrationAccessDenied for a parent this caller cannot hand off
    from, DeviceNotConnectedToProject / DeviceBoundToAnotherChat for a target
    outside the user's consent, ValueError for server policy or a chat with
    no project, and ActiveRunConflict for a folder in use.
    """
    try:
        # Lock the parent: concurrent calls for one tool call serialize here,
        # so the existing-child lookup below is the idempotency key.
        parent: Any = await db.scalar(
            select(AgentRun)
            .where(AgentRun.job_id == parent_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            parent is None
            or parent.user_id != current_user.id
            or parent.organization_id != current_user.organization_id
            or parent.execution_provider != "nous"
            or parent.parent_run_id is not None
            or parent.thread_id is None
        ):
            raise IntegrationAccessDenied()
        existing = await existing_handoff_run(
            db, parent_run_id=parent_run_id, tool_call_id=tool_call_id
        )
        if existing is not None:
            await db.commit()
            return existing
        if parent.status != JobStatus.RUNNING.value or not brief.strip():
            raise IntegrationAccessDenied()
        thread = await get_thread_lean(db, parent.thread_id, current_user.id)
        context = await create_chat_context(
            db,
            current_user=current_user,
            thread=thread,
            device_id=device_id,
            workspace_id=workspace_id,
        )
        child_id = str(uuid4())
        child = AgentRun(
            job_id=child_id,
            organization_id=parent.organization_id,
            user_id=parent.user_id,
            thread_id=parent.thread_id,
            conversation_id=parent.conversation_id,
            project_id=context.project_id,
            execution_provider="codex",
            status=JobStatus.QUEUED.value,
            parent_run_id=parent.job_id,
            run_metadata={
                "handoff_brief": brief[:MAX_BRIEF_CHARS],
                "handoff_tool_call_id": tool_call_id,
            },
        )
        db.add(child)
        await db.flush()
        await reserve_workspace(
            db,
            run=child,
            context=context,
            device_id=device_id,
            workspace_id=workspace_id,
        )
        await append_event(
            db,
            run_id=child_id,
            event_type=RunEventType.RUN_CREATED,
            payload={},
            organization_id=child.organization_id,
        )
        await insert_outbox(
            db,
            run_id=child_id,
            organization_id=child.organization_id,
            kind="harness.execute",
            payload={
                "execution_provider": "codex",
                "thread_id": str(child.thread_id),
                "user_id": str(child.user_id),
                "parent_run_id": parent.job_id,
            },
        )
        await db.commit()
        return child_id
    except IntegrityError:
        # uq_harness_active_workspace: another writer took the folder between
        # the pre-check and the insert.
        await db.rollback()
        raise ActiveRunConflict(
            "A response already owns this local workspace."
        ) from None
    except Exception:
        await db.rollback()
        raise
