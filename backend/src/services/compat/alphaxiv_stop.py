"""Exact-run cancellation through the native run transition owner."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace
from src.services.agent.agent_run_service import get_run
from src.services.agent.agent_submission_service import (
    RunCancellationResult,
    abandon_awaiting_submission,
    commit_cancellation,
    request_run_cancellation,
)
from src.services.agent.job_store import compare_and_set_status
from src.services.compat.alphaxiv import CompatibilityError
from src.services.threads import workspace_access
from src.shared.enums import JobStatus

logger = logging.getLogger(__name__)


async def authorize_pdf_chat(
    db: AsyncSession,
    *,
    actor_id: UUID,
    chat_id: UUID,
    require_edit: bool = True,
) -> User:
    """Read fresh authority in a dedicated session; never create a thread.

    The canonical access funnel remains responsible for workspace visibility
    and all deleted ancestors. Populate existing authorization rows first so
    repeated reads in a replay session cannot retain revoked permissions.
    """
    actor: User | None = (
        await db.execute(
            select(User)
            .join(Organization, User.organization_id == Organization.id)
            .where(
                User.id == actor_id,
                User.is_active.is_(True),
                User.is_deleted.is_(False),
                Organization.is_active.is_(True),
                Organization.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if actor is None:
        raise CompatibilityError(404, "Chat not found")

    # get_thread deliberately shares the ORM identity map. Refresh its whole
    # permission chain, including memberships, before its canonical checks.
    await db.execute(
        select(Thread)
        .where(Thread.id == chat_id)
        .options(
            selectinload(Thread.conversation)
            .selectinload(Conversation.workspace)
            .selectinload(Workspace.members)
        )
        .execution_options(populate_existing=True)
    )
    thread = await workspace_access.get_thread(
        db, chat_id, actor_id, include_messages=False
    )
    if thread is None or (
        require_edit and not thread.conversation.workspace.can_user_edit(str(actor_id))
    ):
        raise CompatibilityError(404, "Chat not found")
    return actor


async def stop_pdf_chat_run(
    db: AsyncSession,
    *,
    caller: User,
    chat_id: UUID,
    run_id: str,
    request_id: str | None = None,
) -> RunCancellationResult:
    """Commit a stop for this exact authorized chat/run pair only.

    Normal stops request STOPPING; the existing producer owns cancellation
    acknowledgement. Parked runs use the native guarded abandonment path.
    Repeating a cancelled run is idempotent even after a newer run starts.
    No branch falls back to stopping whichever run is currently active.
    """
    actor_id = UUID(str(caller.id))
    parked = False

    async def claim() -> RunCancellationResult:
        nonlocal parked
        actor = await authorize_pdf_chat(db, actor_id=actor_id, chat_id=chat_id)
        run = await get_run(
            db, run_id, organization_id=actor.organization_id, user_id=actor.id
        )
        if run is None:
            raise CompatibilityError(404, "Run not found")
        await db.refresh(run)
        if (
            run.thread_id != chat_id
            or run.user_id != actor.id
            or run.organization_id != actor.organization_id
        ):
            raise CompatibilityError(404, "Run not found")
        if run.execution_provider != "nous":
            raise CompatibilityError(409, "This run cannot be stopped here")
        status = JobStatus(run.status)
        if status is JobStatus.CANCELLED:
            return RunCancellationResult(run_id, status, False)
        if status.is_terminal:
            raise CompatibilityError(409, "Run is already complete")
        if status is JobStatus.AWAITING_CONFIRMATION:
            abandoned = await abandon_awaiting_submission(
                db,
                thread_id=chat_id,
                organization_id=actor.organization_id,
                user_id=actor.id,
                reason="user_stopped_confirmation",
                expected_run_id=run_id,
            )
            if abandoned is None:
                raise CompatibilityError(409, "Run state changed; retry cancellation")
            parked = True
            return RunCancellationResult(run_id, JobStatus.CANCELLED, True)
        result = await request_run_cancellation(
            db,
            run_id=run_id,
            thread_id=chat_id,
            organization_id=actor.organization_id,
            user_id=actor.id,
            reason="user_requested",
            request_id=request_id,
        )
        if result is None:
            raise CompatibilityError(404, "Run not found")
        if result.status not in (JobStatus.STOPPING, JobStatus.CANCELLED):
            raise CompatibilityError(409, "Run state changed; retry cancellation")
        return result

    try:
        result = await commit_cancellation(db, claim())
    except CompatibilityError:
        raise
    except Exception as exc:
        logger.exception("Failed to stop compatibility PDF chat run")
        raise CompatibilityError(
            503, "Cancellation is temporarily unavailable"
        ) from exc

    if parked:
        # PostgreSQL already owns the terminal result. Redis/L1 is only the
        # same best-effort confirmation mirror used by native cancellation.
        try:
            await compare_and_set_status(
                run_id,
                JobStatus.AWAITING_CONFIRMATION,
                JobStatus.CANCELLED,
                project=False,
            )
        except Exception:
            logger.warning("Could not mirror cancelled confirmation", exc_info=True)
    return result
