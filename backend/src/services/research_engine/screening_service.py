"""Protocol-bound screening queues, assignments and observations (GOO-301).

Every function runs inside the caller's transaction and never commits; the
route owns the one transaction. Callers pass a ``ProjectContext`` from
``resolve_project`` (Workspace SHARE -> Collection UPDATE for mutations), then
each mutation takes the queue's ``research_screening`` stream lock, replays an
identical retry, validates, inserts, and appends exactly one ledger event.
AI output only ever lands in ``screening_suggestions``.
"""

from datetime import datetime, timezone
from typing import Any, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportRecord
from src.models.research_project import ResearchProject
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.models.screening import (
    ScreeningAssignment,
    ScreeningObservation,
    ScreeningQueue,
    ScreeningSuggestion,
)
from src.models.user import User
from src.schemas.research_engine import (
    IdentityEventResponse,
    MyScreeningQueueInfo,
    MyScreeningQueueItem,
    MyScreeningQueueResponse,
    ScreeningAssignmentCreate,
    ScreeningAssignmentResponse,
    ScreeningCounts,
    ScreeningObservationCreate,
    ScreeningObservationResponse,
    ScreeningQueueCreate,
    ScreeningQueueResponse,
    ScreeningRevokeRequest,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
    replay_decisions,
)
from src.services.research_engine import screening_rules
from src.services.research_engine.identity_service import (
    _live_reports,
    _replayed_event,
    _require_role,
)
from src.services.research_engine.project_access import ProjectContext

AGGREGATE_TYPE = "research_screening"
SUBJECT_TYPE = "screening_queue"
MAX_REPORTS = 10_000

QUEUE_NOT_FOUND = "Screening queue not found"
NOT_ASSIGNED = "Not assigned to this queue"
ASSIGNMENT_REVOKED = "Assignment revoked"
# Stale-state 409s, checked in this order (queue -> protocol -> criteria -> report).
QUEUE_SUPERSEDED = "Screening queue superseded; use the reconciled queue"
PROTOCOL_CHANGED = "Protocol version changed; reconcile queue"
CRITERIA_STALE = "Screening criteria changed; reload the queue"
REPORT_MERGED = "Report merged; reconcile queue"
OBSERVATION_EXISTS = "Observation exists; supersede the current observation"


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def _flush_unique(db: AsyncSession, detail: str) -> None:
    """Flush an insert; a unique-index hit becomes a stable 409, never a 500.

    The service checks run first under the Collection lock, so this is only the
    backstop for a writer that bypassed them. The transaction stays aborted and
    the route never commits.
    """
    try:
        await db.flush()
    except IntegrityError as error:
        if "unique" not in str(error.orig).lower():
            raise
        raise _conflict(detail) from error


async def _queue(db: AsyncSession, context: ProjectContext, queue_id: UUID) -> Any:
    """The queue, scoped to this Collection; a foreign id looks missing.

    Typed ``Any`` like identity_service._live_reports: legacy ``Column``
    attributes are not assignable under mypy without the plugin.
    """
    queue = (
        await db.execute(
            select(ScreeningQueue).where(
                ScreeningQueue.id == queue_id,
                ScreeningQueue.collection_id == context.collection.id,
            )
        )
    ).scalar_one_or_none()
    if queue is None:
        raise HTTPException(status_code=404, detail=QUEUE_NOT_FOUND)
    return queue


async def _version(db: AsyncSession, version_id: UUID) -> Any:
    version = await db.get(ResearchProtocolVersion, version_id)
    assert isinstance(version, ResearchProtocolVersion)
    return version


async def _stale(db: AsyncSession, queue: Any) -> str | None:
    """Queue-level staleness, as the matching 409 detail; None when current."""
    superseded = (
        await db.execute(
            select(ScreeningQueue.id).where(
                ScreeningQueue.supersedes_queue_id == queue.id
            )
        )
    ).scalar_one_or_none()
    if superseded is not None:
        return QUEUE_SUPERSEDED
    current = (
        await db.execute(
            select(ResearchProtocol.current_approved_version_id)
            .join(
                ResearchProtocolVersion,
                ResearchProtocolVersion.protocol_id == ResearchProtocol.id,
            )
            .where(
                ResearchProtocolVersion.id == queue.protocol_version_id,
                ResearchProtocol.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if current != queue.protocol_version_id:
        return PROTOCOL_CHANGED
    return None


def _fingerprint(
    operation: str, queue_id: UUID, actor_user_id: UUID, body: Any, **extra: str
) -> str:
    return decision_request_fingerprint(
        {
            "operation": operation,
            "queue_id": str(queue_id),
            "actor_user_id": str(actor_user_id),
            **extra,
            **body.model_dump(mode="json"),
        }
    )


async def _append(
    db: AsyncSession,
    context: ProjectContext,
    *,
    queue_id: UUID,
    event_type: str,
    actor_user_id: UUID,
    actor_role: str,
    reason: str | None,
    payload: dict[str, Any],
    idempotency_key: str,
    fingerprint: str,
) -> None:
    payload = {
        "collection_id": str(context.collection.id),
        "queue_id": str(queue_id),
        **payload,
    }
    try:
        await append_decision(
            db,
            collection_id=cast(UUID, context.collection.id),
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=queue_id,
            event_type=event_type,
            event_schema_version=1,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=queue_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise _conflict("Idempotency conflict") from exc


async def _lock(
    db: AsyncSession, context: ProjectContext, queue_id: UUID
) -> ResearchDecisionStream:
    return await lock_aggregate_stream(
        db,
        collection_id=cast(UUID, context.collection.id),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=queue_id,
    )


def _current_observations() -> Any:
    """Observations no other row supersedes (the chain tips)."""
    later = aliased(ScreeningObservation)
    return select(ScreeningObservation).where(
        ~exists().where(later.supersedes_observation_id == ScreeningObservation.id)
    )


async def _queue_response(
    db: AsyncSession, queue: Any, *, suggestions_skipped: int | None = None
) -> ScreeningQueueResponse:
    # ponytail: a few count queries per queue; batch them if a project has hundreds.
    async def count(query: Any) -> int:
        return int((await db.execute(query)).scalar_one())

    current = _current_observations().where(ScreeningObservation.queue_id == queue.id)
    return ScreeningQueueResponse(
        id=queue.id,
        stage=queue.stage,
        protocol_version_id=queue.protocol_version_id,
        criteria_hash=queue.criteria_hash,
        reviewer_mode=screening_rules.reviewer_mode(
            (await _version(db, queue.protocol_version_id)).snapshot
        ),
        supersedes_queue_id=queue.supersedes_queue_id,
        created_by_id=queue.created_by_id,
        created_at=queue.created_at,
        report_count=len(queue.report_ids),
        assignment_count=await count(
            select(func.count()).where(
                ScreeningAssignment.queue_id == queue.id,
                ScreeningAssignment.revoked_at.is_(None),
            )
        ),
        observation_count=await count(
            select(func.count()).select_from(current.subquery())
        ),
        suggestion_count=await count(
            select(func.count()).where(ScreeningSuggestion.queue_id == queue.id)
        ),
        suggestions_skipped=suggestions_skipped,
        stale=await _stale(db, queue),
    )


async def _corpus(
    db: AsyncSession, collection_id: UUID, data: ScreeningQueueCreate
) -> list[UUID]:
    if data.report_ids is not None:
        report_ids = list(dict.fromkeys(data.report_ids))
        await _live_reports(db, collection_id, report_ids)
        return report_ids
    if data.stage == "full_text":
        raise HTTPException(
            status_code=422, detail="Full-text queue needs explicit report_ids"
        )
    report_ids = list(
        (
            await db.execute(
                select(ResearchReport.id)
                .where(
                    ResearchReport.collection_id == collection_id,
                    ResearchReport.merged_into_report_id.is_(None),
                )
                .order_by(ResearchReport.created_at, ResearchReport.id)
                .limit(MAX_REPORTS + 1)
            )
        )
        .scalars()
        .all()
    )
    if not report_ids:
        raise HTTPException(status_code=422, detail="Project has no reports to screen")
    if len(report_ids) > MAX_REPORTS:
        raise HTTPException(
            status_code=422, detail="Queue exceeds 10000 reports; pass report_ids"
        )
    return report_ids


async def _screen_step(db: AsyncSession, collection_id: UUID, step_id: UUID) -> Any:
    """A completed ``screen`` step of this Collection's engine project."""
    step = (
        await db.execute(
            select(ResearchStep)
            .join(ResearchRun, ResearchRun.id == ResearchStep.run_id)
            .join(ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id)
            .join(ResearchProject, ResearchProject.id == ResearchBlueprint.project_id)
            .where(
                ResearchStep.id == step_id,
                ResearchProject.collection_id == collection_id,
                ResearchStep.is_deleted.is_(False),
                ResearchRun.is_deleted.is_(False),
                ResearchBlueprint.is_deleted.is_(False),
                ResearchProject.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    if step.step_type != "screen" or step.completed_at is None:
        raise HTTPException(
            status_code=422, detail="Step is not a completed screen step"
        )
    return step


async def _import_suggestions(
    db: AsyncSession,
    collection_id: UUID,
    queue_id: UUID,
    step: Any,
    corpus: Sequence[UUID],
) -> int:
    """Attributed AI rows from a validated screen step; returns the skipped count."""
    rows = screening_rules.suggestion_rows(cast(dict[str, Any], step.output or {}))
    source_ids: dict[str, UUID] = {}
    for source_id, _decision, _reason in rows:
        try:
            source_ids[source_id] = UUID(source_id)
        except ValueError:
            continue
    reports: dict[UUID, UUID] = {}
    for observed_source, report_id, merged_into in (
        await db.execute(
            select(
                ResearchReportObservation.source_id,
                ResearchReport.id,
                ResearchReport.merged_into_report_id,
            )
            .join(
                ResearchReport, ResearchReport.id == ResearchReportObservation.report_id
            )
            .where(
                ResearchReportObservation.source_id.in_(list(source_ids.values())),
                ResearchReportObservation.collection_id == collection_id,
            )
        )
    ).all():
        # Merges re-point observations, so a merged hop is rare; follow it anyway.
        while merged_into is not None:
            report_id = merged_into
            merged_into = (
                await db.execute(
                    select(ResearchReport.merged_into_report_id).where(
                        ResearchReport.id == report_id
                    )
                )
            ).scalar_one()
        reports[observed_source] = report_id
    in_corpus = set(corpus)
    skipped = 0
    for source_id, decision, reason in rows:
        source_uuid = source_ids.get(source_id)
        report_id = None if source_uuid is None else reports.get(source_uuid)
        if report_id is None or report_id not in in_corpus:
            skipped += 1
            continue
        db.add(
            ScreeningSuggestion(
                queue_id=queue_id,
                report_id=report_id,
                source_id=source_ids[source_id],
                step_id=step.id,
                model_id=step.model_id,
                decision=decision,
                reason=reason or None,
            )
        )
    await db.flush()
    return skipped


async def create_queue(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    data: ScreeningQueueCreate,
) -> ScreeningQueueResponse:
    """Freeze a corpus at the current approved protocol version (SUPERVISE)."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    collection_id = cast(UUID, context.collection.id)
    # The idempotency key is per Collection: the queue id (and so its stream)
    # does not exist yet. The Collection lock serializes concurrent creates.
    fingerprint = _fingerprint("create_queue", collection_id, actor_user_id, data)
    prior = (
        await db.execute(
            select(ResearchDecisionEvent).where(
                ResearchDecisionEvent.collection_id == collection_id,
                ResearchDecisionEvent.event_type == "screening.queue_created",
                ResearchDecisionEvent.idempotency_key == data.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        if prior.request_fingerprint != fingerprint:
            raise _conflict("Idempotency conflict")
        payload = cast(dict[str, Any], prior.payload)
        return await _queue_response(
            db,
            await _queue(db, context, UUID(str(payload["queue_id"]))),
            suggestions_skipped=payload["suggestions_skipped"],
        )

    version = (
        await db.execute(
            select(
                ResearchProtocolVersion, ResearchProtocol.current_approved_version_id
            )
            .join(
                ResearchProtocol,
                ResearchProtocol.id == ResearchProtocolVersion.protocol_id,
            )
            .where(
                ResearchProtocolVersion.id == data.protocol_version_id,
                ResearchProtocol.collection_id == collection_id,
                ResearchProtocol.is_deleted.is_(False),
            )
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Protocol version not found")
    if version[1] != data.protocol_version_id:
        raise _conflict("Protocol version is not current")
    snapshot = cast(dict[str, Any], version[0].snapshot)
    try:
        mode = screening_rules.reviewer_mode(snapshot)
        reasons = screening_rules.exclusion_reasons(snapshot)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if data.stage == "full_text" and not reasons:
        raise HTTPException(
            status_code=422, detail="Protocol defines no full-text exclusion reasons"
        )
    if data.suggestion_step_id is not None and data.stage != "title_abstract":
        raise HTTPException(
            status_code=422, detail="Suggestions import only into title/abstract queues"
        )
    report_ids = await _corpus(db, collection_id, data)
    step = (
        None
        if data.suggestion_step_id is None
        else await _screen_step(db, collection_id, data.suggestion_step_id)
    )
    if data.supersedes_queue_id is not None:
        previous = await _queue(db, context, data.supersedes_queue_id)
        if previous.stage != data.stage:
            raise HTTPException(
                status_code=422, detail="Reconciled queue must keep its stage"
            )
        if await _stale(db, previous) == QUEUE_SUPERSEDED:
            raise _conflict("Screening queue already reconciled")

    queue_id = uuid4()
    await _lock(db, context, queue_id)
    queue = ScreeningQueue(
        id=queue_id,
        collection_id=collection_id,
        protocol_version_id=data.protocol_version_id,
        criteria_hash=screening_rules.criteria_hash(snapshot),
        stage=data.stage,
        report_ids=[str(report_id) for report_id in report_ids],
        supersedes_queue_id=data.supersedes_queue_id,
        created_by_id=actor_user_id,
    )
    db.add(queue)
    await _flush_unique(db, "Screening queue already reconciled")
    await db.refresh(queue)
    skipped = None
    if step is not None:
        skipped = await _import_suggestions(
            db, collection_id, queue_id, step, report_ids
        )
    await _append(
        db,
        context,
        queue_id=queue_id,
        event_type="screening.queue_created",
        actor_user_id=actor_user_id,
        actor_role="supervisor",
        reason=None,
        payload={
            "stage": data.stage,
            "protocol_version_id": str(data.protocol_version_id),
            "criteria_hash": queue.criteria_hash,
            "report_ids": queue.report_ids,
            "exclusion_reasons": reasons,
            "supersedes_queue_id": (
                str(data.supersedes_queue_id) if data.supersedes_queue_id else None
            ),
            "reviewer_mode": mode,
            "suggestions_skipped": skipped,
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _queue_response(db, queue, suggestions_skipped=skipped)


async def _assignment(db: AsyncSession, queue_id: UUID, assignment_id: UUID) -> Any:
    return (
        await db.execute(
            select(ScreeningAssignment).where(
                ScreeningAssignment.id == assignment_id,
                ScreeningAssignment.queue_id == queue_id,
            )
        )
    ).scalar_one_or_none()


async def _assignment_response(
    db: AsyncSession, assignment_id: UUID
) -> ScreeningAssignmentResponse:
    row = await db.get(ScreeningAssignment, assignment_id)
    assert isinstance(row, ScreeningAssignment)
    response: ScreeningAssignmentResponse = ScreeningAssignmentResponse.model_validate(
        row
    )
    return response


async def assign(
    db: AsyncSession,
    context: ProjectContext,
    queue_id: UUID,
    actor_user_id: UUID,
    data: ScreeningAssignmentCreate,
) -> ScreeningAssignmentResponse:
    """Assign a current REVIEWER to the whole queue (SUPERVISE)."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    queue = await _queue(db, context, queue_id)
    stream = await _lock(db, context, queue_id)
    fingerprint = _fingerprint("assign", queue_id, actor_user_id, data)
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        return await _assignment_response(
            db, UUID(cast(dict[str, Any], replayed.payload)["assignment_id"])
        )
    reviewer = data.reviewer_user_id
    workspace = context.workspace
    member = workspace.owner_id == reviewer or any(
        not m.is_deleted and m.user_id == reviewer for m in workspace.members
    )
    has_role = (
        await db.execute(
            select(ResearchProjectRoleAssignment.id).where(
                ResearchProjectRoleAssignment.collection_id == queue.collection_id,
                ResearchProjectRoleAssignment.user_id == reviewer,
                ResearchProjectRoleAssignment.role == ResearchProjectRole.REVIEWER,
                ResearchProjectRoleAssignment.is_deleted.is_(False),
            )
        )
    ).first() is not None
    # Same eligibility as a project role assignment (projects.py): a current
    # member of this workspace in the project's organization.
    same_org = (
        await db.execute(select(User.organization_id).where(User.id == reviewer))
    ).scalar_one_or_none() == context.organization_id
    if not (member and same_org and has_role):
        raise HTTPException(status_code=422, detail="User is not an eligible reviewer")
    active = (
        await db.execute(
            select(ScreeningAssignment.id).where(
                ScreeningAssignment.queue_id == queue_id,
                ScreeningAssignment.reviewer_id == reviewer,
                ScreeningAssignment.revoked_at.is_(None),
            )
        )
    ).first()
    if active is not None:
        raise _conflict("Reviewer already assigned")
    assignment = ScreeningAssignment(
        id=uuid4(),
        queue_id=queue_id,
        reviewer_id=reviewer,
        assigned_by_id=actor_user_id,
    )
    db.add(assignment)
    await _flush_unique(db, "Reviewer already assigned")
    await _append(
        db,
        context,
        queue_id=queue_id,
        event_type="screening.assigned",
        actor_user_id=actor_user_id,
        actor_role="supervisor",
        reason=None,
        payload={"assignment_id": str(assignment.id), "reviewer_id": str(reviewer)},
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _assignment_response(db, cast(UUID, assignment.id))


async def revoke(
    db: AsyncSession,
    context: ProjectContext,
    queue_id: UUID,
    assignment_id: UUID,
    actor_user_id: UUID,
    data: ScreeningRevokeRequest,
) -> ScreeningAssignmentResponse:
    """End an assignment revision (SUPERVISE); re-assigning inserts a new row."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    await _queue(db, context, queue_id)
    stream = await _lock(db, context, queue_id)
    fingerprint = _fingerprint(
        "revoke", queue_id, actor_user_id, data, assignment_id=str(assignment_id)
    )
    if await _replayed_event(db, stream, data.idempotency_key, fingerprint):
        return await _assignment_response(db, assignment_id)
    assignment = await _assignment(db, queue_id, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if assignment.revoked_at is not None:
        raise _conflict(ASSIGNMENT_REVOKED)
    assignment.revoked_at = datetime.now(timezone.utc)
    assignment.revoked_by_id = actor_user_id
    await db.flush()
    await _append(
        db,
        context,
        queue_id=queue_id,
        event_type="screening.unassigned",
        actor_user_id=actor_user_id,
        actor_role="supervisor",
        reason=data.reason,
        payload={
            "assignment_id": str(assignment_id),
            "reviewer_id": str(assignment.reviewer_id),
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _assignment_response(db, assignment_id)


async def list_queues(
    db: AsyncSession, context: ProjectContext
) -> list[ScreeningQueueResponse]:
    """Queues with counts only; decision breakdowns belong to GOO-302."""
    queues = (
        (
            await db.execute(
                select(ScreeningQueue)
                .where(ScreeningQueue.collection_id == context.collection.id)
                .order_by(ScreeningQueue.created_at, ScreeningQueue.id)
            )
        )
        .scalars()
        .all()
    )
    return [await _queue_response(db, queue) for queue in queues]


async def _active_assignment(db: AsyncSession, queue_id: UUID, user_id: UUID) -> Any:
    return (
        await db.execute(
            select(ScreeningAssignment).where(
                ScreeningAssignment.queue_id == queue_id,
                ScreeningAssignment.reviewer_id == user_id,
                ScreeningAssignment.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def my_queue(
    db: AsyncSession, context: ProjectContext, queue_id: UUID, user_id: UUID
) -> MyScreeningQueueResponse:
    """The caller's own view: corpus plus only their own current observations."""
    _require_role(context, ResearchProjectRole.REVIEWER)
    queue = await _queue(db, context, queue_id)
    assignment = await _active_assignment(db, queue_id, user_id)
    if assignment is None:
        raise HTTPException(status_code=403, detail=NOT_ASSIGNED)
    report_ids = [UUID(value) for value in queue.report_ids]
    titles: dict[UUID, str] = {
        report_id: title
        for report_id, title in (
            await db.execute(
                select(ResearchReport.id, ResearchReport.title_snapshot).where(
                    ResearchReport.id.in_(report_ids),
                    ResearchReport.collection_id == queue.collection_id,
                )
            )
        ).all()
    }
    identifiers: dict[UUID, dict[str, list[str]]] = {i: {} for i in report_ids}
    for report_id, kind, value in (
        await db.execute(
            select(
                ResearchReportIdentifier.report_id,
                ResearchReportIdentifier.kind,
                ResearchReportIdentifier.value,
            )
            .where(ResearchReportIdentifier.report_id.in_(report_ids))
            .order_by(ResearchReportIdentifier.kind, ResearchReportIdentifier.value)
        )
    ).all():
        identifiers[report_id].setdefault(kind, []).append(value)
    abstracts: dict[UUID, str] = {}
    for report_id, abstract in (
        await db.execute(
            select(ResearchReportObservation.report_id, ResearchSource.abstract)
            .join(
                ResearchSource, ResearchSource.id == ResearchReportObservation.source_id
            )
            .where(
                ResearchReportObservation.report_id.in_(report_ids),
                ResearchSource.abstract.is_not(None),
            )
            .order_by(
                ResearchReportObservation.created_at, ResearchReportObservation.id
            )
        )
    ).all():
        abstracts.setdefault(report_id, abstract)
    # GOO-300 fallback: an imported record's parsed abstract.
    for report_id, parsed in (
        await db.execute(
            select(ResearchImportRecord.report_id, ResearchImportRecord.parsed)
            .where(ResearchImportRecord.report_id.in_(report_ids))
            .order_by(ResearchImportRecord.created_at, ResearchImportRecord.id)
        )
    ).all():
        abstract = (parsed or {}).get("abstract")
        if isinstance(abstract, str) and abstract:
            abstracts.setdefault(report_id, abstract)
    own = {
        cast(UUID, row.report_id): ScreeningObservationResponse.model_validate(row)
        for row in (
            await db.execute(
                _current_observations().where(
                    ScreeningObservation.queue_id == queue_id,
                    ScreeningObservation.reviewer_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    }
    version = await _version(db, queue.protocol_version_id)
    items = [
        MyScreeningQueueItem(
            report_id=report_id,
            title_snapshot=titles.get(report_id, ""),
            identifiers=identifiers[report_id],
            abstract=abstracts.get(report_id),
            observation=own.get(report_id),
        )
        for report_id in report_ids
    ]
    screened = sum(item.observation is not None for item in items)
    return MyScreeningQueueResponse(
        queue=MyScreeningQueueInfo(
            id=queue.id,
            stage=queue.stage,
            protocol_version_id=queue.protocol_version_id,
            criteria_hash=queue.criteria_hash,
            exclusion_reasons=screening_rules.exclusion_reasons(version.snapshot),
            stale=await _stale(db, queue),
        ),
        assignment_id=assignment.id,
        items=items,
        counts=ScreeningCounts(
            total=len(items), screened=screened, remaining=len(items) - screened
        ),
    )


async def _observation_response(
    db: AsyncSession, observation_id: UUID
) -> ScreeningObservationResponse:
    row = await db.get(ScreeningObservation, observation_id)
    assert isinstance(row, ScreeningObservation)
    response: ScreeningObservationResponse = (
        ScreeningObservationResponse.model_validate(row)
    )
    return response


async def submit(
    db: AsyncSession,
    context: ProjectContext,
    queue_id: UUID,
    actor_user_id: UUID,
    data: ScreeningObservationCreate,
) -> ScreeningObservationResponse:
    """Record one human observation (REVIEW context plus an active assignment)."""
    _require_role(context, ResearchProjectRole.REVIEWER)
    queue = await _queue(db, context, queue_id)
    stream = await _lock(db, context, queue_id)
    fingerprint = _fingerprint("submit", queue_id, actor_user_id, data)
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        return await _observation_response(
            db, UUID(cast(dict[str, Any], replayed.payload)["observation_id"])
        )
    assignment = await _assignment(db, queue_id, data.assignment_id)
    if assignment is None or assignment.reviewer_id != actor_user_id:
        raise HTTPException(status_code=403, detail=NOT_ASSIGNED)
    if assignment.revoked_at is not None:
        raise _conflict(ASSIGNMENT_REVOKED)
    stale = await _stale(db, queue)
    if stale is not None:
        raise _conflict(stale)
    if data.criteria_hash != queue.criteria_hash:
        raise _conflict(CRITERIA_STALE)
    if str(data.report_id) not in queue.report_ids:
        raise HTTPException(status_code=404, detail="Report not found")
    report = await db.get(ResearchReport, data.report_id)
    assert isinstance(report, ResearchReport)
    if report.merged_into_report_id is not None:
        raise _conflict(REPORT_MERGED)
    reasons = screening_rules.exclusion_reasons(
        (await _version(db, queue.protocol_version_id)).snapshot
    )
    try:
        screening_rules.validate_observation(
            queue.stage, data.decision, data.exclusion_reason, reasons
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    # GOO-302 slot: 409 "Report resolved; reopen to change" goes here.
    current = (
        await db.execute(
            _current_observations().where(
                ScreeningObservation.queue_id == queue_id,
                ScreeningObservation.report_id == data.report_id,
                ScreeningObservation.reviewer_id == actor_user_id,
            )
        )
    ).scalar_one_or_none()
    current_id = None if current is None else current.id
    if data.supersedes_observation_id != current_id:
        raise _conflict(OBSERVATION_EXISTS)
    observation = ScreeningObservation(
        id=uuid4(),
        queue_id=queue_id,
        report_id=data.report_id,
        reviewer_id=actor_user_id,
        assignment_id=assignment.id,
        decision=data.decision,
        exclusion_reason=data.exclusion_reason,
        note=data.note,
        supersedes_observation_id=current_id,
    )
    db.add(observation)
    await _flush_unique(db, OBSERVATION_EXISTS)
    payload: dict[str, Any] = {
        "observation_id": str(observation.id),
        "assignment_id": str(assignment.id),
        "reviewer_id": str(actor_user_id),
        "report_id": str(data.report_id),
        "decision": data.decision,
        "exclusion_reason": data.exclusion_reason,
    }
    if current_id is not None:
        payload["superseded_observation_id"] = str(current_id)
    await _append(
        db,
        context,
        queue_id=queue_id,
        event_type=(
            "screening.observed" if current_id is None else "screening.superseded"
        ),
        actor_user_id=actor_user_id,
        actor_role="reviewer",
        reason=data.note,
        payload=payload,
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _observation_response(db, cast(UUID, observation.id))


async def history(
    db: AsyncSession, context: ProjectContext, queue_id: UUID
) -> list[IdentityEventResponse]:
    """Replay-validated screening events for one queue, in order."""
    await _queue(db, context, queue_id)
    events = await replay_decisions(
        db,
        collection_id=cast(UUID, context.collection.id),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=queue_id,
    )
    return [
        IdentityEventResponse.model_validate(event, from_attributes=True)
        for event in events
    ]
