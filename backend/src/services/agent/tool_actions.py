"""Shared fail-closed action guard for mutations requested through a harness.

Lifecycle: ``request_action`` records the intent (idempotent by invocation id
and canonical payload). ``decide_action`` binds one interactive decision to
the stored target. ``execute_action`` claims an approved row with a
conditional UPDATE, commits the claim, revalidates authority, then runs the
effect and the receipt in one transaction. A commit that fails ambiguously
leaves the row ``executing``; ``sweep_stale_actions`` moves it to
``outcome_unknown`` and nothing ever re-runs it.

Authority is bound to the consent chain (the consumed grant request), not to
one grant token: grants expire in minutes and renewal revokes the old token,
while an approval is human-paced. A grant issued without a consent request
(trusted internal issuance) is checked as itself.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.schemas.integration_tools import ToolInvocation, ToolResult
from src.schemas.tool_actions import ActionActor, ActionStatus
from src.services.integrations.context import live

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset({"create_project_note"})
# Identity comes from the session binding, never from the model's arguments.
IDENTITY_KEYS = frozenset({"project_id", "user_id", "organization_id", "thread_id"})
STALE_EXECUTION = timedelta(minutes=5)
MAX_TITLE = 255
MAX_CONTENT = 200_000
MAX_TAG = 64
REQUIRED_SCOPE = "tools:write"


class ToolActionError(Exception):
    pass


class ToolActionArgumentError(ToolActionError):
    pass


class ActionConflict(ToolActionError):
    pass


class ActionNotFound(ToolActionError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_note_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if IDENTITY_KEYS & arguments.keys():
        raise ToolActionArgumentError("identity arguments are not accepted")
    if arguments.keys() - {"title", "content", "tags"}:
        raise ToolActionArgumentError("only title, content and tags are accepted")
    title, content = arguments.get("title"), arguments.get("content")
    if not isinstance(title, str) or not title.strip():
        raise ToolActionArgumentError("title is required")
    if len(title) > MAX_TITLE:
        raise ToolActionArgumentError("title must be at most 255 characters")
    if not isinstance(content, str) or not content.strip():
        raise ToolActionArgumentError("content is required")
    if len(content) > MAX_CONTENT:
        raise ToolActionArgumentError("content must be at most 200000 characters")
    tags = arguments.get("tags", [])
    if not isinstance(tags, list) or not all(
        isinstance(t, str) and 0 < len(t) <= MAX_TAG for t in tags
    ):
        raise ToolActionArgumentError("tags must be strings of 1-64 characters")
    return {"title": title, "content": content, "tags": tags}


def _status(row: IntegrationToolAction) -> ActionStatus:
    return ActionStatus(
        invocation_id=row.invocation_id,
        state=cast(Any, row.state),
        tool_name=row.tool_name,
        result=ToolResult.model_validate(row.result) if row.result else None,
    )


def _scoped(actor: ActionActor, invocation_id: UUID) -> Any:
    query = select(IntegrationToolAction).where(
        IntegrationToolAction.organization_id == actor.organization_id,
        IntegrationToolAction.user_id == actor.user_id,
        IntegrationToolAction.invocation_id == invocation_id,
        IntegrationToolAction.is_deleted.is_(False),
    )
    if actor.consent_id is not None:
        # Any grant renewed under the same consent sees the consent's actions.
        query = query.where(IntegrationToolAction.consent_id == actor.consent_id)
    elif actor.grant_id is not None:
        query = query.where(IntegrationToolAction.grant_id == actor.grant_id)
    return query.execution_options(populate_existing=True)


async def request_action(
    db: AsyncSession, actor: ActionActor, invocation: ToolInvocation
) -> ActionStatus:
    """Record the intent once. Same id + same payload replays; a different payload conflicts."""
    if invocation.tool_name not in ALLOWED_ACTIONS:
        raise ToolActionArgumentError("tool is not available as an action")
    arguments = _validate_note_arguments(invocation.arguments)
    digest = canonical_hash(invocation.tool_name, arguments)
    existing = await db.scalar(_scoped(actor, invocation.invocation_id))
    if existing is None:
        row = IntegrationToolAction(
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            project_id=actor.project_id,
            thread_id=actor.thread_id,
            run_id=str(actor.run_id) if actor.run_id else None,
            grant_id=actor.grant_id,
            consent_id=actor.consent_id,
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            arguments=arguments,
            argument_hash=digest,
            state="awaiting_approval",
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent identical request, or the same id under another
            # consent of this user: the first writer wins, re-read it. The
            # rollback already discards the pending row.
            await db.rollback()
            logger.info(
                "integration action insert lost a race",
                extra={"invocation_id": str(invocation.invocation_id)},
            )
            existing = await db.scalar(_scoped(actor, invocation.invocation_id))
        else:
            return _status(row)
    if existing is None:
        # Same invocation id under a different consent: never disclose it.
        raise ActionConflict()
    if existing.argument_hash != digest or not _same_target(existing, actor):
        raise ActionConflict()
    return _status(existing)


def _same_target(row: IntegrationToolAction, actor: ActionActor) -> bool:
    """A replay must name the stored target; the hash excludes server bindings."""
    return bool(
        row.project_id == actor.project_id
        and row.thread_id == actor.thread_id
        and row.run_id == (str(actor.run_id) if actor.run_id else None)
    )


async def decide_action(
    db: AsyncSession, user: User, invocation_id: UUID, *, approved: bool
) -> ActionStatus:
    """Bind one decision from the interactive requester to the stored target."""
    if user.organization_id is None:
        raise ActionNotFound()
    row = await db.scalar(
        select(IntegrationToolAction)
        .where(
            IntegrationToolAction.organization_id == user.organization_id,
            IntegrationToolAction.user_id == user.id,
            IntegrationToolAction.invocation_id == invocation_id,
            IntegrationToolAction.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ActionNotFound()
    decided = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row.id,
            IntegrationToolAction.state == "awaiting_approval",
        )
        .values(
            state="approved" if approved else "failed",
            approved=approved,
            decided_by=user.id,
            decided_at=_now(),
            last_error=None if approved else "denied by user",
            updated_at=_now(),
        )
    )
    await db.commit()
    if cast(CursorResult[Any], decided).rowcount != 1:
        raise ActionConflict()  # already decided or already running
    await db.refresh(row)
    return _status(row)


async def get_action_status(
    db: AsyncSession, actor: ActionActor, invocation_id: UUID
) -> ActionStatus:
    row = await db.scalar(_scoped(actor, invocation_id))
    if row is None:
        raise ActionNotFound()
    return _status(row)


async def _authority_intact(db: AsyncSession, row: IntegrationToolAction) -> str | None:
    """Return a stable reason when the request may no longer run."""
    if row.grant_id is None:
        return None  # trusted native request; the adapter still checks the project
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.id == row.grant_id)
        .execution_options(populate_existing=True)
    )
    if (
        grant is None
        or grant.is_deleted
        or grant.user_id != row.user_id
        or grant.organization_id != row.organization_id
        or grant.project_id != row.project_id
        or REQUIRED_SCOPE not in grant.scopes
    ):
        return "grant no longer authorizes this action"
    if grant.request_id is None:
        # No consent chain: the token itself must still be live.
        if grant.revoked_at is not None or not live(grant.expires_at):
            return "grant revoked or expired"
        return None
    if row.consent_id is not None and row.consent_id != grant.request_id:
        return "grant no longer authorizes this action"
    consent = await db.scalar(
        select(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == grant.request_id,
            IntegrationGrantRequest.status == "consumed",
            IntegrationGrantRequest.user_id == row.user_id,
            IntegrationGrantRequest.organization_id == row.organization_id,
            IntegrationGrantRequest.project_id == row.project_id,
            IntegrationGrantRequest.consent_revoked_at.is_(None),
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if consent is None or REQUIRED_SCOPE not in consent.scopes:
        return "consent revoked"
    if grant.device_id is not None and consent.device_id != grant.device_id:
        return "consent revoked"
    return None


async def _run_effect(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    from src.services.agent.tools_impl import _tool_create_project_note

    # Project comes from the stored binding; the adapter re-checks edit authority.
    return await _tool_create_project_note(
        {**row.arguments, "project_id": str(row.project_id)},
        db,
        user,
        commit=False,
    )


async def _finish(
    db: AsyncSession,
    row_id: UUID,
    *,
    state: str,
    last_error: str | None,
    result: dict[str, Any] | None = None,
) -> bool:
    """Write the receipt only while the row is still ours; commit with the effect."""
    written = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row_id,
            IntegrationToolAction.state == "executing",
        )
        .values(
            state=state,
            last_error=last_error,
            result=result,
            executed_at=_now(),
            updated_at=_now(),
        )
    )
    if cast(CursorResult[Any], written).rowcount != 1:
        # The sweeper already called this outcome unknown; discard the effect.
        await db.rollback()
        return False
    # Effect and receipt commit together: a rollback here leaves no note, and
    # an ambiguous commit leaves the row `executing` for the sweeper.
    await db.commit()
    return True


async def _fail_after_rollback(
    db: AsyncSession, row_id: UUID, reason: str, result: dict[str, Any] | None = None
) -> ActionStatus | None:
    await db.rollback()
    await _finish(db, row_id, state="failed", last_error=reason, result=result)
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    return _status(row) if row is not None else None


async def _execute_row(db: AsyncSession, row_id: UUID) -> ActionStatus | None:
    now = _now()
    claimed = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row_id,
            IntegrationToolAction.state == "approved",
        )
        .values(state="executing", claimed_at=now, updated_at=now)
    )
    # Commit the claim before any effect so a second worker can never run it.
    await db.commit()
    if cast(CursorResult[Any], claimed).rowcount != 1:
        return None
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    if row is None:
        return None
    invocation = {"invocation_id": str(row.invocation_id)}

    try:
        reason = await _authority_intact(db, row)
        user = None if reason else await db.get(User, row.user_id)
        if user is None or not user.is_active or user.is_deleted:
            reason = reason or "requesting user is not active"
        elif user.organization_id != row.organization_id:
            reason = "requesting user left the organization"
    except Exception as error:  # noqa: BLE001 - the outcome is known: nothing ran
        logger.error(
            "integration action authority check failed",
            extra=invocation,
            exc_info=error,
        )
        return await _fail_after_rollback(db, row_id, "authority check failed")
    if reason or user is None:
        logger.warning("integration action denied: %s", reason, extra=invocation)
        return await _fail_after_rollback(db, row_id, reason or "denied")

    try:
        payload = await _run_effect(db, row, user)
    except Exception as error:  # noqa: BLE001 - nothing was committed
        logger.error(
            "integration action effect failed", extra=invocation, exc_info=error
        )
        return await _fail_after_rollback(db, row_id, "effect failed before commit")

    if "error" in payload:
        # The adapter reports failures as a payload and may leave the session
        # poisoned; discard any partial write with the failed receipt.
        logger.warning("integration action effect rejected", extra=invocation)
        result = ToolResult(content=[payload], is_error=True, source_refs=[])
        return await _fail_after_rollback(
            db, row_id, str(payload.get("error")), result.model_dump(mode="json")
        )

    result = ToolResult(
        content=[payload],
        is_error=False,
        source_refs=[{"note_id": payload.get("note_id")}],
    )
    if not await _finish(
        db,
        row_id,
        state="succeeded",
        last_error=None,
        result=result.model_dump(mode="json"),
    ):
        logger.error(
            "integration action finished after being marked unknown", extra=invocation
        )
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    return _status(row) if row is not None else None


async def execute_action(db: AsyncSession, invocation_id: UUID) -> ActionStatus | None:
    """Run one approved action by invocation id. Returns None when nothing was claimed."""
    row_id = await db.scalar(
        select(IntegrationToolAction.id).where(
            IntegrationToolAction.invocation_id == invocation_id,
            IntegrationToolAction.state == "approved",
            IntegrationToolAction.is_deleted.is_(False),
        )
    )
    if row_id is None:
        return None
    return await _execute_row(db, row_id)


async def drain_integration_actions(db: AsyncSession, *, limit: int = 50) -> int:
    """Execute approved rows oldest first. Returns how many finished (any outcome).

    Gated by the same kill switch as new requests: with the flag off, approved
    rows wait instead of running.
    """
    if not settings.NOUS_MCP_ENABLED:
        return 0
    row_ids = (
        await db.scalars(
            select(IntegrationToolAction.id)
            .where(
                IntegrationToolAction.state == "approved",
                IntegrationToolAction.is_deleted.is_(False),
            )
            .order_by(IntegrationToolAction.decided_at)
            .limit(limit)
        )
    ).all()
    finished = 0
    for row_id in row_ids:
        if await _execute_row(db, row_id) is not None:
            finished += 1
    return finished


async def sweep_stale_actions(db: AsyncSession) -> int:
    """An `executing` row older than STALE_EXECUTION is uncertain, never retried."""
    cutoff = _now() - STALE_EXECUTION
    swept = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.state == "executing",
            or_(
                IntegrationToolAction.claimed_at.is_(None),
                IntegrationToolAction.claimed_at < cutoff,
            ),
        )
        .values(
            state="outcome_unknown",
            last_error="worker did not confirm the outcome",
            updated_at=_now(),
        )
    )
    await db.commit()
    count = int(cast(CursorResult[Any], swept).rowcount)
    if count:
        logger.error("integration actions with unknown outcome", extra={"count": count})
    return count
