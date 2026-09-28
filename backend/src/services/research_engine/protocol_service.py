"""Versioned research protocol persistence and approval transactions."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_project import ResearchProject
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import (
    ProtocolDeviation,
    ProtocolRegistrationOperation,
    ResearchProtocol,
    ResearchProtocolVersion,
    ResearchQuestion,
    ResearchQuestionVersion,
)
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.schemas.research_engine import (
    ProtocolApprovalRequest,
    ProtocolDeviationCreate,
    ProtocolRegistrationCreate,
    ResearchProtocolCreate,
    ResearchProtocolVersionCreate,
    ResearchQuestionVersionCreate,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
)
from src.services.research_engine.project_access import ProjectContext

CANONICALIZATION_VERSION = "research-protocol-v1"


def canonical_hash(value: Mapping[str, Any]) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): normalize(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [normalize(nested) for nested in item]
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("protocol content must contain finite numbers")
            if item == 0:
                return 0
            if item.is_integer():
                return int(item)
        return item

    canonical = json.dumps(
        normalize(dict(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def question_content(data: ResearchQuestionVersionCreate) -> dict[str, Any]:
    return {
        "question": data.question,
        "hypothesis": data.hypothesis,
        "scope": data.scope,
        "framework": data.framework,
        "canonicalization_version": CANONICALIZATION_VERSION,
    }


def protocol_content(
    question_version_id: UUID,
    blueprint_id: UUID,
    snapshot: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "question_version_id": str(question_version_id),
        "blueprint_id": str(blueprint_id),
        "snapshot": dict(snapshot),
        "execution_plan": dict(execution_plan),
        "canonicalization_version": CANONICALIZATION_VERSION,
    }


def blueprint_execution_plan(blueprint: ResearchBlueprint) -> dict[str, Any]:
    """Capture the exact executable blueprint state reviewed by a supervisor."""
    return {
        "blueprint_id": str(blueprint.id),
        "blueprint_version": blueprint.version,
        "steps": blueprint.steps or [],
        "parameters": blueprint.parameters or {},
    }


async def _question_version(
    db: AsyncSession, version_id: UUID, collection_id: UUID
) -> ResearchQuestionVersion:
    version = cast(
        ResearchQuestionVersion | None,
        (
            await db.execute(
                select(ResearchQuestionVersion)
                .join(
                    ResearchQuestion,
                    ResearchQuestion.id == ResearchQuestionVersion.question_id,
                )
                .where(
                    ResearchQuestionVersion.id == version_id,
                    ResearchQuestion.collection_id == collection_id,
                    ResearchQuestion.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .one_or_none(),
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Question version not found")
    return version


async def _blueprint(
    db: AsyncSession, blueprint_id: UUID, collection_id: UUID
) -> ResearchBlueprint:
    blueprint = cast(
        ResearchBlueprint | None,
        (
            await db.execute(
                select(ResearchBlueprint)
                .join(
                    ResearchProject, ResearchProject.id == ResearchBlueprint.project_id
                )
                .where(
                    ResearchBlueprint.id == blueprint_id,
                    ResearchBlueprint.is_deleted.is_(False),
                    ResearchProject.collection_id == collection_id,
                    ResearchProject.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .one_or_none(),
    )
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return blueprint


async def create_question(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    data: ResearchQuestionVersionCreate,
) -> ResearchQuestion:
    question = ResearchQuestion(collection_id=context.collection.id)
    db.add(question)
    await db.flush()
    content = question_content(data)
    version = ResearchQuestionVersion(
        question_id=question.id,
        version=1,
        question=data.question,
        hypothesis=data.hypothesis,
        scope=data.scope,
        framework=data.framework,
        content_hash=canonical_hash(content),
        author_user_id=actor_user_id,
    )
    db.add(version)
    await db.flush()
    question.current_version_id = version.id
    await db.commit()
    await db.refresh(question)
    return question


async def add_question_version(
    db: AsyncSession,
    question_id: UUID,
    collection_id: UUID,
    actor_user_id: UUID,
    data: ResearchQuestionVersionCreate,
) -> ResearchQuestion:
    question = cast(
        ResearchQuestion | None,
        (
            await db.execute(
                select(ResearchQuestion)
                .where(
                    ResearchQuestion.id == question_id,
                    ResearchQuestion.collection_id == collection_id,
                    ResearchQuestion.is_deleted.is_(False),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one_or_none(),
    )
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    parent_id = data.parent_version_id or question.current_version_id
    if parent_id != question.current_version_id:
        raise HTTPException(status_code=409, detail="Question version is stale")
    latest = (
        await db.execute(
            select(ResearchQuestionVersion.version)
            .where(ResearchQuestionVersion.question_id == question.id)
            .order_by(ResearchQuestionVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()
    content = question_content(data)
    version = ResearchQuestionVersion(
        question_id=question.id,
        version=latest + 1,
        parent_version_id=parent_id,
        question=data.question,
        hypothesis=data.hypothesis,
        scope=data.scope,
        framework=data.framework,
        content_hash=canonical_hash(content),
        author_user_id=actor_user_id,
    )
    db.add(version)
    await db.flush()
    question.current_version_id = version.id
    await db.commit()
    await db.refresh(question)
    return question


async def create_protocol(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    data: ResearchProtocolCreate,
) -> ResearchProtocol:
    await _question_version(db, data.question_version_id, context.collection.id)
    blueprint = await _blueprint(db, data.blueprint_id, context.collection.id)
    protocol = ResearchProtocol(collection_id=context.collection.id, name=data.name)
    db.add(protocol)
    await db.flush()
    snapshot = data.snapshot.model_dump(mode="json")
    execution_plan = blueprint_execution_plan(blueprint)
    version = ResearchProtocolVersion(
        protocol_id=protocol.id,
        version=1,
        question_version_id=data.question_version_id,
        blueprint_id=data.blueprint_id,
        execution_plan=execution_plan,
        snapshot=snapshot,
        content_hash=canonical_hash(
            protocol_content(
                data.question_version_id,
                data.blueprint_id,
                snapshot,
                execution_plan,
            )
        ),
        status="draft",
        change_kind="initial",
        author_user_id=actor_user_id,
    )
    db.add(version)
    await db.flush()
    protocol.current_draft_version_id = version.id
    await db.commit()
    await db.refresh(protocol)
    return protocol


async def add_protocol_version(
    db: AsyncSession,
    protocol_id: UUID,
    collection_id: UUID,
    actor_user_id: UUID,
    data: ResearchProtocolVersionCreate,
) -> ResearchProtocol:
    protocol = await require_protocol(db, protocol_id, collection_id, lock=True)
    if data.parent_version_id != protocol.current_draft_version_id:
        raise HTTPException(status_code=409, detail="Protocol draft is stale")
    await _question_version(db, data.question_version_id, collection_id)
    blueprint = await _blueprint(db, data.blueprint_id, collection_id)
    parent = await require_protocol_version(db, protocol_id, data.parent_version_id)
    latest = (
        await db.execute(
            select(ResearchProtocolVersion.version)
            .where(ResearchProtocolVersion.protocol_id == protocol_id)
            .order_by(ResearchProtocolVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()
    snapshot = data.snapshot.model_dump(mode="json")
    execution_plan = blueprint_execution_plan(blueprint)
    version = ResearchProtocolVersion(
        protocol_id=protocol_id,
        version=latest + 1,
        parent_version_id=parent.id,
        question_version_id=data.question_version_id,
        blueprint_id=data.blueprint_id,
        execution_plan=execution_plan,
        snapshot=snapshot,
        content_hash=canonical_hash(
            protocol_content(
                data.question_version_id,
                data.blueprint_id,
                snapshot,
                execution_plan,
            )
        ),
        status="draft",
        change_kind="amendment",
        amendment_reason=data.amendment_reason,
        author_user_id=actor_user_id,
    )
    db.add(version)
    await db.flush()
    protocol.current_draft_version_id = version.id
    await db.commit()
    await db.refresh(protocol)
    return protocol


async def require_protocol(
    db: AsyncSession, protocol_id: UUID, collection_id: UUID, *, lock: bool = False
) -> ResearchProtocol:
    query = select(ResearchProtocol).where(
        ResearchProtocol.id == protocol_id,
        ResearchProtocol.collection_id == collection_id,
        ResearchProtocol.is_deleted.is_(False),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    protocol = cast(
        ResearchProtocol | None,
        (await db.execute(query)).scalars().one_or_none(),
    )
    if protocol is None:
        raise HTTPException(status_code=404, detail="Protocol not found")
    return protocol


async def require_protocol_version(
    db: AsyncSession, protocol_id: UUID, version_id: UUID, *, lock: bool = False
) -> ResearchProtocolVersion:
    query = select(ResearchProtocolVersion).where(
        ResearchProtocolVersion.id == version_id,
        ResearchProtocolVersion.protocol_id == protocol_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    version = cast(
        ResearchProtocolVersion | None,
        (await db.execute(query)).scalars().one_or_none(),
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Protocol version not found")
    return version


async def approve_protocol_version(
    db: AsyncSession,
    context: ProjectContext,
    protocol_id: UUID,
    version_id: UUID,
    actor_user_id: UUID,
    data: ProtocolApprovalRequest,
) -> ResearchDecisionEvent:
    if ResearchProjectRole.SUPERVISOR not in context.effective_roles:
        raise HTTPException(status_code=403, detail="supervisor role required")
    protocol = await require_protocol(db, protocol_id, context.collection.id, lock=True)
    version = await require_protocol_version(db, protocol_id, version_id, lock=True)
    fingerprint_data = {
        "protocol_id": str(protocol_id),
        "version_id": str(version_id),
        "actor_user_id": str(actor_user_id),
        **data.model_dump(mode="json"),
    }
    fingerprint = decision_request_fingerprint(fingerprint_data)
    stream = (
        await db.execute(
            select(ResearchDecisionStream.id).where(
                ResearchDecisionStream.aggregate_type == "research_protocol",
                ResearchDecisionStream.aggregate_id == protocol_id,
            )
        )
    ).scalar_one_or_none()
    if stream is not None:
        existing = cast(
            ResearchDecisionEvent | None,
            (
                await db.execute(
                    select(ResearchDecisionEvent).where(
                        ResearchDecisionEvent.stream_id == stream,
                        ResearchDecisionEvent.idempotency_key
                        == f"{data.idempotency_key}:approved",
                    )
                )
            ).scalar_one_or_none(),
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency conflict")
            return existing
    if version.author_user_id == actor_user_id:
        raise HTTPException(
            status_code=403, detail="Protocol authors cannot self-approve"
        )
    if version.status != "draft" or protocol.current_draft_version_id != version.id:
        raise HTTPException(
            status_code=409, detail="Protocol version is not current draft"
        )
    if version.version != data.expected_protocol_version:
        raise HTTPException(status_code=409, detail="Protocol version is stale")
    if version.content_hash != data.expected_content_hash:
        raise HTTPException(status_code=409, detail="Protocol content hash is stale")
    recomputed_hash = canonical_hash(
        protocol_content(
            version.question_version_id,
            version.blueprint_id,
            version.snapshot,
            version.execution_plan,
        )
    )
    if recomputed_hash != version.content_hash:
        raise HTTPException(status_code=409, detail="Protocol content hash is invalid")
    if (
        protocol.current_approved_version_id
        != data.expected_current_approved_version_id
    ):
        raise HTTPException(
            status_code=409, detail="Approved protocol pointer is stale"
        )

    previous: ResearchProtocolVersion | None = None
    if protocol.current_approved_version_id is not None:
        previous = await require_protocol_version(
            db, protocol_id, protocol.current_approved_version_id, lock=True
        )
        previous.status = "superseded"
        previous.superseded_at = datetime.now(timezone.utc)
        await append_decision(
            db,
            collection_id=context.collection.id,
            aggregate_type="research_protocol",
            aggregate_id=protocol.id,
            event_type="protocol.superseded",
            event_schema_version=1,
            actor_user_id=actor_user_id,
            actor_role="supervisor",
            subject_type="research_protocol_version",
            subject_id=previous.id,
            subject_version_id=previous.id,
            subject_hash=previous.content_hash,
            reason=data.reason,
            payload={
                "protocol_id": str(protocol.id),
                "superseded_by_version_id": str(version.id),
                "superseded_by_hash": version.content_hash,
            },
            idempotency_key=f"{data.idempotency_key}:superseded",
            request_fingerprint=fingerprint,
        )
    try:
        approved_result = await append_decision(
            db,
            collection_id=context.collection.id,
            aggregate_type="research_protocol",
            aggregate_id=protocol.id,
            event_type="protocol.approved",
            event_schema_version=1,
            actor_user_id=actor_user_id,
            actor_role="supervisor",
            subject_type="research_protocol_version",
            subject_id=version.id,
            subject_version_id=version.id,
            subject_hash=version.content_hash,
            reason=data.reason,
            payload={
                "protocol_id": str(protocol.id),
                "question_version_id": str(version.question_version_id),
                "blueprint_id": str(version.blueprint_id),
                "previous_approved_version_id": (
                    str(previous.id) if previous is not None else None
                ),
                "expected_protocol_version": data.expected_protocol_version,
                "canonicalization_version": CANONICALIZATION_VERSION,
            },
            idempotency_key=f"{data.idempotency_key}:approved",
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc
    now = datetime.now(timezone.utc)
    version.status = "approved"
    version.approved_by_user_id = actor_user_id
    version.approved_at = now
    protocol.current_approved_version_id = version.id
    await db.commit()
    await db.refresh(approved_result.event)
    return approved_result.event


async def record_registration(
    db: AsyncSession,
    protocol_id: UUID,
    collection_id: UUID,
    actor_user_id: UUID,
    data: ProtocolRegistrationCreate,
) -> ProtocolRegistrationOperation:
    version = await require_protocol_version(
        db, protocol_id, data.protocol_version_id, lock=True
    )
    if version.content_hash != data.protocol_version_hash:
        raise HTTPException(status_code=409, detail="Protocol version hash is stale")
    if version.status not in {"approved", "superseded"}:
        raise HTTPException(
            status_code=409, detail="Only approved protocols can be registered"
        )
    if data.status not in {"registered", "failed"}:
        raise HTTPException(status_code=422, detail="Invalid registration status")
    if data.status == "registered" and (
        not data.external_identifier or not data.receipt
    ):
        raise HTTPException(
            status_code=422,
            detail="Registered receipt requires external identifier and receipt",
        )
    if data.status == "failed" and not data.failure_reason:
        raise HTTPException(
            status_code=422, detail="Failed registration requires a reason"
        )
    fingerprint = canonical_hash(
        {
            "actor_user_id": str(actor_user_id),
            **data.model_dump(mode="json"),
        }
    )
    existing = cast(
        ProtocolRegistrationOperation | None,
        (
            await db.execute(
                select(ProtocolRegistrationOperation).where(
                    ProtocolRegistrationOperation.protocol_version_id
                    == data.protocol_version_id,
                    ProtocolRegistrationOperation.idempotency_key
                    == data.idempotency_key,
                )
            )
        )
        .scalars()
        .one_or_none(),
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="Idempotency conflict")
        return existing
    row = ProtocolRegistrationOperation(
        collection_id=collection_id,
        recorded_by_user_id=actor_user_id,
        request_fingerprint=fingerprint,
        **data.model_dump(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def record_deviation(
    db: AsyncSession,
    protocol_id: UUID,
    collection_id: UUID,
    actor_user_id: UUID,
    data: ProtocolDeviationCreate,
) -> ProtocolDeviation:
    version = await require_protocol_version(db, protocol_id, data.protocol_version_id)
    if version.status not in {"approved", "superseded"}:
        raise HTTPException(
            status_code=409, detail="Deviation requires an approved protocol"
        )
    if data.run_id is not None:
        run = cast(
            ResearchRun | None,
            (
                await db.execute(
                    select(ResearchRun)
                    .join(
                        ResearchBlueprint,
                        ResearchBlueprint.id == ResearchRun.blueprint_id,
                    )
                    .join(
                        ResearchProject,
                        ResearchProject.id == ResearchBlueprint.project_id,
                    )
                    .where(
                        ResearchRun.id == data.run_id,
                        ResearchRun.protocol_version_id == version.id,
                        ResearchRun.is_deleted.is_(False),
                        ResearchBlueprint.is_deleted.is_(False),
                        ResearchProject.is_deleted.is_(False),
                        ResearchProject.collection_id == collection_id,
                    )
                )
            )
            .scalars()
            .one_or_none(),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Protocol-bound run not found")
        if data.output_reference is not None:
            output_step = (
                await db.execute(
                    select(ResearchStep.id).where(
                        ResearchStep.id == data.output_reference,
                        ResearchStep.run_id == run.id,
                        ResearchStep.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if output_step is None:
                raise HTTPException(status_code=404, detail="Run output not found")
        run.conformance_status = "deviated"
    elif data.output_reference is not None:
        raise HTTPException(status_code=422, detail="output_reference requires run_id")
    row = ProtocolDeviation(
        collection_id=collection_id,
        actor_user_id=actor_user_id,
        **data.model_dump(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
