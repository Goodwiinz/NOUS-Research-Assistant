"""Project-scoped report/study identity: observe, merge, split, link, history.

GOO-299. Every function here runs inside the caller's transaction and never
commits. Mutations first take the per-project ``research_identity`` decision
stream lock (after ``resolve_project``'s Workspace SHARE -> Collection UPDATE),
so imports, merges, splits and study links on one Collection are serialized and
each mutation is recorded as exactly one ledger event.
"""

import json
import re
from typing import Any, Iterable, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocol
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_source import ResearchSource
from src.schemas.research_engine import (
    IdentityEventResponse,
    ReportCandidatesResponse,
    ReportMergeRequest,
    ReportObservationResponse,
    ReportResponse,
    ReportSplitRequest,
    ReportSuggestion,
    StudyLinkRequest,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
    replay_decisions,
)
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.report_identity import (
    IDENTITY_KINDS,
    assign_report,
    report_identifiers,
)

AGGREGATE_TYPE = "research_identity"
SUBJECT_TYPE = "research_report"
_REPORT_NOT_FOUND = "Report not found"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def _lock(db: AsyncSession, collection_id: UUID) -> ResearchDecisionStream:
    return await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )


async def _replayed_event(
    db: AsyncSession,
    stream: ResearchDecisionStream,
    idempotency_key: str,
    fingerprint: str,
) -> ResearchDecisionEvent | None:
    """Return the prior event for an identical retry; 409 for a reused key.

    Checked under the stream lock *before* any mutation, so a retry never
    re-applies (and never trips over state its first attempt already changed).
    """
    existing = cast(
        ResearchDecisionEvent | None,
        (
            await db.execute(
                select(ResearchDecisionEvent).where(
                    ResearchDecisionEvent.stream_id == stream.id,
                    ResearchDecisionEvent.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none(),
    )
    if existing is not None and existing.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail="Idempotency conflict")
    return existing


async def _protocol_version_id(db: AsyncSession, collection_id: UUID) -> str | None:
    version_id = (
        await db.execute(
            select(ResearchProtocol.current_approved_version_id)
            .where(
                ResearchProtocol.collection_id == collection_id,
                ResearchProtocol.is_deleted.is_(False),
                ResearchProtocol.current_approved_version_id.is_not(None),
            )
            .order_by(ResearchProtocol.updated_at.desc(), ResearchProtocol.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(version_id) if version_id is not None else None


def _require_role(context: ProjectContext, role: ResearchProjectRole) -> None:
    if role not in context.effective_roles:
        raise HTTPException(status_code=403, detail=f"{role.value} role required")


async def _append(
    db: AsyncSession,
    *,
    collection_id: UUID,
    event_type: str,
    actor_user_id: UUID,
    actor_role: str,
    subject_id: UUID,
    reason: str,
    payload: dict[str, Any],
    idempotency_key: str,
    fingerprint: str,
) -> None:
    try:
        await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type=event_type,
            event_schema_version=1,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=subject_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


async def _live_reports(
    db: AsyncSession,
    collection_id: UUID,
    report_ids: Iterable[UUID],
    *,
    merged_status: int = 409,
) -> dict[UUID, Any]:
    """Reports of this Collection by id; a foreign id looks exactly like a missing one.

    Values are ``ResearchReport`` rows, typed ``Any`` because the legacy
    ``Column`` attributes are not assignable under mypy without the plugin.
    """
    wanted = list(dict.fromkeys(report_ids))
    rows = (
        (
            await db.execute(
                select(ResearchReport)
                .where(
                    ResearchReport.id.in_(wanted),
                    ResearchReport.collection_id == collection_id,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    found = {cast(UUID, row.id): row for row in rows}
    if set(found) != set(wanted):
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    if any(row.merged_into_report_id is not None for row in rows):
        raise HTTPException(
            status_code=merged_status,
            detail=(
                "Report already merged" if merged_status == 409 else _REPORT_NOT_FOUND
            ),
        )
    return found


async def _responses(
    db: AsyncSession, collection_id: UUID, report_ids: list[UUID] | None = None
) -> list[ReportResponse]:
    query = select(ResearchReport).where(ResearchReport.collection_id == collection_id)
    if report_ids is not None:
        query = query.where(ResearchReport.id.in_(report_ids))
    reports = (
        (await db.execute(query.order_by(ResearchReport.created_at, ResearchReport.id)))
        .scalars()
        .all()
    )
    ids = [cast(UUID, report.id) for report in reports]
    identifiers: dict[UUID, dict[str, list[str]]] = {i: {} for i in ids}
    for report_id, kind, value in (
        await db.execute(
            select(
                ResearchReportIdentifier.report_id,
                ResearchReportIdentifier.kind,
                ResearchReportIdentifier.value,
            )
            .where(ResearchReportIdentifier.report_id.in_(ids))
            .order_by(ResearchReportIdentifier.kind, ResearchReportIdentifier.value)
        )
    ).all():
        identifiers[report_id].setdefault(kind, []).append(value)
    observations: dict[UUID, list[ReportObservationResponse]] = {i: [] for i in ids}
    for observation, run_id in (
        await db.execute(
            select(ResearchReportObservation, ResearchSource.run_id)
            .join(
                ResearchSource, ResearchSource.id == ResearchReportObservation.source_id
            )
            .where(ResearchReportObservation.report_id.in_(ids))
            .order_by(
                ResearchReportObservation.created_at, ResearchReportObservation.id
            )
        )
    ).all():
        observations[cast(UUID, observation.report_id)].append(
            ReportObservationResponse(
                source_id=observation.source_id,
                run_id=run_id,
                match_method=observation.match_method,
                evidence=observation.evidence,
            )
        )
    return [
        ReportResponse.model_validate(
            {
                "id": report.id,
                "title_snapshot": report.title_snapshot,
                "identifiers": identifiers[cast(UUID, report.id)],
                "study_id": report.study_id,
                "study_link_status": report.study_link_status,
                "study_link_rationale": report.study_link_rationale,
                "merged_into_report_id": report.merged_into_report_id,
                "observations": observations[cast(UUID, report.id)],
            }
        )
        for report in reports
    ]


async def _response(
    db: AsyncSession, collection_id: UUID, report_id: UUID
) -> ReportResponse:
    return (await _responses(db, collection_id, [report_id]))[0]


async def observe_sources(
    db: AsyncSession, *, collection_id: UUID, sources: Sequence[ResearchSource]
) -> list[ResearchReportObservation]:
    """Attach each provider snapshot to a report by identifiers only.

    ``rag_store`` sources are skipped (org-scoped documents keep their own
    dedup). A source that already has an observation is skipped under the lock,
    so a repeated import creates nothing; ``UNIQUE(source_id)`` stays the
    database guard underneath. Original ``ResearchSource`` rows are never touched.
    """
    await _lock(db, collection_id)
    candidates = [s for s in sources if s.connector_type != "rag_store"]
    observed = set(
        (
            await db.execute(
                select(ResearchReportObservation.source_id).where(
                    ResearchReportObservation.source_id.in_([s.id for s in candidates])
                )
            )
        )
        .scalars()
        .all()
    )
    index: dict[tuple[str, str], UUID] = {
        (kind, value): report_id
        for kind, value, report_id in (
            await db.execute(
                select(
                    ResearchReportIdentifier.kind,
                    ResearchReportIdentifier.value,
                    ResearchReportIdentifier.report_id,
                )
                .join(
                    ResearchReport,
                    ResearchReport.id == ResearchReportIdentifier.report_id,
                )
                .where(
                    ResearchReportIdentifier.collection_id == collection_id,
                    ResearchReport.merged_into_report_id.is_(None),
                )
            )
        ).all()
    }
    created: list[ResearchReportObservation] = []
    for source in candidates:
        if source.id in observed:
            continue
        observed.add(source.id)
        raw = cast(dict[str, Any], source.metadata_ or {}).get("identifiers") or {}
        hit = assign_report(index, raw)
        report_id = cast(UUID | None, hit.report_key)
        if report_id is None:
            report_id = uuid4()
            db.add(
                ResearchReport(
                    id=report_id,
                    collection_id=collection_id,
                    title_snapshot=(source.title or "")[:500],
                )
            )
        # The identifier set only grows; values claimed by another report are
        # conflicts, kept in evidence and never inserted.
        for kind, value in report_identifiers(raw).items():
            if kind in IDENTITY_KINDS and (kind, value) not in index:
                index[(kind, value)] = report_id
                db.add(
                    ResearchReportIdentifier(
                        collection_id=collection_id,
                        report_id=report_id,
                        kind=kind,
                        value=value[:512],
                    )
                )
        await db.flush()
        observation_id = (
            await db.execute(
                pg_insert(ResearchReportObservation)
                .values(
                    id=uuid4(),
                    collection_id=collection_id,
                    report_id=report_id,
                    source_id=source.id,
                    match_method=hit.match_method,
                    evidence=_json_safe(hit.evidence),
                )
                .on_conflict_do_nothing(index_elements=["source_id"])
                .returning(ResearchReportObservation.id)
            )
        ).scalar_one_or_none()
        if observation_id is not None:
            created.append(
                cast(
                    ResearchReportObservation,
                    await db.get(ResearchReportObservation, observation_id),
                )
            )
    return created


async def list_reports(
    db: AsyncSession, *, collection_id: UUID
) -> list[ReportResponse]:
    return await _responses(db, collection_id)


def _normalized_title(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.casefold()).strip()


def _year(metadata: dict[str, Any] | None) -> str | None:
    metadata = metadata or {}
    date = metadata.get("publication_date")
    if isinstance(date, str) and re.match(r"\d{4}", date):
        return date[:4]
    published = metadata.get("published")  # Crossref date-parts [[Y, M, D]]
    if isinstance(published, list) and published and isinstance(published[0], list):
        return str(published[0][0]) if published[0] else None
    return None


async def candidates(
    db: AsyncSession, *, collection_id: UUID, report_id: UUID
) -> ReportCandidatesResponse:
    """Read-only: suggest same-title/same-year reports and surface id conflicts."""
    rows = (
        await db.execute(
            select(
                ResearchReport.id,
                ResearchReport.title_snapshot,
                ResearchReport.merged_into_report_id,
                ResearchReportObservation.evidence,
                ResearchSource.metadata_,
            )
            .outerjoin(
                ResearchReportObservation,
                ResearchReportObservation.report_id == ResearchReport.id,
            )
            .outerjoin(
                ResearchSource, ResearchSource.id == ResearchReportObservation.source_id
            )
            .where(ResearchReport.collection_id == collection_id)
        )
    ).all()
    titles: dict[UUID, str] = {}
    years: dict[UUID, set[str]] = {}
    conflicts: list[dict[str, Any]] = []
    for rid, title, merged_into, evidence, metadata in rows:
        if merged_into is not None:
            continue
        titles[rid] = _normalized_title(title)
        year = _year(metadata)
        years.setdefault(rid, set()).update([year] if year else [])
        if rid == report_id and evidence:
            conflicts.extend(evidence.get("conflicts") or [])
    if report_id not in titles:
        raise HTTPException(status_code=404, detail=_REPORT_NOT_FOUND)
    target_years = years[report_id]
    suggested = [
        ReportSuggestion(report_id=rid, reason="title_year")
        for rid, title in titles.items()
        if rid != report_id and title and title == titles[report_id]
        # Unknown year on either side does not veto; a known mismatch does.
        and (not target_years or not years[rid] or bool(target_years & years[rid]))
    ]
    return ReportCandidatesResponse(
        report_id=report_id, suggested=suggested, conflicts=conflicts
    )


async def link_study(
    db: AsyncSession,
    context: ProjectContext,
    report_id: UUID,
    actor_user_id: UUID,
    data: StudyLinkRequest,
) -> ReportResponse:
    """Reviewers propose; adjudicators confirm or dispute. One event per change."""
    proposing = data.status == "proposed"
    _require_role(
        context,
        ResearchProjectRole.REVIEWER if proposing else ResearchProjectRole.ADJUDICATOR,
    )
    collection_id = cast(UUID, context.collection.id)
    stream = await _lock(db, collection_id)
    fingerprint = decision_request_fingerprint(
        {
            "operation": "study_link",
            "report_id": str(report_id),
            "actor_user_id": str(actor_user_id),
            **data.model_dump(mode="json"),
        }
    )
    if await _replayed_event(db, stream, data.idempotency_key, fingerprint):
        return await _response(db, collection_id, report_id)
    report = (await _live_reports(db, collection_id, [report_id], merged_status=404))[
        report_id
    ]
    if proposing and report.study_link_status in {"confirmed", "disputed"}:
        # A reviewer proposal must not downgrade or re-point an adjudicated link.
        raise HTTPException(status_code=409, detail="Study link is already adjudicated")
    study_id = data.study_id or cast(UUID | None, report.study_id)
    if study_id is None:
        study_id = uuid4()
        db.add(
            ResearchStudy(
                id=study_id,
                collection_id=collection_id,
                label=report.title_snapshot,
            )
        )
        await db.flush()  # no ORM relationship orders the study before the report
    elif (
        await db.execute(
            select(ResearchStudy.id).where(
                ResearchStudy.id == study_id,
                ResearchStudy.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Study not found")
    prior_study_id, prior_status = report.study_id, report.study_link_status
    report.study_id = study_id
    report.study_link_status = data.status
    report.study_link_actor_id = actor_user_id
    report.study_link_rationale = data.rationale
    await db.flush()
    evidence = await candidates(db, collection_id=collection_id, report_id=report_id)
    await _append(
        db,
        collection_id=collection_id,
        event_type="identity.study_linked",
        actor_user_id=actor_user_id,
        actor_role="reviewer" if proposing else "adjudicator",
        subject_id=report_id,
        reason=data.rationale,
        payload={
            "collection_id": str(collection_id),
            "report_id": str(report_id),
            "study_id": str(study_id),
            "status": data.status,
            "prior_study_id": str(prior_study_id) if prior_study_id else None,
            "prior_status": prior_status,
            "match_evidence": evidence.model_dump(mode="json"),
            "protocol_version_id": await _protocol_version_id(db, collection_id),
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _response(db, collection_id, report_id)


async def _identifier_pairs(
    db: AsyncSession, report_ids: list[UUID]
) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(ResearchReportIdentifier.kind, ResearchReportIdentifier.value)
            .where(ResearchReportIdentifier.report_id.in_(report_ids))
            .order_by(ResearchReportIdentifier.kind, ResearchReportIdentifier.value)
        )
    ).all()
    return [{"kind": kind, "value": value} for kind, value in rows]


async def merge_reports(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    data: ReportMergeRequest,
) -> ReportResponse:
    """Re-point losers' identifiers and observations at the survivor.

    Losers are never deleted; they keep ``merged_into_report_id``. A survivor
    without a study link inherits the first linked loser's link (the ledger
    replay applies the same rule).
    """
    _require_role(context, ResearchProjectRole.ADJUDICATOR)
    collection_id = cast(UUID, context.collection.id)
    survivor_id = data.surviving_report_id
    loser_ids = list(dict.fromkeys(data.merged_report_ids))
    if survivor_id in loser_ids:
        raise HTTPException(status_code=422, detail="Report cannot merge into itself")
    stream = await _lock(db, collection_id)
    fingerprint = decision_request_fingerprint(
        {
            "operation": "merge",
            "actor_user_id": str(actor_user_id),
            **data.model_dump(mode="json"),
        }
    )
    if await _replayed_event(db, stream, data.idempotency_key, fingerprint):
        return await _response(db, collection_id, survivor_id)
    reports = await _live_reports(db, collection_id, [survivor_id, *loser_ids])
    moved_source_ids = [
        str(source_id)
        for source_id in (
            await db.execute(
                select(ResearchReportObservation.source_id)
                .where(ResearchReportObservation.report_id.in_(loser_ids))
                .order_by(ResearchReportObservation.source_id)
            )
        )
        .scalars()
        .all()
    ]
    moved_identifiers = await _identifier_pairs(db, loser_ids)
    models: tuple[Any, ...] = (ResearchReportIdentifier, ResearchReportObservation)
    for model in models:
        await db.execute(
            update(model)
            .where(model.report_id.in_(loser_ids))
            .values(report_id=survivor_id)
        )
    survivor = reports[survivor_id]
    for loser_id in loser_ids:
        loser = reports[loser_id]
        loser.merged_into_report_id = survivor_id
        if survivor.study_id is None and loser.study_id is not None:
            survivor.study_id = loser.study_id
            survivor.study_link_status = loser.study_link_status
            survivor.study_link_actor_id = loser.study_link_actor_id
            survivor.study_link_rationale = loser.study_link_rationale
    await db.flush()
    await _append(
        db,
        collection_id=collection_id,
        event_type="identity.report_merged",
        actor_user_id=actor_user_id,
        actor_role="adjudicator",
        subject_id=survivor_id,
        reason=data.rationale,
        payload={
            "collection_id": str(collection_id),
            "surviving_report_id": str(survivor_id),
            "merged_report_ids": [str(loser_id) for loser_id in loser_ids],
            "moved_source_ids": moved_source_ids,
            "moved_identifiers": moved_identifiers,
            "protocol_version_id": await _protocol_version_id(db, collection_id),
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _response(db, collection_id, survivor_id)


async def split_report(
    db: AsyncSession,
    context: ProjectContext,
    report_id: UUID,
    actor_user_id: UUID,
    data: ReportSplitRequest,
) -> ReportResponse:
    """Move named observations (and identifiers only they carry) to a new report."""
    _require_role(context, ResearchProjectRole.ADJUDICATOR)
    collection_id = cast(UUID, context.collection.id)
    stream = await _lock(db, collection_id)
    fingerprint = decision_request_fingerprint(
        {
            "operation": "split",
            "report_id": str(report_id),
            "actor_user_id": str(actor_user_id),
            **data.model_dump(mode="json"),
        }
    )
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        new_id = UUID(cast(dict[str, Any], replayed.payload)["new_report_id"])
        return await _response(db, collection_id, new_id)
    await _live_reports(db, collection_id, [report_id])
    rows = (
        await db.execute(
            select(ResearchReportObservation, ResearchSource.title)
            .join(
                ResearchSource, ResearchSource.id == ResearchReportObservation.source_id
            )
            .where(ResearchReportObservation.report_id == report_id)
            .order_by(
                ResearchReportObservation.created_at, ResearchReportObservation.id
            )
        )
    ).all()
    wanted = set(data.source_ids)
    moving = [(obs, title) for obs, title in rows if obs.source_id in wanted]
    staying = [obs for obs, _title in rows if obs.source_id not in wanted]
    if len(moving) != len(wanted):
        raise HTTPException(status_code=404, detail="Source not found in report")
    if not staying:
        raise HTTPException(
            status_code=409, detail="Split must leave at least one source"
        )

    def observed(observations: Iterable[ResearchReportObservation]) -> set[tuple]:
        return {
            (kind, value)
            for obs in observations
            for kind, value in (
                cast(dict[str, Any], obs.evidence or {}).get("observed") or {}
            ).items()
        }

    only_moving = observed(obs for obs, _title in moving) - observed(staying)
    moved_identifiers = [
        pair
        for pair in await _identifier_pairs(db, [report_id])
        if (pair["kind"], pair["value"]) in only_moving
    ]
    new_id = uuid4()
    db.add(
        ResearchReport(
            id=new_id,
            collection_id=collection_id,
            title_snapshot=(moving[0][1] or "")[:500],
        )
    )
    await db.flush()
    for obs, _title in moving:
        obs.report_id = new_id
    for pair in moved_identifiers:
        await db.execute(
            update(ResearchReportIdentifier)
            .where(
                ResearchReportIdentifier.report_id == report_id,
                ResearchReportIdentifier.kind == pair["kind"],
                ResearchReportIdentifier.value == pair["value"],
            )
            .values(report_id=new_id)
        )
    await db.flush()
    await _append(
        db,
        collection_id=collection_id,
        event_type="identity.report_split",
        actor_user_id=actor_user_id,
        actor_role="adjudicator",
        subject_id=report_id,
        reason=data.rationale,
        payload={
            "collection_id": str(collection_id),
            "source_report_id": str(report_id),
            "new_report_id": str(new_id),
            "moved_source_ids": sorted(str(obs.source_id) for obs, _t in moving),
            "moved_identifiers": moved_identifiers,
            "protocol_version_id": await _protocol_version_id(db, collection_id),
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _response(db, collection_id, new_id)


async def history(
    db: AsyncSession, *, collection_id: UUID
) -> list[IdentityEventResponse]:
    """Replay-validated identity decisions for one Collection, in order."""
    events = await replay_decisions(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    return [
        IdentityEventResponse.model_validate(event, from_attributes=True)
        for event in events
    ]
