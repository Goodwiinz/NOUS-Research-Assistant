"""Shared fail-closed action guard for mutations requested through a harness.

Lifecycle: ``request_action`` records the intent (idempotent by invocation id
and canonical payload). ``decide_action`` binds one interactive decision to
the stored target. ``execute_action`` claims an approved row with a
conditional UPDATE, commits the claim, revalidates authority, then runs the
effect and the receipt in one transaction. A commit that fails ambiguously
leaves the row ``executing``; ``sweep_stale_actions`` moves it to
``outcome_unknown`` and nothing ever re-runs it.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.integration_grant import IntegrationGrant
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
    unknown = arguments.keys() - {"title", "content", "tags"}
    if unknown:
        raise ToolActionArgumentError("unknown arguments")
    title, content = arguments.get("title"), arguments.get("content")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
        raise ToolActionArgumentError("title is required")
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_CONTENT
    ):
        raise ToolActionArgumentError("content is required")
    tags = arguments.get("tags", [])
    if not isinstance(tags, list) or not all(
        isinstance(t, str) and 0 < len(t) <= 64 for t in tags
    ):
        raise ToolActionArgumentError("tags must be short strings")
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
    if actor.grant_id is not None:
        # A grant only ever sees the actions it requested.
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
            # Concurrent identical request: the first writer wins, re-read it.
            await db.rollback()
            db.expunge(row)
            existing = await db.scalar(_scoped(actor, invocation.invocation_id))
        else:
            return _status(row)
    if existing is None:
        # Same invocation id under a different grant: never disclose it.
        raise ActionConflict()
    if existing.argument_hash != digest:
        raise ActionConflict()
    return _status(existing)


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
    if row.grant_id is not None:
        grant = await db.scalar(
            select(IntegrationGrant)
            .where(IntegrationGrant.id == row.grant_id)
            .execution_options(populate_existing=True)
        )
        if (
            grant is None
            or grant.is_deleted
            or grant.revoked_at is not None
            or not live(grant.expires_at)
            or "tools:write" not in grant.scopes
            or grant.user_id != row.user_id
            or grant.organization_id != row.organization_id
            or grant.project_id != row.project_id
        ):
            return "grant revoked or no longer authorizes this action"
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


async def execute_action(db: AsyncSession, invocation_id: UUID) -> ActionStatus | None:
    """Run one approved action. Returns None when there was nothing to claim."""
    row = await db.scalar(
        select(IntegrationToolAction)
        .where(
            IntegrationToolAction.invocation_id == invocation_id,
            IntegrationToolAction.state == "approved",
            IntegrationToolAction.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    now = _now()
    claimed = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row.id,
            IntegrationToolAction.state == "approved",
        )
        .values(state="executing", claimed_at=now, updated_at=now)
    )
    # Commit the claim before any effect so a second worker can never run it.
    await db.commit()
    if cast(CursorResult[Any], claimed).rowcount != 1:
        return None
    await db.refresh(row)

    reason = await _authority_intact(db, row)
    user = None if reason else await db.get(User, row.user_id)
    if user is None or not user.is_active or user.is_deleted:
        reason = reason or "requesting user is not active"
    if reason or user is None:
        row.state, row.last_error, row.executed_at = "failed", reason, _now()
        await db.commit()
        return _status(row)

    try:
        payload = await _run_effect(db, row, user)
    except Exception as error:  # noqa: BLE001 - nothing was committed
        await db.rollback()
        row = await db.get(IntegrationToolAction, row.id, populate_existing=True)
        if row is not None:
            row.state, row.last_error = "failed", "effect failed before commit"
            row.executed_at = _now()
            await db.commit()
        logger.warning("integration action effect failed", exc_info=error)
        return _status(row) if row is not None else None

    is_error = "error" in payload
    row.result = ToolResult(
        content=[payload],
        is_error=is_error,
        source_refs=([] if is_error else [{"note_id": payload.get("note_id")}]),
    ).model_dump(mode="json")
    row.state = "failed" if is_error else "succeeded"
    row.last_error = str(payload.get("error")) if is_error else None
    row.executed_at = _now()
    # Effect and receipt commit together: a rollback here leaves no note, and
    # an ambiguous commit leaves the row `executing` for the sweeper.
    await db.commit()
    return _status(row)


async def drain_integration_actions(db: AsyncSession, *, limit: int = 50) -> int:
    """Execute approved rows oldest first. Returns how many finished (any outcome)."""
    invocation_ids = (
        await db.scalars(
            select(IntegrationToolAction.invocation_id)
            .where(
                IntegrationToolAction.state == "approved",
                IntegrationToolAction.is_deleted.is_(False),
            )
            .order_by(IntegrationToolAction.decided_at)
            .limit(limit)
        )
    ).all()
    finished = 0
    for invocation_id in invocation_ids:
        if await execute_action(db, invocation_id) is not None:
            finished += 1
    return finished


async def sweep_stale_actions(db: AsyncSession) -> int:
    """An `executing` row older than STALE_EXECUTION is uncertain, never retried."""
    swept = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.state == "executing",
            IntegrationToolAction.claimed_at < _now() - STALE_EXECUTION,
        )
        .values(
            state="outcome_unknown",
            last_error="worker did not confirm the outcome",
            updated_at=_now(),
        )
    )
    await db.commit()
    return int(cast(CursorResult[Any], swept).rowcount)
