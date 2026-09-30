"""Protocol-bound screening queues, assignments and observations (GOO-301).

Every function runs inside the caller's transaction and never commits; the
route owns the one transaction. Callers pass a ``ProjectContext`` from
``resolve_project`` (Workspace SHARE -> Collection UPDATE for mutations), then
each mutation takes the queue's ``research_screening`` stream lock, replays an
identical retry, validates, inserts, and appends exactly one ledger event.
AI output only ever lands in ``screening_suggestions``.

GOO-302 (blind dual review): a report is revealed in a queue iff it has a
``screening_resolutions`` row. That row is written in the same transaction as
the submission that brings its fresh observations up to the mode's required
count, and another reviewer's observation is visible only if it is an input
of such a row (``visible_observation_ids``, the one predicate for every read
path). Adjudication and reopen are adjudicator ledger events that insert a new
chain tip; no observation or resolution is ever updated.
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
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
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
    ScreeningResolution,
    ScreeningSuggestion,
)
from src.models.user import User
from src.schemas.research_engine import (
    MyScreeningQueueInfo,
    MyScreeningQueueItem,
    MyScreeningQueueResponse,
    ScreeningAdjudicateRequest,
    ScreeningAssignmentCreate,
    ScreeningAssignmentResponse,
    ScreeningConflictResponse,
    ScreeningCounts,
    ScreeningEventResponse,
    ScreeningObservationCreate,
    ScreeningObservationResponse,
    ScreeningQueueCreate,
    ScreeningQueueResponse,
    ScreeningReopenRequest,
    ScreeningResolutionResponse,
    ScreeningRevokeRequest,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    DecisionReplayError,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
    replay_decisions,
)
from src.services.research_decisions.ledger import replay_screening_resolutions
from src.services.research_engine import screening_rules
from src.services.research_engine.corpus_service import raw_allowed, visible_parsed
from src.services.research_engine.identity_service import (
    _replayed_event,
    _require_role,
    live_reports,
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
# GOO-302 reveal / adjudication.
REPORT_RESOLVED = "Report resolved; reopen to change"
INPUTS_STALE = "Adjudication inputs are stale"
NOT_IN_CONFLICT = "Report is not in conflict"
NOT_RESOLVED = "Report is not resolved"
RESOLUTION_STALE = "Resolution changed; reload the queue"
SELF_ADJUDICATION = "Adjudicator reviewed this report"
_RESOLVED_BASES = ("single", "agreement", "adjudicated")
_OBSERVATION_EVENTS = ("screening.observed", "screening.superseded")


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
        if not _is_unique_violation(error):
            raise
        raise _conflict(detail) from error


def _is_unique_violation(error: IntegrityError) -> bool:
    """PostgreSQL SQLSTATE 23505, or SQLite's unique-constraint error code."""
    orig = error.orig
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return (
        sqlstate == "23505"
        or getattr(orig, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
    )


async def _queue(db: AsyncSession, context: ProjectContext, queue_id: UUID) -> Any:
    """The queue, scoped to this Collection; a foreign id looks missing.

    Typed ``Any`` like identity_service.live_reports: legacy ``Column``
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
) -> ResearchDecisionEvent:
    payload = {
        "collection_id": str(context.collection.id),
        "queue_id": str(queue_id),
        **payload,
    }
    try:
        result = await append_decision(
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
    return result.event


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


def _resolution_tips() -> Any:
    """Resolutions no later row supersedes: each report's current resolution."""
    later = aliased(ScreeningResolution)
    return select(ScreeningResolution).where(
        ~exists().where(later.supersedes_resolution_id == ScreeningResolution.id)
    )


async def _tips(db: AsyncSession, queue_id: UUID) -> dict[UUID, Any]:
    """report_id -> current resolution, for the revealed reports of a queue."""
    rows = await db.execute(
        _resolution_tips().where(ScreeningResolution.queue_id == queue_id)
    )
    return {cast(UUID, row.report_id): row for row in rows.scalars()}


async def _tip(db: AsyncSession, queue_id: UUID, report_id: UUID) -> Any:
    return (
        await db.execute(
            _resolution_tips().where(
                ScreeningResolution.queue_id == queue_id,
                ScreeningResolution.report_id == report_id,
            )
        )
    ).scalar_one_or_none()


async def _revealed_ids(
    db: AsyncSession, queue_id: UUID, report_id: UUID | None = None
) -> set[UUID]:
    """Every observation id named as an input of any resolution row."""
    query = select(ScreeningResolution.input_observation_ids).where(
        ScreeningResolution.queue_id == queue_id
    )
    if report_id is not None:
        query = query.where(ScreeningResolution.report_id == report_id)
    return {
        UUID(str(value))
        for inputs in (await db.execute(query)).scalars()
        for value in inputs
    }


async def visible_observation_ids(
    db: AsyncSession, queue_id: UUID, viewer_id: UUID
) -> set[UUID]:
    """THE reveal predicate: the viewer's own observations plus every input of
    a resolution row in this queue. Every read path that could expose another
    reviewer's decision (mine, history, conflicts, and anything added later)
    filters through this set."""
    own = (
        await db.execute(
            select(ScreeningObservation.id).where(
                ScreeningObservation.queue_id == queue_id,
                ScreeningObservation.reviewer_id == viewer_id,
            )
        )
    ).scalars()
    return {cast(UUID, value) for value in own} | await _revealed_ids(db, queue_id)


def _resolution_response(row: Any) -> ScreeningResolutionResponse:
    response: ScreeningResolutionResponse = ScreeningResolutionResponse.model_validate(
        row
    )
    return response


async def _derive_and_record(
    db: AsyncSession, queue: Any, report_id: UUID, event: ResearchDecisionEvent
) -> Any:
    """Derive (``screening_rules.derive``) and insert the report's resolution
    once its fresh observations reach the required count; None otherwise.

    Runs under the queue's stream lock right after the triggering observation
    and its event, so exactly one submission reveals a report.
    """
    consumed = await _revealed_ids(db, queue.id, report_id)
    fresh = [
        screening_rules.Obs(
            cast(UUID, row.id),
            cast(UUID, row.reviewer_id),
            cast(str, row.decision),
            cast(str | None, row.exclusion_reason),
        )
        for row in (
            await db.execute(
                _current_observations().where(
                    ScreeningObservation.queue_id == queue.id,
                    ScreeningObservation.report_id == report_id,
                )
            )
        ).scalars()
        if row.id not in consumed
    ]
    mode = screening_rules.reviewer_mode(
        (await _version(db, queue.protocol_version_id)).snapshot
    )
    derived = screening_rules.derive(mode, fresh)
    if derived is None:
        return None
    tip = await _tip(db, queue.id, report_id)
    event_id = cast(UUID, event.id)
    row = ScreeningResolution(
        id=screening_rules.auto_resolution_id(event_id),
        queue_id=queue.id,
        report_id=report_id,
        basis=derived.basis,
        outcome=derived.outcome,
        exclusion_reason=derived.exclusion_reason,
        input_observation_ids=derived.input_observation_ids,
        criteria_hash=queue.criteria_hash,
        supersedes_resolution_id=None if tip is None else tip.id,
        event_id=event_id,
    )
    db.add(row)
    await _flush_unique(db, REPORT_RESOLVED)
    return row


async def _queue_response(
    db: AsyncSession, queue: Any, *, suggestions_skipped: int | None = None
) -> ScreeningQueueResponse:
    # ponytail: a few count queries per queue; batch them if a project has hundreds.
    async def count(query: Any) -> int:
        return int((await db.execute(query)).scalar_one())

    current = _current_observations().where(ScreeningObservation.queue_id == queue.id)
    tips = (await _tips(db, queue.id)).values()
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
        resolved_count=sum(tip.basis in _RESOLVED_BASES for tip in tips),
        conflict_count=sum(tip.basis == "conflict" for tip in tips),
    )


async def _corpus(
    db: AsyncSession, collection_id: UUID, data: ScreeningQueueCreate
) -> list[UUID]:
    if data.report_ids is not None:
        report_ids = list(dict.fromkeys(data.report_ids))
        await live_reports(db, collection_id, report_ids)
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
    """Queues with counts only; resolved/conflict counts cover revealed rows."""
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
    """The caller's own view: corpus, own current observations, and peers'
    current observations only where ``visible_observation_ids`` allows."""
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
    # GOO-300 fallback: an imported record's parsed abstract, only where its
    # receipt allows redistribution (restricted text never leaves the server).
    for report_id, parsed, receipt in (
        await db.execute(
            select(
                ResearchImportRecord.report_id,
                ResearchImportRecord.parsed,
                ResearchImportReceipt,
            )
            .join(
                ResearchImportReceipt,
                ResearchImportReceipt.id == ResearchImportRecord.receipt_id,
            )
            .where(ResearchImportRecord.report_id.in_(report_ids))
            .order_by(ResearchImportRecord.created_at, ResearchImportRecord.id)
        )
    ).all():
        abstract = visible_parsed(parsed or {}, raw_allowed(receipt)).get("abstract")
        if isinstance(abstract, str) and abstract:
            abstracts.setdefault(report_id, abstract)
    visible = await visible_observation_ids(db, queue_id, user_id)
    own: dict[UUID, ScreeningObservationResponse] = {}
    others: dict[UUID, list[ScreeningObservationResponse]] = {}
    for row in (
        await db.execute(
            _current_observations()
            .where(ScreeningObservation.queue_id == queue_id)
            .order_by(ScreeningObservation.created_at, ScreeningObservation.id)
        )
    ).scalars():
        report_id = cast(UUID, row.report_id)
        if row.reviewer_id == user_id:
            own[report_id] = ScreeningObservationResponse.model_validate(row)
        elif screening_rules.visible(row.reviewer_id, row.id, user_id, visible):
            others.setdefault(report_id, []).append(
                ScreeningObservationResponse.model_validate(row)
            )
    tips = await _tips(db, queue_id)
    version = await _version(db, queue.protocol_version_id)
    items = [
        MyScreeningQueueItem(
            report_id=report_id,
            title_snapshot=titles.get(report_id, ""),
            identifiers=identifiers[report_id],
            abstract=abstracts.get(report_id),
            observation=own.get(report_id),
            reveal_state="revealed" if report_id in tips else "hidden",
            others=others.get(report_id, []),
            resolution=(
                _resolution_response(tips[report_id]) if report_id in tips else None
            ),
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
            total=len(items),
            screened=screened,
            remaining=len(items) - screened,
            revealed=len(tips),
            conflicts=sum(tip.basis == "conflict" for tip in tips.values()),
        ),
    )


async def _observation_response(
    db: AsyncSession, observation_id: UUID, event_id: UUID
) -> ScreeningObservationResponse:
    """The observation, plus the resolution its event revealed (if any)."""
    row = await db.get(ScreeningObservation, observation_id)
    assert isinstance(row, ScreeningObservation)
    response: ScreeningObservationResponse = (
        ScreeningObservationResponse.model_validate(row)
    )
    resolution = (
        await db.execute(
            select(ScreeningResolution).where(ScreeningResolution.event_id == event_id)
        )
    ).scalar_one_or_none()
    if resolution is not None:
        response.resolution = _resolution_response(resolution)
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
            db,
            UUID(cast(dict[str, Any], replayed.payload)["observation_id"]),
            cast(UUID, replayed.id),
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
    # Step 4a (GOO-302): reveal is irreversible; changing a revealed vote
    # needs an adjudicator's recorded reopen first.
    tip = await _tip(db, queue_id, data.report_id)
    if tip is not None and tip.basis != "reopened":
        raise _conflict(REPORT_RESOLVED)
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
    event = await _append(
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
    await _derive_and_record(db, queue, data.report_id, event)
    return await _observation_response(
        db, cast(UUID, observation.id), cast(UUID, event.id)
    )


async def history(
    db: AsyncSession, context: ProjectContext, queue_id: UUID, user_id: UUID
) -> list[ScreeningEventResponse]:
    """Replay-validated screening events for one queue, in order (VIEW).

    Every automatic resolution row is re-derived from the events; drift raises
    ``DecisionReplayError``. An observation event the caller cannot see (per
    ``visible_observation_ids``) loses its decision, exclusion reason and note
    and is marked ``redacted``; no hash fields are exposed.
    """
    await _queue(db, context, queue_id)
    events = await replay_decisions(
        db,
        collection_id=cast(UUID, context.collection.id),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=queue_id,
    )
    await _check_resolutions(db, queue_id, events)
    visible = await visible_observation_ids(db, queue_id, user_id)
    responses: list[ScreeningEventResponse] = []
    for event in events:
        response = ScreeningEventResponse.model_validate(event, from_attributes=True)
        payload = response.payload
        if event.event_type in _OBSERVATION_EVENTS and (
            UUID(str(payload["observation_id"])) not in visible
        ):
            response.payload = {
                key: value
                for key, value in payload.items()
                if key not in ("decision", "exclusion_reason")
            }
            response.reason = None
            response.redacted = True
        responses.append(response)
    return responses


async def _check_resolutions(
    db: AsyncSession, queue_id: UUID, events: Sequence[ResearchDecisionEvent]
) -> None:
    """Stored resolution rows must equal the ones replay derives from events."""

    def key(row: Any) -> tuple[Any, ...]:
        return (
            str(row.id),
            str(row.event_id),
            str(row.report_id),
            row.basis,
            row.outcome,
            row.exclusion_reason,
            sorted(str(i) for i in row.input_observation_ids),
            (
                None
                if row.supersedes_resolution_id is None
                else str(row.supersedes_resolution_id)
            ),
        )

    stored = (
        await db.execute(
            select(ScreeningResolution).where(ScreeningResolution.queue_id == queue_id)
        )
    ).scalars()
    replayed = replay_screening_resolutions(events, queue_id)
    if sorted(map(key, stored)) != sorted(map(key, replayed)):
        raise DecisionReplayError("screening resolution does not match inputs")


async def conflicts(
    db: AsyncSession, context: ProjectContext, queue_id: UUID, user_id: UUID
) -> list[ScreeningConflictResponse]:
    """Revealed conflicts awaiting a human adjudicator (VIEW + ADJUDICATOR)."""
    _require_role(context, ResearchProjectRole.ADJUDICATOR)
    queue = await _queue(db, context, queue_id)
    tips = {
        report_id: tip
        for report_id, tip in (await _tips(db, queue_id)).items()
        if tip.basis == "conflict"
    }
    if not tips:
        return []
    visible = await visible_observation_ids(db, queue_id, user_id)
    inputs = {UUID(str(i)) for tip in tips.values() for i in tip.input_observation_ids}
    observations = {
        cast(UUID, row.id): row
        for row in (
            await db.execute(
                select(ScreeningObservation).where(
                    ScreeningObservation.id.in_(inputs & visible)
                )
            )
        ).scalars()
    }
    titles = dict(
        (
            await db.execute(
                select(ResearchReport.id, ResearchReport.title_snapshot).where(
                    ResearchReport.id.in_(list(tips)),
                    ResearchReport.collection_id == queue.collection_id,
                )
            )
        ).all()
    )
    return [
        ScreeningConflictResponse(
            report_id=report_id,
            title_snapshot=titles.get(report_id, ""),
            resolution=_resolution_response(tips[report_id]),
            observations=[
                ScreeningObservationResponse.model_validate(observations[oid])
                for oid in map(UUID, map(str, tips[report_id].input_observation_ids))
                if oid in observations
            ],
        )
        for report_id in (UUID(value) for value in queue.report_ids)
        if report_id in tips
    ]


async def _resolution_by_id(db: AsyncSession, resolution_id: UUID) -> Any:
    row = await db.get(ScreeningResolution, resolution_id)
    assert isinstance(row, ScreeningResolution)
    return row


async def _adjudicator_target(
    db: AsyncSession, context: ProjectContext, queue_id: UUID
) -> tuple[Any, ResearchDecisionStream]:
    """ADJUDICATOR role, the queue (404), then its stream lock."""
    _require_role(context, ResearchProjectRole.ADJUDICATOR)
    queue = await _queue(db, context, queue_id)
    return queue, await _lock(db, context, queue_id)


async def _current_report(db: AsyncSession, queue: Any, report_id: UUID) -> None:
    """GOO-301's queue -> protocol staleness, then the report in this corpus."""
    stale = await _stale(db, queue)
    if stale is not None:
        raise _conflict(stale)
    if str(report_id) not in queue.report_ids:
        raise HTTPException(status_code=404, detail="Report not found")


async def adjudicate(
    db: AsyncSession,
    context: ProjectContext,
    queue_id: UUID,
    report_id: UUID,
    actor_user_id: UUID,
    data: ScreeningAdjudicateRequest,
) -> ScreeningResolutionResponse:
    """Resolve the exact conflict tip the adjudicator saw (ADJUDICATE)."""
    queue, stream = await _adjudicator_target(db, context, queue_id)
    fingerprint = _fingerprint(
        "adjudicate", queue_id, actor_user_id, data, report_id=str(report_id)
    )
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        return _resolution_response(
            await _resolution_by_id(
                db, UUID(cast(dict[str, Any], replayed.payload)["resolution_id"])
            )
        )
    await _current_report(db, queue, report_id)
    if data.criteria_hash != queue.criteria_hash:
        raise _conflict(CRITERIA_STALE)
    tip = await _tip(db, queue_id, report_id)
    inputs = sorted(str(i) for i in data.input_observation_ids)
    current = {
        str(row.id)
        for row in (
            await db.execute(
                _current_observations().where(
                    ScreeningObservation.queue_id == queue_id,
                    ScreeningObservation.id.in_(data.input_observation_ids),
                )
            )
        ).scalars()
    }
    if (
        tip is None
        or tip.id != data.resolution_id
        or inputs != sorted(str(i) for i in tip.input_observation_ids)
        or set(inputs) != current
    ):
        raise _conflict(INPUTS_STALE)
    if tip.basis != "conflict":
        raise _conflict(NOT_IN_CONFLICT)
    reviewed = (
        await db.execute(
            select(ScreeningObservation.id).where(
                ScreeningObservation.id.in_(data.input_observation_ids),
                ScreeningObservation.reviewer_id == actor_user_id,
            )
        )
    ).first()
    if reviewed is not None:
        raise HTTPException(status_code=403, detail=SELF_ADJUDICATION)
    reasons = screening_rules.exclusion_reasons(
        (await _version(db, queue.protocol_version_id)).snapshot
    )
    try:
        screening_rules.validate_observation(
            queue.stage, data.decision, data.exclusion_reason, reasons
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    resolution_id = uuid4()
    event = await _append(
        db,
        context,
        queue_id=queue_id,
        event_type="screening.adjudicated",
        actor_user_id=actor_user_id,
        actor_role="adjudicator",
        reason=data.rationale,
        payload={
            "report_id": str(report_id),
            "resolution_id": str(resolution_id),
            "conflict_resolution_id": str(tip.id),
            "input_observation_ids": inputs,
            "criteria_hash": queue.criteria_hash,
            "decision": data.decision,
            "exclusion_reason": data.exclusion_reason,
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _insert_resolution(
        db,
        ScreeningResolution(
            id=resolution_id,
            queue_id=queue_id,
            report_id=report_id,
            basis="adjudicated",
            outcome=data.decision,
            exclusion_reason=data.exclusion_reason,
            input_observation_ids=inputs,
            criteria_hash=queue.criteria_hash,
            supersedes_resolution_id=tip.id,
            event_id=event.id,
        ),
    )


async def _insert_resolution(
    db: AsyncSession, row: ScreeningResolution
) -> ScreeningResolutionResponse:
    db.add(row)
    await _flush_unique(db, RESOLUTION_STALE)
    await db.refresh(row)
    return _resolution_response(row)


async def reopen(
    db: AsyncSession,
    context: ProjectContext,
    queue_id: UUID,
    report_id: UUID,
    actor_user_id: UUID,
    data: ScreeningReopenRequest,
) -> ScreeningResolutionResponse:
    """Reopen a resolved report (ADJUDICATE); each reviewer must then submit a
    fresh observation before it can resolve again."""
    queue, stream = await _adjudicator_target(db, context, queue_id)
    fingerprint = _fingerprint(
        "reopen", queue_id, actor_user_id, data, report_id=str(report_id)
    )
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        return _resolution_response(
            await _resolution_by_id(
                db, UUID(cast(dict[str, Any], replayed.payload)["resolution_id"])
            )
        )
    await _current_report(db, queue, report_id)
    tip = await _tip(db, queue_id, report_id)
    if tip is None or tip.basis == "reopened":
        raise _conflict(NOT_RESOLVED)
    if tip.id != data.resolution_id:
        raise _conflict(RESOLUTION_STALE)
    resolution_id = uuid4()
    event = await _append(
        db,
        context,
        queue_id=queue_id,
        event_type="screening.reopened",
        actor_user_id=actor_user_id,
        actor_role="adjudicator",
        reason=data.rationale,
        payload={
            "report_id": str(report_id),
            "resolution_id": str(resolution_id),
            "reopened_resolution_id": str(tip.id),
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _insert_resolution(
        db,
        ScreeningResolution(
            id=resolution_id,
            queue_id=queue_id,
            report_id=report_id,
            basis="reopened",
            outcome=None,
            exclusion_reason=None,
            input_observation_ids=[],
            criteria_hash=queue.criteria_hash,
            supersedes_resolution_id=tip.id,
            event_id=event.id,
        ),
    )
