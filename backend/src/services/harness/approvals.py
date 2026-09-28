"""Owner-mediated, one-shot decisions for exact live Codex callbacks."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.models.integration_grant import IntegrationGrant
from src.schemas.harness import BridgeEvent, NativeRequest, NativeResponseAck
from src.schemas.integration_context import IntegrationContext
from src.services.agent.run_event_store import append_event
from src.services.agent.run_event_types import RunEventType
from src.services.harness.delivery import TERMINAL, _authorize, now
from src.services.integrations.context import (
    IntegrationAccessDenied,
    IntegrationConflict,
    _validate_grant,
)
from src.shared.enums import JobStatus


class NativeRequestConflict(IntegrationConflict):
    """The callback is stale, changed, unauthorized or already consumed."""


class NativeDecision(BaseModel):
    """A strict one-time decision, or exact answers to native questions."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kind: Literal["decision", "answers"]
    approved: bool | None = Field(default=None, alias="allow")
    answers: dict[str, list[str]] | None = None
    target_hash: str | None = Field(
        default=None, alias="targetHash", pattern=r"^[0-9a-f]{64}$"
    )

    @classmethod
    def allow(cls, *, target_hash: str | None = None) -> "NativeDecision":
        return cls(kind="decision", allow=True, targetHash=target_hash)

    @classmethod
    def deny(cls, *, target_hash: str | None = None) -> "NativeDecision":
        return cls(kind="decision", allow=False, targetHash=target_hash)

    @classmethod
    def answer(
        cls, answers: dict[str, list[str]], *, target_hash: str | None = None
    ) -> "NativeDecision":
        return cls(kind="answers", answers=answers, targetHash=target_hash)

    def response(self, request: HarnessNativeRequest) -> dict[str, Any]:
        if self.target_hash is not None and self.target_hash != request.target_hash:
            raise NativeRequestConflict("Native request target changed")
        if request.method == "item/tool/requestUserInput":
            if (
                self.kind != "answers"
                or self.answers is None
                or self.approved is not None
            ):
                raise NativeRequestConflict("Answers required for this native request")
            questions = request.target.get("questions")
            if not isinstance(questions, list):
                raise NativeRequestConflict("Native questions are unavailable")
            ids = [q.get("id") for q in questions if isinstance(q, dict)]
            if (
                len(ids) != len(questions)
                or len(set(ids)) != len(ids)
                or set(self.answers) != set(ids)
                or any(
                    not isinstance(values, list)
                    or len(values) > 32
                    or any(
                        not isinstance(item, str) or len(item) > 4000 for item in values
                    )
                    for values in self.answers.values()
                )
            ):
                raise NativeRequestConflict("Answers do not match native questions")
            return {"kind": "answers", "answers": self.answers}
        if (
            self.kind != "decision"
            or type(self.approved) is not bool
            or self.answers is not None
        ):
            raise NativeRequestConflict("A one-time decision is required")
        return {"kind": "decision", "allow": self.approved}


def _request_id(value: str | int) -> tuple[str, str]:
    if isinstance(value, str) and len(value) > 255:
        raise ValueError("native callback ID exceeds limit")
    return ("string", value) if isinstance(value, str) else ("number", str(value))


def _validate_native_request(
    event: BridgeEvent, body: NativeRequest
) -> tuple[dict[str, Any], str]:
    """Allow only pinned, one-shot request families and bind their target."""
    p = body.params
    if (
        p.get("threadId") != body.sessionId
        or p.get("turnId") != body.turnId
        or p.get("itemId") != body.itemId
        or event.runId.int == 0
    ):
        raise ValueError("native callback identity mismatch")
    if p.get("approvalId") != body.approvalId:
        raise ValueError("native approval identity mismatch")
    if body.method == "item/commandExecution/requestApproval":
        allowed = {
            "threadId",
            "turnId",
            "itemId",
            "approvalId",
            "command",
            "cwd",
            "reason",
            "startedAtMs",
            "environmentId",
            "kind",
            "commandActions",
            "grantRoot",
            "proposedExecpolicyAmendment",
            "proposedNetworkPolicyAmendments",
            "networkApprovalContext",
        }
        if set(p) - allowed or any(
            p.get(key) is not None
            for key in (
                "grantRoot",
                "proposedExecpolicyAmendment",
                "proposedNetworkPolicyAmendments",
                "networkApprovalContext",
            )
        ):
            raise ValueError("persistent native permission changes are unsupported")
        if not isinstance(p.get("command"), str) or not p["command"]:
            raise ValueError("command approval target is missing")
        if len(p["command"]) > 6000 or any(
            key in p and p[key] is not None and not isinstance(p[key], str)
            for key in ("cwd", "reason", "environmentId", "kind")
        ):
            raise ValueError("invalid command approval target")
        if p.get("commandActions") is not None and not isinstance(
            p["commandActions"], list
        ):
            raise ValueError("invalid command approval actions")
    elif body.method == "item/fileChange/requestApproval":
        allowed = {"threadId", "turnId", "itemId", "reason", "startedAtMs", "grantRoot"}
        if set(p) - allowed or p.get("grantRoot") is not None:
            raise ValueError("unsupported file approval fields")
        if p.get("reason") is not None and not isinstance(p["reason"], str):
            raise ValueError("invalid file approval reason")
    elif body.method == "item/tool/requestUserInput":
        allowed = {
            "threadId",
            "turnId",
            "itemId",
            "questions",
            "isBlocking",
            "autoResolutionMs",
        }
        questions = p.get("questions")
        if (
            set(p) - allowed
            or not isinstance(p.get("isBlocking"), bool)
            or (
                p.get("autoResolutionMs") is not None
                and (
                    not isinstance(p.get("autoResolutionMs"), int)
                    or p["autoResolutionMs"] < 0
                )
            )
            or not isinstance(questions, list)
            or not questions
            or len(questions) > 16
            or any(
                not isinstance(q, dict)
                or set(q)
                - {"id", "header", "question", "options", "isOther", "isSecret"}
                or not isinstance(q.get("id"), str)
                or not q["id"]
                or not isinstance(q.get("header"), str)
                or not q["header"]
                or not isinstance(q.get("question"), str)
                or not q["question"]
                or q.get("isSecret", False) is not False
                or len(q["id"]) > 255
                or len(q["question"]) > 2000
                or len(q["header"]) > 200
                or (q.get("isOther") is not None and not isinstance(q["isOther"], bool))
                or (
                    q.get("options") is not None
                    and (
                        not isinstance(q["options"], list)
                        or len(q["options"]) > 32
                        or any(
                            not isinstance(option, dict)
                            or set(option) - {"label", "description"}
                            or not isinstance(option.get("label"), str)
                            or not isinstance(option.get("description"), str)
                            for option in q["options"]
                        )
                    )
                )
                for q in questions
            )
            or len(
                {
                    q["id"]
                    for q in questions
                    if isinstance(q, dict) and isinstance(q.get("id"), str)
                }
            )
            != len(questions)
        ):
            raise ValueError("invalid native input questions")
    else:
        raise ValueError("unsupported native request kind")
    encoded = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode()) > 8 * 1024:
        raise ValueError("native request target exceeds limit")
    return p, hashlib.sha256(encoded.encode()).hexdigest()


async def ingest_native_request(
    db: AsyncSession, context: IntegrationContext, event: BridgeEvent
) -> UUID:
    """Persist exact request and canonical display event in one transaction."""
    body = event.body
    if not isinstance(body, NativeRequest):
        raise ValueError("native request event required")
    try:
        run = await db.scalar(
            select(AgentRun)
            .where(AgentRun.job_id == str(event.runId))
            .with_for_update()
        )
        session = await db.scalar(
            select(HarnessSession)
            .where(HarnessSession.run_id == str(event.runId))
            .execution_options(populate_existing=True)
        )
        command = await db.get(HarnessCommand, event.commandId, populate_existing=True)
        if run is None or session is None or command is None:
            raise IntegrationAccessDenied()
        await _authorize(db, context, run, session)
        if (
            event.deviceId != session.device_id
            or event.workspaceId != session.workspace_id
            or event.generation != session.generation
            or command.run_id != str(event.runId)
            or command.generation != event.generation
            or command.kind != "start"
            or session.provider_session_id != body.sessionId
            or session.provider_turn_id != body.turnId
            or JobStatus(run.status).is_terminal
            or session.observation in TERMINAL
        ):
            raise NativeRequestConflict("Native callback is no longer live")
        target, target_hash = _validate_native_request(event, body)
        request_type, request_value = _request_id(body.requestId)
        duplicate = await db.scalar(
            select(HarnessNativeRequest).where(
                HarnessNativeRequest.run_id == str(event.runId),
                HarnessNativeRequest.generation == event.generation,
                HarnessNativeRequest.session_id == body.sessionId,
                HarnessNativeRequest.turn_id == body.turnId,
                HarnessNativeRequest.item_id == body.itemId,
                HarnessNativeRequest.request_id_type == request_type,
                HarnessNativeRequest.request_id_value == request_value,
            )
        )
        receipt = await db.scalar(
            select(HarnessReceipt).where(
                HarnessReceipt.run_id == str(event.runId),
                HarnessReceipt.generation == event.generation,
                HarnessReceipt.source_id == event.sourceId,
                HarnessReceipt.source_seq == event.sourceSeq,
            )
        )
        if receipt is not None:
            if receipt.digest != event._wire_digest or duplicate is None:
                raise ValueError("conflicting native request replay")
            await db.commit()
            return duplicate.id
        if (
            session.source_id not in (None, event.sourceId)
            or event.sourceSeq != session.source_seq + 1
        ):
            raise ValueError("source gap or changed source identity")
        if duplicate is not None:
            if duplicate.target_hash != target_hash:
                raise ValueError("native callback target changed")
            request = duplicate
        else:
            grant = await _validate_grant(
                db,
                await db.get(
                    IntegrationGrant, session.grant_id, populate_existing=True
                ),
            )
            expiry = min(
                grant.expires_at.replace(tzinfo=timezone.utc),
                now() + timedelta(minutes=5),
            )
            request = HarnessNativeRequest(
                id=uuid4(),
                run_id=str(event.runId),
                command_id=command.id,
                actor_id=run.user_id,
                organization_id=run.organization_id,
                project_id=run.project_id,
                device_id=session.device_id,
                workspace_id=session.workspace_id,
                grant_id=grant.id,
                generation=session.generation,
                session_id=body.sessionId,
                turn_id=body.turnId,
                item_id=body.itemId,
                request_id_type=request_type,
                request_id_value=request_value,
                approval_id=body.approvalId,
                method=body.method,
                target=target,
                target_hash=target_hash,
                expires_at=expiry,
            )
            db.add(request)
            display_name = {
                "item/commandExecution/requestApproval": "command execution",
                "item/fileChange/requestApproval": "file change",
                "item/tool/requestUserInput": "user input",
            }[body.method]
            display_args = (
                {
                    k: target.get(k)
                    for k in ("command", "cwd", "reason")
                    if target.get(k) is not None
                }
                if body.method.endswith("commandExecution/requestApproval")
                else (
                    {
                        "reason": target.get("reason"),
                    }
                    if body.method.endswith("fileChange/requestApproval")
                    else {"questions": target["questions"]}
                )
            )
            await append_event(
                db,
                run_id=run.job_id,
                event_type=RunEventType.APPROVAL_REQUIRED,
                payload={
                    "approval_id": str(request.id),
                    "tool_call_id": body.itemId,
                    "name": display_name,
                    "args": display_args,
                },
                organization_id=run.organization_id,
            )
        session.source_id = event.sourceId
        session.source_seq = event.sourceSeq
        db.add(
            HarnessReceipt(
                run_id=str(event.runId),
                generation=event.generation,
                source_id=event.sourceId,
                source_seq=event.sourceSeq,
                digest=event._wire_digest,
                canonical_seq=int(run.last_event_seq),
            )
        )
        await db.commit()
        return request.id
    except Exception:
        await db.rollback()
        raise


async def resolve_native_request(
    db: AsyncSession,
    context: IntegrationContext,
    request_id: UUID,
    decision: NativeDecision,
) -> None:
    """Atomically consume browser decision and enqueue the exact native reply."""
    try:
        request = await db.scalar(
            select(HarnessNativeRequest)
            .where(HarnessNativeRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None:
            raise IntegrationAccessDenied()
        if (
            request.actor_id != context.user_id
            or request.organization_id != context.organization_id
            or request.project_id != context.project_id
            or request.run_id
            != (str(context.run_id) if context.run_id else request.run_id)
        ):
            raise IntegrationAccessDenied()
        if (
            request.consumed_at is not None
            or request.expires_at.replace(tzinfo=timezone.utc) <= now()
        ):
            raise NativeRequestConflict("Native request expired or already consumed")
        run = await db.scalar(
            select(AgentRun).where(AgentRun.job_id == request.run_id).with_for_update()
        )
        session = await db.scalar(
            select(HarnessSession)
            .where(HarnessSession.run_id == request.run_id)
            .execution_options(populate_existing=True)
        )
        if run is None or session is None:
            raise IntegrationAccessDenied()
        current_grant = await _validate_grant(
            db, await db.get(IntegrationGrant, session.grant_id, populate_existing=True)
        )
        original_grant = await db.get(
            IntegrationGrant, request.grant_id, populate_existing=True
        )
        if (
            original_grant is None
            or original_grant.request_id is None
            or current_grant.request_id != original_grant.request_id
        ):
            raise NativeRequestConflict("Native request grant was revoked")
        await _authorize(db, context, run, session)
        if (
            session.id is None
            or session.device_id != request.device_id
            or session.workspace_id != request.workspace_id
            or context.grant_id != current_grant.id
            or session.generation != request.generation
            or session.provider_session_id != request.session_id
            or session.provider_turn_id != request.turn_id
            or session.observation != "running"
            or run.status != JobStatus.RUNNING.value
            or JobStatus(run.status).is_terminal
            or session.observation in TERMINAL
        ):
            raise NativeRequestConflict("Native callback target is no longer live")
        response = decision.response(request)
        request_id_value: str | int = (
            request.request_id_value
            if request.request_id_type == "string"
            else int(request.request_id_value)
        )
        command = HarnessCommand(
            id=uuid4(),
            run_id=request.run_id,
            generation=request.generation,
            kind="respond",
            body={
                "kind": "respond",
                "requestId": request_id_value,
                "response": response,
                "approvalRecordId": str(request.id),
            },
            expires_at=min(
                current_grant.expires_at.replace(tzinfo=timezone.utc),
                request.expires_at.replace(tzinfo=timezone.utc),
            ),
        )
        request.consumed_at = now()
        request.decision = response
        request.response_command_id = command.id
        db.add(command)
        await append_event(
            db,
            run_id=request.run_id,
            event_type=RunEventType.APPROVAL_RESOLVED,
            payload={
                "approval_id": str(request.id),
                "decision": (
                    "answers"
                    if response["kind"] == "answers"
                    else "allow" if response["allow"] else "deny"
                ),
            },
            organization_id=run.organization_id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def ingest_native_response_ack(
    db: AsyncSession, context: IntegrationContext, event: BridgeEvent
) -> int:
    """Commit local response receipt so its durable command stops leasing."""
    body = event.body
    if not isinstance(body, NativeResponseAck):
        raise ValueError("native response receipt required")
    try:
        run = await db.scalar(
            select(AgentRun)
            .where(AgentRun.job_id == str(event.runId))
            .with_for_update()
        )
        session = await db.scalar(
            select(HarnessSession)
            .where(HarnessSession.run_id == str(event.runId))
            .execution_options(populate_existing=True)
        )
        command = await db.get(HarnessCommand, event.commandId, populate_existing=True)
        request = await db.get(
            HarnessNativeRequest, body.approvalRecordId, populate_existing=True
        )
        if run is None or session is None or command is None or request is None:
            raise IntegrationAccessDenied()
        await _authorize(db, context, run, session)
        if (
            event.deviceId != session.device_id
            or event.workspaceId != session.workspace_id
            or event.generation != session.generation
            or command.run_id != str(event.runId)
            or command.generation != event.generation
            or command.kind != "respond"
            or command.body.get("approvalRecordId") != str(request.id)
            or request.response_command_id != command.id
            or request.generation != event.generation
        ):
            raise NativeRequestConflict("Native response receipt target mismatch")
        receipt = await db.scalar(
            select(HarnessReceipt).where(
                HarnessReceipt.run_id == str(event.runId),
                HarnessReceipt.generation == event.generation,
                HarnessReceipt.source_id == event.sourceId,
                HarnessReceipt.source_seq == event.sourceSeq,
            )
        )
        if receipt is not None:
            if receipt.digest != event._wire_digest:
                raise ValueError("conflicting response receipt replay")
            await db.commit()
            return int(receipt.canonical_seq or run.last_event_seq)
        if (
            session.source_id not in (None, event.sourceId)
            or event.sourceSeq != session.source_seq + 1
        ):
            raise ValueError("source gap or changed source identity")
        command.acknowledged = True
        session.source_id = event.sourceId
        session.source_seq = event.sourceSeq
        canonical_seq = int(run.last_event_seq)
        db.add(
            HarnessReceipt(
                run_id=str(event.runId),
                generation=event.generation,
                source_id=event.sourceId,
                source_seq=event.sourceSeq,
                digest=event._wire_digest,
                canonical_seq=canonical_seq,
            )
        )
        await db.commit()
        return canonical_seq
    except Exception:
        await db.rollback()
        raise


async def get_native_request(
    db: AsyncSession, request_id: UUID, actor_id: UUID
) -> dict[str, Any]:
    """Return display-only details; never expose native callback IDs or grants."""
    request = await db.scalar(
        select(HarnessNativeRequest).where(
            HarnessNativeRequest.id == request_id,
            HarnessNativeRequest.actor_id == actor_id,
        )
    )
    if request is None:
        raise IntegrationAccessDenied()
    return {
        "id": request.id,
        "run_id": UUID(request.run_id),
        "method": request.method,
        "target": request.target,
        "target_hash": request.target_hash,
        "expires_at": request.expires_at,
        "consumed": request.consumed_at is not None,
        "expired": request.expires_at.replace(tzinfo=timezone.utc) <= now(),
    }


async def native_request_context(
    db: AsyncSession, request_id: UUID, actor_id: UUID
) -> IntegrationContext:
    """Derive scoped service authority from an owner-bound challenge."""
    request = await db.scalar(
        select(HarnessNativeRequest).where(
            HarnessNativeRequest.id == request_id,
            HarnessNativeRequest.actor_id == actor_id,
        )
    )
    if request is None:
        raise IntegrationAccessDenied()
    session = await db.scalar(
        select(HarnessSession)
        .where(HarnessSession.run_id == request.run_id)
        .execution_options(populate_existing=True)
    )
    run = await db.scalar(select(AgentRun).where(AgentRun.job_id == request.run_id))
    if session is None or run is None or run.user_id != actor_id:
        raise IntegrationAccessDenied()
    return IntegrationContext(
        user_id=request.actor_id,
        organization_id=request.organization_id,
        project_id=request.project_id,
        thread_id=run.thread_id,
        run_id=UUID(request.run_id),
        grant_id=session.grant_id,
    )
