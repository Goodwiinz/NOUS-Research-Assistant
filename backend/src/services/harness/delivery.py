"""Durable bridge delivery. PostgreSQL owns commands, dedupe and lifecycle.

A command lease only schedules delivery; it never authorizes a fresh native
start. Retransmissions retain their UUID and generation. Source receipts and
canonical events commit together, before transport acknowledges them.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.chat_message import ChatMessage
from src.models.harness_session import HarnessCommand, HarnessReceipt, HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.schemas.harness import BridgeCommand, BridgeEvent, Observation, ProducerEvent
from src.schemas.integration_context import IntegrationContext
from src.services.agent.agent_submission_service import finalize_submission
from src.services.agent.run_event_store import append_event, read_events
from src.services.agent.run_event_types import RunEventType
from src.services.harness.runs import authorize_external_submission
from src.services.integrations.context import IntegrationAccessDenied, _validate_grant
from src.shared.enums import AgentOutboxStatus, JobStatus

TERMINAL = {
    "completed": JobStatus.COMPLETED,
    "failed": JobStatus.FAILED,
    "interrupted": JobStatus.CANCELLED,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


def grant_context(grant: IntegrationGrant) -> IntegrationContext:
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        thread_id=grant.thread_id,
        run_id=UUID(grant.run_id) if grant.run_id else None,
        grant_id=grant.id,
    )


async def _authorize(
    db: AsyncSession, context: IntegrationContext, run: Any, session: HarnessSession
) -> IntegrationGrant:
    if (
        run.execution_provider != "codex"
        or run.user_id != context.user_id
        or run.organization_id != context.organization_id
        or run.project_id != context.project_id
        or (context.run_id and str(context.run_id) != run.job_id)
    ):
        raise IntegrationAccessDenied()
    await authorize_external_submission(
        db,
        context=context,
        device_id=session.device_id,
        workspace_id=session.workspace_id,
        thread_id=run.thread_id,
    )
    grant = await db.get(IntegrationGrant, context.grant_id, populate_existing=True)
    assert grant is not None
    grant = cast(IntegrationGrant, grant)
    original = await db.get(IntegrationGrant, session.grant_id, populate_existing=True)
    # Renewal is allowed only along the original browser-consent lineage.
    if (
        original is None
        or original.request_id != grant.request_id
        or original.request_id is None
    ):
        raise IntegrationAccessDenied()
    return cast(IntegrationGrant, grant)


async def _locked(db: AsyncSession, run_id: str) -> tuple[Any, HarnessSession]:
    run = await db.scalar(
        select(AgentRun)
        .where(AgentRun.job_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session = await db.scalar(
        select(HarnessSession)
        .where(HarnessSession.run_id == run_id)
        .execution_options(populate_existing=True)
    )
    if run is None or session is None:
        raise IntegrationAccessDenied()
    return run, session


async def dispatch_pending(db: AsyncSession) -> int:
    """Lease accepted outbox intents into durable device commands (no network)."""
    if not settings.HARNESS_BRIDGE_ENABLED:
        return 0

    async def terminalize(outbox: Any, run: Any, *, cancelled: bool) -> None:
        """Close an accepted run that cannot be handed to the local process."""
        outbox.status = AgentOutboxStatus.FAILED.value
        outbox.updated_at = now()
        if cancelled:
            await finalize_submission(
                db,
                run_id=run.job_id,
                status=JobStatus.CANCELLED,
                organization_id=run.organization_id,
                event_type=RunEventType.RUN_CANCELLED,
                payload={"reason": "Cancelled before local execution started."},
                provider="codex",
            )
            return
        message = "Local Codex could not be started."
        await finalize_submission(
            db,
            run_id=run.job_id,
            status=JobStatus.FAILED,
            organization_id=run.organization_id,
            event_type=RunEventType.RUN_FAILED,
            payload={"code": "harness_dispatch_failed", "message": message},
            error_code="harness_dispatch_failed",
            error=message,
            provider="codex",
        )

    try:
        rows = (
            await db.scalars(
                select(AgentOutbox)
                .where(
                    AgentOutbox.kind == "harness.execute",
                    AgentOutbox.status == "pending",
                )
                .order_by(AgentOutbox.created_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        count = 0
        for raw_outbox in rows:
            outbox: Any = raw_outbox
            run, session = await _locked(db, str(outbox.run_id))
            if JobStatus(run.status).is_terminal:
                outbox.status = AgentOutboxStatus.FAILED.value
                outbox.updated_at = now()
                session.workspace_locked = False
                continue
            if (
                run.cancel_requested_at is not None
                or JobStatus(run.status) is JobStatus.STOPPING
            ):
                await terminalize(outbox, run, cancelled=True)
                continue
            grant = await db.get(
                IntegrationGrant, session.grant_id, populate_existing=True
            )
            try:
                grant = await _validate_grant(db, grant)
                await _authorize(db, grant_context(grant), run, session)
            except IntegrationAccessDenied:
                await terminalize(outbox, run, cancelled=False)
                continue
            message: Any = (
                await db.get(ChatMessage, run.user_message_id)
                if run.user_message_id is not None
                else None
            )
            if (
                message is None
                or not isinstance(message.content, str)
                or not message.content.strip()
            ):
                await terminalize(outbox, run, cancelled=False)
                continue
            db.add(
                HarnessCommand(
                    id=uuid4(),
                    run_id=run.job_id,
                    generation=session.generation,
                    kind="start",
                    body={"kind": "start", "input": message.content},
                    expires_at=grant.expires_at,
                )
            )
            outbox.status = "dispatched"
            outbox.dispatched_at = now()
            count += 1
        await db.commit()
        return count
    except Exception:
        await db.rollback()
        raise


async def lease_commands(
    db: AsyncSession, context: IntegrationContext, device_id: UUID
) -> list[BridgeCommand]:
    """Poll across workers; recheck current grant and enqueue exact Stop intent."""
    try:
        grant = await _validate_grant(
            db, await db.get(IntegrationGrant, context.grant_id, populate_existing=True)
        )
        if grant.device_id != device_id:
            raise IntegrationAccessDenied()
        sessions = (
            await db.scalars(
                select(HarnessSession)
                .join(AgentRun, AgentRun.job_id == HarnessSession.run_id)
                .where(
                    HarnessSession.device_id == device_id,
                    AgentRun.user_id == context.user_id,
                    AgentRun.organization_id == context.organization_id,
                    AgentRun.project_id == context.project_id,
                    HarnessSession.workspace_locked.is_(True),
                    *(
                        []
                        if context.run_id is None
                        else [AgentRun.job_id == str(context.run_id)]
                    ),
                    *(
                        []
                        if context.thread_id is None
                        else [AgentRun.thread_id == context.thread_id]
                    ),
                )
                .limit(100)
            )
        ).all()
        result = []
        for candidate in sessions:
            try:
                run, session = await _locked(db, candidate.run_id)
                await _authorize(db, context, run, session)
            except IntegrationAccessDenied:
                # One unauthorized session must not starve the device's others.
                # Stay locked: releasing needs terminal evidence only the
                # reconcile/projection path has.
                continue
            session.grant_id = grant.id
            if (
                run.cancel_requested_at
                and session.provider_session_id
                and session.provider_turn_id
            ):
                existing = await db.scalar(
                    select(HarnessCommand.id).where(
                        HarnessCommand.run_id == run.job_id,
                        HarnessCommand.generation == session.generation,
                        HarnessCommand.kind == "interrupt",
                    )
                )
                if existing is None:
                    db.add(
                        HarnessCommand(
                            run_id=run.job_id,
                            generation=session.generation,
                            kind="interrupt",
                            body={
                                "kind": "interrupt",
                                "sessionId": session.provider_session_id,
                                "turnId": session.provider_turn_id,
                            },
                            expires_at=grant.expires_at,
                        )
                    )
                    await db.flush()
            commands = (
                await db.scalars(
                    select(HarnessCommand)
                    .where(
                        HarnessCommand.run_id == run.job_id,
                        HarnessCommand.generation == session.generation,
                        HarnessCommand.acknowledged.is_(False),
                        or_(
                            HarnessCommand.lease_until.is_(None),
                            HarnessCommand.lease_until < now(),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for command in commands:
                if command.kind == "start" and (
                    not settings.HARNESS_BRIDGE_ENABLED or run.cancel_requested_at
                ):
                    continue
                # Expiry renewal does not change a native-action identity.
                command.expires_at = grant.expires_at
                command.lease_until = min(
                    utc(grant.expires_at), now() + timedelta(seconds=15)
                )
                result.append(
                    BridgeCommand(
                        deviceId=device_id,
                        runId=UUID(run.job_id),
                        commandId=command.id,
                        workspaceId=session.workspace_id,
                        generation=session.generation,
                        expiresAt=utc(command.expires_at),
                        body=cast(Any, command.body),
                    )
                )
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def ingest_bridge_event(
    db: AsyncSession, context: IntegrationContext, event: BridgeEvent
) -> int:
    """Return canonical cursor only after commit and any required projection."""
    run_id = str(event.runId)
    try:
        run, session = await _locked(db, run_id)
        await _authorize(db, context, run, session)
        command = await db.get(HarnessCommand, event.commandId, populate_existing=True)
        if (
            event.deviceId != session.device_id
            or event.workspaceId != session.workspace_id
            or event.generation != session.generation
            or command is None
            or command.run_id != run_id
            or command.generation != event.generation
        ):
            raise IntegrationAccessDenied()
        receipt = await db.scalar(
            select(HarnessReceipt).where(
                HarnessReceipt.run_id == run_id,
                HarnessReceipt.generation == event.generation,
                HarnessReceipt.source_id == event.sourceId,
                HarnessReceipt.source_seq == event.sourceSeq,
            )
        )
        if receipt is not None:
            if receipt.digest != event._wire_digest:
                raise ValueError("conflicting source replay")
            if receipt.canonical_seq is not None:
                await db.commit()
                assert isinstance(receipt.canonical_seq, int)
                return receipt.canonical_seq
            await db.commit()
            return await project_terminal(db, run_id)
        if (
            session.source_id not in (None, event.sourceId)
            or event.sourceSeq != session.source_seq + 1
        ):
            raise ValueError("source gap or changed source identity")
        if JobStatus(run.status).is_terminal or session.observation in TERMINAL:
            raise ValueError("terminal source is closed")
        body = event.body
        seq = int(run.last_event_seq)
        pending_projection = False
        if isinstance(body, ProducerEvent):
            if command.kind != "start":
                raise IntegrationAccessDenied()
            row = await append_event(
                db,
                run_id=run_id,
                event_type=body.eventType,
                payload=body.payload.model_dump(mode="json", exclude_none=True),
                organization_id=run.organization_id,
            )
            seq = int(row.seq)
        else:
            if not isinstance(body, Observation):
                raise IntegrationAccessDenied()
            if (
                session.provider_session_id not in (None, body.sessionId)
                or (
                    session.provider_turn_id is not None
                    and session.provider_turn_id != body.turnId
                )
                or (body.state != "unknown" and not body.turnId)
            ):
                raise ValueError("native identity mismatch")
            if command.kind == "interrupt" and (
                command.body["sessionId"] != body.sessionId
                or command.body["turnId"] != body.turnId
            ):
                raise ValueError("interrupt target mismatch")
            session.provider_session_id = body.sessionId
            session.provider_turn_id = body.turnId
            session.observation = body.state
            session.observed_at = now()
            if body.state in TERMINAL:
                pending_projection = True
                run.status = JobStatus.RECOVERING.value
            elif body.state == "unknown":
                run.status = JobStatus.RECOVERING.value
            else:
                if run.started_at is None:
                    row = await append_event(
                        db,
                        run_id=run_id,
                        event_type=RunEventType.RUN_STARTED,
                        payload={},
                        organization_id=run.organization_id,
                    )
                    seq = int(row.seq)
                    run.started_at = now()
                run.status = (
                    JobStatus.STOPPING.value
                    if run.cancel_requested_at
                    else JobStatus.RUNNING.value
                )
            run.updated_at = now()
        session.source_id = event.sourceId
        session.source_seq = event.sourceSeq
        command.acknowledged = True
        db.add(
            HarnessReceipt(
                run_id=run_id,
                generation=event.generation,
                source_id=event.sourceId,
                source_seq=event.sourceSeq,
                digest=event._wire_digest,
                canonical_seq=None if pending_projection else seq,
            )
        )
        await db.commit()
        if pending_projection:
            return await project_terminal(db, run_id)
        return seq
    except Exception:
        await db.rollback()
        raise


async def project_terminal(db: AsyncSession, run_id: str) -> int:
    """Retryable canonical transcript projection, independent of any browser.

    Source terminal evidence commits first, sealing the source. Release the row
    lock before the existing assistant writer opens its own transaction; then
    reacquire it for finalization. The client_message_id makes competing retries
    converge on the same assistant row.
    """
    run, session = await _locked(db, run_id)
    if session.observation not in TERMINAL:
        raise ValueError("terminal evidence required")
    state = session.observation
    org, user, thread = run.organization_id, run.user_id, str(run.thread_id)
    terminal_already = JobStatus(run.status).is_terminal
    content: list[str] = []
    cursor = 0
    while not terminal_already:
        rows = await read_events(
            db, run_id, organization_id=org, user_id=user, after_seq=cursor, limit=500
        )
        if not rows:
            break
        for row in rows:
            if row.event_type == RunEventType.ASSISTANT_DELTA.value:
                content.append(cast(dict[str, Any], row.payload)["text"])
        cursor = int(rows[-1].seq)
    await db.commit()
    if not terminal_already:
        from src.services.agent.agent_execution_service import (
            _persist_assistant_message_safe,
        )

        # A turn that fails or is interrupted before any assistant text leaves
        # no deltas. ChatMessageResponse requires non-empty content, so an empty
        # row would 500 every subsequent read of the thread (GET .../messages).
        text = (
            "".join(content)
            or {
                "failed": "External execution failed.",
                "interrupted": "External execution was stopped.",
                "completed": "External execution completed without output.",
            }[state]
        )
        assistant_id = await _persist_assistant_message_safe(
            thread_id=thread,
            content=text,
            model_name="codex",
            tool_executions_out=None,
            stopped=state == "interrupted",
            required=True,
            client_message_id=run_id,
        )
        if not assistant_id:
            raise RuntimeError("assistant projection required")
        run, session = await _locked(db, run_id)
        if not JobStatus(run.status).is_terminal:
            payload: dict[str, Any] = (
                {"code": "harness_failed", "message": "External execution failed."}
                if state == "failed"
                else {"assistant_message_id": assistant_id}
            )
            await finalize_submission(
                db,
                run_id=run_id,
                status=TERMINAL[state],
                organization_id=org,
                event_type={
                    "completed": RunEventType.RUN_COMPLETED,
                    "failed": RunEventType.RUN_FAILED,
                    "interrupted": RunEventType.RUN_CANCELLED,
                }[state],
                payload=payload,
                provider="codex",
            )
    run, _ = await _locked(db, run_id)
    if not JobStatus(run.status).is_terminal:
        raise RuntimeError("terminal projection incomplete")
    seq = int(run.last_event_seq)
    receipts = (
        await db.scalars(
            select(HarnessReceipt).where(
                HarnessReceipt.run_id == run_id, HarnessReceipt.canonical_seq.is_(None)
            )
        )
    ).all()
    for receipt in receipts:
        receipt.canonical_seq = seq
    await db.commit()
    return seq


async def reconcile_pending(db: AsyncSession) -> int:
    ids = list(
        (
            await db.scalars(
                select(HarnessSession.run_id)
                .join(AgentRun, AgentRun.job_id == HarnessSession.run_id)
                .where(
                    HarnessSession.observation.in_(TERMINAL),
                    HarnessSession.workspace_locked.is_(True),
                )
                .limit(100)
            )
        ).all()
    )
    await db.commit()
    count = 0
    for run_id in ids:
        try:
            await project_terminal(db, run_id)
            count += 1
        except Exception:
            await db.rollback()
            # Leave the sealed source and workspace lock intact for the next tick.
    return count


async def lease_runs(
    db: AsyncSession, context: IntegrationContext, device_id: UUID
) -> dict[str, int]:
    """Exact live run set a grant may renew; device-wide renewal is unsafe."""
    try:
        grant = await _validate_grant(
            db, await db.get(IntegrationGrant, context.grant_id, populate_existing=True)
        )
        if grant.device_id != device_id:
            raise IntegrationAccessDenied()
        sessions = (
            await db.scalars(
                select(HarnessSession)
                .join(AgentRun, AgentRun.job_id == HarnessSession.run_id)
                .where(
                    HarnessSession.device_id == device_id,
                    HarnessSession.workspace_locked.is_(True),
                    AgentRun.user_id == context.user_id,
                    AgentRun.organization_id == context.organization_id,
                    AgentRun.project_id == context.project_id,
                    *(
                        []
                        if context.run_id is None
                        else [AgentRun.job_id == str(context.run_id)]
                    ),
                    *(
                        []
                        if context.thread_id is None
                        else [AgentRun.thread_id == context.thread_id]
                    ),
                )
                .limit(100)
            )
        ).all()
        result: dict[str, int] = {}
        for session in sessions:
            run: Any = await db.get(AgentRun, session.run_id, populate_existing=True)
            try:
                await _authorize(db, context, run, session)
            except IntegrationAccessDenied:
                continue  # Skipped, still locked; see lease_commands.
            result[session.run_id] = session.generation
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
