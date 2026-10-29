"""Machine handoff: a NOUS run hands a task brief to Codex on a paired machine.

The child is an ordinary Codex run (``execution_provider="codex"``) with
``parent_run_id`` set. It shares the parent's thread but never its
single-writer slot (the parent holds that as ``delegated``) and never writes
an assistant row: its reply is stored on the run for the parent's graph.

Authority is the chat path's: ``create_chat_context`` mints a short-lived
run grant from prior browser consent (project or account), and
``reserve_workspace`` locks the folder. The mint commits the grant on its
own; the child, its session, event and outbox row then commit together.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_outbox import HARNESS_EXECUTE_KIND
from src.models.agent_run import AgentRun
from src.services.agent.agent_run_service import (
    ActiveRunConflict,
    integrity_constraint_name,
)
from src.services.agent.agent_submission_service import insert_outbox
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.harness.chat_context import create_chat_context
from src.services.harness.runs import reserve_workspace
from src.services.integrations.context import IntegrationAccessDenied
from src.services.threads.workspace_access import get_thread_lean
from src.shared.enums import JobStatus

MAX_BRIEF_CHARS = 8000
# agent_runs.handoff_tool_call_id is VARCHAR(255): PostgreSQL would raise a
# DataError (SQLite stores anything), so a longer id is refused up front.
MAX_TOOL_CALL_ID_CHARS = 255
HANDOFF_CALL_INDEX = "uq_agent_runs_handoff_call"
ACTIVE_WORKSPACE_INDEX = "uq_harness_active_workspace"
# SQLite does not name a partial unique index on IntegrityError; these exact
# generated key messages identify the same two indexes in the unit schema.
_SQLITE_KEY_MESSAGES = {
    HANDOFF_CALL_INDEX: (
        "unique constraint failed: "
        "agent_runs.parent_run_id, agent_runs.handoff_tool_call_id"
    ),
    ACTIVE_WORKSPACE_INDEX: (
        "unique constraint failed: "
        "harness_sessions.device_id, harness_sessions.workspace_id"
    ),
}


def _violates(exc: IntegrityError, index: str) -> bool:
    """Whether ``exc`` is a collision on exactly ``index``."""
    name = integrity_constraint_name(exc)
    if name is not None:
        return name == index
    return _SQLITE_KEY_MESSAGES[index] in str(exc.orig).lower()


async def _lock_parent(db: AsyncSession, parent_run_id: str) -> Any:
    return await db.scalar(
        select(AgentRun)
        .where(AgentRun.job_id == parent_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def existing_handoff_run(
    db: AsyncSession,
    *,
    parent_run_id: str,
    tool_call_id: str,
    user_id: Any,
    organization_id: Any,
) -> str | None:
    """The child this tool call already created, if any (node replay).

    ``uq_agent_runs_handoff_call`` keeps it unique. The owner filter is
    defense in depth: callers pass a parent run they already own.
    """
    found: str | None = await db.scalar(
        select(AgentRun.job_id).where(
            AgentRun.parent_run_id == parent_run_id,
            AgentRun.handoff_tool_call_id == tool_call_id,
            AgentRun.user_id == user_id,
            AgentRun.organization_id == organization_id,
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
    from or a blank or over-long brief, DeviceNotConnectedToProject /
    DeviceBoundToAnotherChat for a target outside the user's consent,
    ValueError for server policy or a chat that is gone or has no project,
    and ActiveRunConflict for a folder in use.

    The caller's session must have no pending work: the grant mint inside
    ``create_chat_context`` commits, and every refusal rolls back.
    """
    if not tool_call_id or len(tool_call_id) > MAX_TOOL_CALL_ID_CHARS:
        raise IntegrationAccessDenied()
    owner = {
        "user_id": current_user.id,
        "organization_id": current_user.organization_id,
    }
    try:
        # Lock the parent: concurrent calls for one tool call serialize here.
        # uq_agent_runs_handoff_call is the last word if they do not.
        parent: Any = await _lock_parent(db, parent_run_id)
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
            db, parent_run_id=parent_run_id, tool_call_id=tool_call_id, **owner
        )
        if existing is not None:
            await db.commit()
            return existing
        # The user approves exactly the brief the card shows: refuse, never cut.
        if (
            parent.status != JobStatus.RUNNING.value
            or not brief.strip()
            or len(brief) > MAX_BRIEF_CHARS
        ):
            raise IntegrationAccessDenied()
        thread = await get_thread_lean(db, parent.thread_id, current_user.id)
        if thread is None:
            raise ValueError("This chat is no longer available for a handoff.")
        context = await create_chat_context(
            db,
            current_user=current_user,
            thread=thread,
            device_id=device_id,
            workspace_id=workspace_id,
        )
        # The mint committed, which released the parent lock: take it again
        # and recheck what may have changed meanwhile.
        parent = await _lock_parent(db, parent_run_id)
        if parent is None or parent.status != JobStatus.RUNNING.value:
            raise IntegrationAccessDenied()
        existing = await existing_handoff_run(
            db, parent_run_id=parent_run_id, tool_call_id=tool_call_id, **owner
        )
        if existing is not None:
            await db.commit()
            return existing
        child_id = str(uuid4())
        child = AgentRun(
            job_id=child_id,
            organization_id=parent.organization_id,
            user_id=parent.user_id,
            thread_id=parent.thread_id,
            conversation_id=parent.conversation_id,
            # The thread's project now (the grant was minted for it), which
            # may differ from the project the parent turn started under.
            project_id=context.project_id,
            execution_provider="codex",
            status=JobStatus.QUEUED.value,
            parent_run_id=parent.job_id,
            handoff_tool_call_id=tool_call_id,
            run_metadata={"handoff_brief": brief},
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
            kind=HARNESS_EXECUTE_KIND,
            payload={
                "execution_provider": "codex",
                "thread_id": str(child.thread_id),
                "user_id": str(child.user_id),
                "parent_run_id": parent.job_id,
            },
        )
        await db.commit()
        return child_id
    except IntegrityError as exc:
        await db.rollback()
        if _violates(exc, HANDOFF_CALL_INDEX):
            # A concurrent call for the same tool call committed first.
            winner = await existing_handoff_run(
                db, parent_run_id=parent_run_id, tool_call_id=tool_call_id, **owner
            )
            await db.commit()
            if winner is not None:
                return winner
            raise
        if _violates(exc, ACTIVE_WORKSPACE_INDEX):
            # Another writer took the folder between the pre-check and insert.
            raise ActiveRunConflict(
                "A response already owns this local workspace."
            ) from None
        raise
    except Exception:
        await db.rollback()
        raise
