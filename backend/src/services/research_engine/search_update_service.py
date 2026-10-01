"""Scheduled search updates and classified corpus deltas (GOO-319).

API writers (``create_schedule``, ``version_schedule``) follow the route's
``resolve_project`` (SUPERVISE), then this Collection's
``research_search_update`` stream; one commit each. A schedule pins one
exact GOO-298 strategy from a completed run's search journal (re-hashed on
save) and the approved protocol version it was built under; only a journal
entry can be pinned, so local saved searches are never backfilled as
strategies. A changed strategy is a new version and passes the protocol
check again.

The worker:

1. ``claim_due`` locks each enabled tip version ``FOR UPDATE SKIP LOCKED``,
   derives the latest due fire (missed fires coalesced) and inserts the
   execution ``ON CONFLICT (schedule_id, scheduled_local) DO NOTHING``;
   commits. The committed row is the claim, so it survives a restart.
2. ``run_execution`` locks the execution row, inserts a ``started`` attempt
   (or ``skipped``: overlap or disabled) and commits; a ``started`` attempt
   older than ``STALE_EXECUTION`` without a terminal attempt is dead and a
   new one is inserted.
3. It rechecks, as the schedule owner, EDIT access, the SUPERVISE role and
   the protocol version; any failure is a ``failed`` attempt and nothing
   runs. Locks are released before provider I/O.
4. Search (``discovery.search_sources``), then the import as one GOO-300
   receipt (``scheduled:{execution_id}``; a retry reuses it and never
   searches again), committed.
5. Crossref ``update-to`` notices and, only when the protocol requires it,
   citation chasing; then the corpus snapshot (GOO-300 export), the
   classified delta, the results row, the ``succeeded`` attempt and the
   ``search_update.executed`` event in one commit.

No staleness is written here: GOO-320 accepts a delta by ``(execution_id,
delta_hash)`` and decides what it invalidates.

# ponytail: out of scope, each added at its seam: sub-hourly schedules,
# backfilling missed fires, retraction feeds beyond Crossref ``update-to``
# (Retraction Watch is read only where Crossref carries it), notifications
# on new deltas, per-provider schedules, converting local saved searches
# into strategies.
"""

import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.core.config import settings
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_project_role import ResearchProjectRole
from src.models.research_report import ResearchReport
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_search_update import (
    ResearchSearchExecution,
    ResearchSearchExecutionAttempt,
    ResearchSearchExecutionResult,
    ResearchSearchSchedule,
)
from src.models.research_step import ResearchStep
from src.models.workspace import WorkspaceRole
from src.schemas.research_engine import (
    CitationChaseRequest,
    SearchAttemptResponse,
    SearchDeltaResponse,
    SearchExecutionListResponse,
    SearchExecutionResponse,
    SearchScheduleCreate,
    SearchScheduleListResponse,
    SearchScheduleResponse,
    SearchScheduleVersionCreate,
    SearchScheduleVersionResponse,
    SearchStrategyOption,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import corpus_export, corpus_service
from src.services.research_engine import search_update_rules as rules
from src.services.research_engine.connectors.crossref_connector import CrossrefConnector
from src.services.research_engine.discovery import search_sources
from src.services.research_engine.identity_service import (
    _replayed_event,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)

logger = logging.getLogger(__name__)

AGGREGATE_TYPE = "research_search_update"
SUBJECT_TYPE = "search_schedule"
RECEIPT_KIND = "scheduled_search"
DELTA_SCHEMA = "nous.academic.search-delta.v1"
EXPORT_SCHEMA = "nous.academic.search-updates.v1"
STALE_EXECUTION = timedelta(minutes=30)
MAX_NOTICE_DOIS = 400  # 20 Crossref requests; beyond -> check not performed
MAX_CHASE_SEEDS = 3  # new reports chased per execution when required
TERMINAL = ("succeeded", "failed", "skipped")

SCHEDULE_NOT_FOUND = "Schedule not found"
EXECUTION_NOT_FOUND = "Execution not found"
STRATEGY_NOT_FOUND = "Strategy not found in a completed run of this project"
STRATEGY_MISMATCH = "Strategy hash mismatch"
SUPERSEDED = (
    "Strategy was built under a superseded protocol; approve an amendment "
    "and bind a new strategy"
)
STALE_TIP = "Schedule changed; reload"
EDITOR_REQUIRED = "Schedule owners need edit access to the project"
CORPUS_TOO_LARGE = "The corpus is too large for a baseline snapshot"

OWNER_ACCESS_REVOKED = "owner_access_revoked"
OWNER_ROLE_REVOKED = "owner_role_revoked"
PROJECT_ARCHIVED = "project_archived"
PROTOCOL_CHANGED = "protocol_changed"

S = ResearchSearchSchedule
E = ResearchSearchExecution
A = ResearchSearchExecutionAttempt
R = ResearchSearchExecutionResult
ConnectorsFactory = Callable[[UUID | None], Mapping[str, Any]]


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _worker() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:100]


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _tips() -> Any:
    successor = aliased(ResearchSearchSchedule)
    return select(S).where(
        ~exists().where(successor.supersedes_schedule_version_id == S.id)
    )


# --- Strategy pinning --------------------------------------------------------


async def _completed_runs(db: AsyncSession, context: ProjectContext) -> list[Any]:
    runs = await corpus_export._runs(db, context)
    return [r for r in runs if r.status == RunStatus.COMPLETED.value]


async def _queries(
    db: AsyncSession, run_ids: Sequence[Any]
) -> dict[tuple[Any, str], str]:
    """``(run_id, strategy_version) -> rendered query`` from search steps (the
    strategy itself redacts parameters, so the query lives in the output)."""
    steps = await _all(
        db,
        select(ResearchStep).where(
            ResearchStep.run_id.in_(run_ids), ResearchStep.step_type == "search"
        ),
    )
    out: dict[tuple[Any, str], str] = {}
    for step in steps:
        output = cast(dict[str, Any], step.output or {})
        version = (output.get("coverage") or {}).get("strategy_version")
        if version and isinstance(output.get("query"), str):
            out[(step.run_id, version)] = output["query"]
    return out


async def strategy_options(
    db: AsyncSession, context: ProjectContext
) -> list[SearchStrategyOption]:
    runs = await _completed_runs(db, context)
    queries = await _queries(db, [r.id for r in runs])
    current = await current_protocol_version_id(db, _cid(context))
    options = []
    for run in runs:
        manifest = cast(dict[str, Any], run.reproducibility_manifest or {})
        journal = manifest.get("_search_receipts_v1") or {}
        for version, strategy in sorted((journal.get("strategies") or {}).items()):
            query = queries.get((run.id, version))
            if query is None:
                continue
            options.append(
                SearchStrategyOption(
                    source_run_id=run.id,
                    step_id=str(strategy.get("step_id") or ""),
                    strategy_version=version,
                    query=query,
                    providers=list(
                        (strategy.get("intended") or {}).get("selected_providers") or []
                    ),
                    protocol_version_id=strategy.get("protocol_version_id"),
                    current_protocol=current is not None
                    and strategy.get("protocol_version_id") == current,
                )
            )
    return options


async def _pinned(
    db: AsyncSession,
    context: ProjectContext,
    run_id: UUID,
    step_id: str,
    strategy_version: str,
) -> tuple[dict[str, Any], str, UUID]:
    """(strategy snapshot, query, protocol version id) for one journal entry;
    422 for a missing entry or a hash mismatch, 409 for a superseded
    protocol."""
    run = next((r for r in await _completed_runs(db, context) if r.id == run_id), None)
    if run is None:
        raise HTTPException(status_code=422, detail=STRATEGY_NOT_FOUND)
    manifest = cast(dict[str, Any], run.reproducibility_manifest or {})
    journal = manifest.get("_search_receipts_v1") or {}
    strategy = (journal.get("strategies") or {}).get(strategy_version)
    query = (await _queries(db, [run.id])).get((run.id, strategy_version))
    if not isinstance(strategy, dict) or query is None:
        raise HTTPException(status_code=422, detail=STRATEGY_NOT_FOUND)
    if (
        strategy.get("strategy_version") != strategy_version
        or rules.strategy_hash(strategy) != strategy_version
        or str(strategy.get("step_id")) != step_id
    ):
        raise HTTPException(status_code=422, detail=STRATEGY_MISMATCH)
    current = await current_protocol_version_id(db, _cid(context))
    if current is None or strategy.get("protocol_version_id") != current:
        raise HTTPException(status_code=409, detail=SUPERSEDED)
    return dict(strategy), query, UUID(current)


# --- Reads -------------------------------------------------------------------


def _version_response(row: Any) -> SearchScheduleVersionResponse:
    return cast(
        SearchScheduleVersionResponse,
        SearchScheduleVersionResponse.model_validate(row, from_attributes=True),
    )


def _execution_status(attempts: Sequence[Any]) -> str:
    terminal = [a for a in attempts if a.outcome in TERMINAL]
    if terminal:
        return cast(str, terminal[-1].outcome)
    return "started" if attempts else "pending"


def _execution_response(
    row: Any, attempts: Sequence[Any], result: Any | None
) -> SearchExecutionResponse:
    counts = None if result is None else (result.delta or {}).get("counts")
    return SearchExecutionResponse(
        id=row.id,
        schedule_id=row.schedule_id,
        schedule_version_id=row.schedule_version_id,
        scheduled_local=row.scheduled_local,
        scheduled_for=row.scheduled_for,
        missed_fires=row.missed_fires,
        created_at=row.created_at,
        status=cast(Any, _execution_status(attempts)),
        attempts=[
            SearchAttemptResponse.model_validate(a, from_attributes=True)
            for a in attempts
        ],
        import_receipt_id=None if result is None else result.import_receipt_id,
        baseline_execution_id=(
            None if result is None else result.baseline_execution_id
        ),
        delta_hash=None if result is None else result.delta_hash,
        counts=counts,
    )


async def _executions(
    db: AsyncSession, collection_id: UUID, schedule_id: UUID | None = None
) -> list[SearchExecutionResponse]:
    query = select(E).where(E.collection_id == collection_id)
    if schedule_id is not None:
        query = query.where(E.schedule_id == schedule_id)
    rows = await _all(db, query.order_by(E.scheduled_for, E.created_at, E.id))
    ids = [r.id for r in rows]
    attempts: dict[Any, list[Any]] = {i: [] for i in ids}
    results: dict[Any, Any] = {}
    if ids:
        for attempt in await _all(
            db,
            select(A).where(A.execution_id.in_(ids)).order_by(A.created_at, A.id),
        ):
            attempts[attempt.execution_id].append(attempt)
        results = {
            r.execution_id: r
            for r in await _all(db, select(R).where(R.execution_id.in_(ids)))
        }
    return [_execution_response(r, attempts[r.id], results.get(r.id)) for r in rows]


def _chain(versions: Sequence[Any]) -> list[Any]:
    by_previous = {v.supersedes_schedule_version_id: v for v in versions}
    chain = [next(v for v in versions if v.supersedes_schedule_version_id is None)]
    while chain[-1].id in by_previous:
        chain.append(by_previous[chain[-1].id])
    return chain


def _schedule_response(
    chain: Sequence[Any],
    executions: Sequence[SearchExecutionResponse],
    now: datetime,
) -> SearchScheduleResponse:
    tip = chain[-1]
    last = executions[-1] if executions else None
    last_attempt = None
    if last is not None and last.attempts:
        last_attempt = last.attempts[-1].model_dump()
    next_local = next_utc = None
    if tip.enabled:
        cron, tz = rules.parse_schedule(tip.cron, tip.timezone)
        after = rules.local_now(tip.created_at, tz)
        if last is not None:
            after = max(
                after, datetime.strptime(last.scheduled_local, rules.LOCAL_FORMAT)
            )
        next_local = rules.next_fire(cron, tz, after, now)
        next_utc = None if next_local is None else rules.to_utc(next_local, tz)
    return SearchScheduleResponse(
        schedule_id=tip.schedule_id,
        tip=_version_response(tip),
        versions=[_version_response(v) for v in chain],
        status=cast(Any, rules.schedule_status(bool(tip.enabled), last_attempt)),
        next_fire_local=(
            None if next_local is None else next_local.strftime(rules.LOCAL_FORMAT)
        ),
        next_fire_utc=next_utc,
        last_execution=last,
    )


async def _schedules(
    db: AsyncSession, collection_id: UUID
) -> list[SearchScheduleResponse]:
    versions = await _all(
        db,
        select(S).where(S.collection_id == collection_id).order_by(S.created_at, S.id),
    )
    executions = await _executions(db, collection_id)
    now = _now()
    by_schedule: dict[Any, list[Any]] = {}
    for version in versions:
        by_schedule.setdefault(version.schedule_id, []).append(version)
    return [
        _schedule_response(
            _chain(chain), [e for e in executions if e.schedule_id == sid], now
        )
        for sid, chain in by_schedule.items()
    ]


async def list_schedules(
    db: AsyncSession, context: ProjectContext
) -> SearchScheduleListResponse:
    """VIEW: every schedule (tip, versions, derived status, next fire, last
    execution) and the pinnable strategies of completed runs."""
    return SearchScheduleListResponse(
        schedules=await _schedules(db, _cid(context)),
        strategies=await strategy_options(db, context),
    )


async def list_executions(
    db: AsyncSession, context: ProjectContext, schedule_id: UUID
) -> SearchExecutionListResponse:
    """VIEW: one schedule's executions with every attempt (failures kept)."""
    exists_row = await db.scalar(
        select(S.id).where(S.collection_id == _cid(context), S.id == schedule_id)
    )
    if exists_row is None:
        raise HTTPException(status_code=404, detail=SCHEDULE_NOT_FOUND)
    return SearchExecutionListResponse(
        executions=await _executions(db, _cid(context), schedule_id)
    )


async def accepted_delta(
    db: AsyncSession, collection_id: UUID, execution_id: UUID
) -> SearchDeltaResponse:
    """The GOO-320 seam: one succeeded execution's classified delta. A
    foreign, unknown or unfinished execution looks the same: 404."""
    row = (
        await db.execute(
            select(R, E)
            .join(E, E.id == R.execution_id)
            .where(R.execution_id == execution_id, E.collection_id == collection_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=EXECUTION_NOT_FOUND)
    result, execution = row
    baseline_digest = await _baseline_digest(db, result, execution)
    delta = cast(dict[str, Any], result.delta)
    return cast(
        SearchDeltaResponse,
        SearchDeltaResponse.model_validate(
            {
                "execution_id": execution.id,
                "collection_id": execution.collection_id,
                "schedule_id": execution.schedule_id,
                "schedule_version_id": execution.schedule_version_id,
                "scheduled_local": execution.scheduled_local,
                "baseline_execution_id": result.baseline_execution_id,
                "baseline_digest": baseline_digest,
                "corpus_snapshot_digest": result.corpus_snapshot["digest"],
                "import_receipt_id": result.import_receipt_id,
                "delta_hash": result.delta_hash,
                "counts": delta["counts"],
                "items": delta["items"],
                "coverage": result.coverage,
                "citation_chasing": result.citation_chasing,
            }
        ),
    )


async def _baseline_digest(db: AsyncSession, result: Any, execution: Any) -> str:
    if result.baseline_execution_id is not None:
        previous = await db.get(R, result.baseline_execution_id)
        return cast(str, previous.corpus_snapshot["digest"])
    root = await db.get(S, execution.schedule_id)
    return cast(str, root.baseline_digest)


async def export_delta(
    db: AsyncSession,
    context: ProjectContext,
    execution_id: UUID,
    delta_class: str | None = None,
) -> dict[str, Any]:
    """VIEW: the sealed ``nous.academic.search-delta.v1`` export (read-only).
    ``delta_class`` filters the items; counts and ``delta_hash`` stay whole."""
    delta = await accepted_delta(db, _cid(context), execution_id)
    body = delta.model_dump(mode="json", by_alias=True)
    if delta_class is not None:
        body["items"] = [i for i in body["items"] if i["class"] == delta_class]
        body["filter"] = {"class": delta_class}
    version = await db.get(S, delta.schedule_version_id)
    body["schedule_version"] = _version_response(version).model_dump(mode="json")
    body["statement"] = (
        "Missing works are unknown, never deleted or retracted; only a Crossref "
        "update-to notice classifies a correction or retraction."
    )
    return {
        **corpus_export.seal(body, _now().isoformat()),
        "schema": DELTA_SCHEMA,
    }


async def export_part(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``search-updates.json`` body."""
    collection_id = _cid(context)
    schedules = await _schedules(db, collection_id)
    return {
        "project_id": str(collection_id),
        "schedules": [s.model_dump(mode="json") for s in schedules],
        "executions": [
            e.model_dump(mode="json") for e in await _executions(db, collection_id)
        ],
    }


# --- API writers -------------------------------------------------------------


async def _begin(
    db: AsyncSession, context: ProjectContext, operation: str, data: Any, actor: UUID
) -> tuple[str, str, dict[str, Any] | None]:
    stream = await lock_aggregate_stream(
        db,
        collection_id=_cid(context),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=_cid(context),
    )
    key = f"{operation}:{data.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor),
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    return key, fingerprint, None if replay is None else dict(replay.payload)


async def _append(
    db: AsyncSession,
    collection_id: UUID,
    *,
    event_type: str,
    subject_id: UUID,
    actor_id: UUID,
    actor_role: str,
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
) -> None:
    payload = {"collection_id": str(collection_id), **payload}
    try:
        await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type=event_type,
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=subject_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=None,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


def _parse(cron: str, timezone_name: str) -> None:
    try:
        rules.parse_schedule(cron, timezone_name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def _require_owner_access(context: ProjectContext) -> None:
    """The schedule runs as its saver: they must be able to EDIT too."""
    if context.workspace_role not in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
        WorkspaceRole.EDITOR,
    }:
        raise HTTPException(status_code=403, detail=EDITOR_REQUIRED)


async def _insert_version(
    db: AsyncSession, row: ResearchSearchSchedule, key: str, fingerprint: str
) -> None:
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        # UNIQUE(supersedes): another version won the tip.
        await db.rollback()
        raise HTTPException(status_code=409, detail=STALE_TIP) from error
    await _append(
        db,
        cast(UUID, row.collection_id),
        event_type="search_update.schedule_versioned",
        subject_id=cast(UUID, row.id),
        actor_id=cast(UUID, row.owner_id),
        actor_role="supervisor",
        payload={
            "schedule_id": str(row.schedule_id),
            "schedule_version_id": str(row.id),
            "supersedes_schedule_version_id": (
                None
                if row.supersedes_schedule_version_id is None
                else str(row.supersedes_schedule_version_id)
            ),
            "protocol_version_id": str(row.protocol_version_id),
            "strategy_version": row.strategy_version,
            "cron": row.cron,
            "timezone": row.timezone,
            "enabled": row.enabled,
        },
        key=key,
        fingerprint=fingerprint,
    )


async def _schedule(
    db: AsyncSession, collection_id: UUID, schedule_id: UUID
) -> SearchScheduleResponse:
    for schedule in await _schedules(db, collection_id):
        if schedule.schedule_id == schedule_id:
            return schedule
    raise HTTPException(status_code=404, detail=SCHEDULE_NOT_FOUND)


async def create_schedule(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: SearchScheduleCreate,
) -> tuple[SearchScheduleResponse, bool]:
    """SUPERVISE: save a schedule pinned to one strategy; the first version
    stores the baseline corpus snapshot (GOO-300 export) and its digest.
    Commits once; ``(schedule, replayed)``."""
    _require_owner_access(context)
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, context, "create", data, actor_id)
    if replay is not None:
        return await _schedule(db, collection_id, UUID(replay["schedule_id"])), True
    _parse(data.cron, data.timezone)
    strategy, query, protocol_id = await _pinned(
        db, context, data.source_run_id, data.step_id, data.strategy_version
    )
    try:
        package = await corpus_export.build_package(db, context)
    except HTTPException as error:
        if error.status_code == 413:
            raise HTTPException(status_code=422, detail=CORPUS_TOO_LARGE) from None
        raise
    schedule_id = uuid4()
    row = ResearchSearchSchedule(
        id=schedule_id,
        schedule_id=schedule_id,
        collection_id=collection_id,
        owner_id=actor_id,
        protocol_version_id=protocol_id,
        source_run_id=data.source_run_id,
        step_id=data.step_id,
        strategy_version=data.strategy_version,
        strategy=strategy,
        query=query,
        cron=data.cron.strip(),
        timezone=data.timezone,
        enabled=data.enabled,
        baseline_snapshot={
            "digest": package["body_sha256"],
            "reports": rules.snapshot_from_package(package["body"]),
        },
        baseline_digest=package["body_sha256"],
    )
    await _insert_version(db, row, key, fingerprint)
    await db.commit()
    return await _schedule(db, collection_id, schedule_id), False


async def version_schedule(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    schedule_id: UUID,
    data: SearchScheduleVersionCreate,
) -> tuple[SearchScheduleResponse, bool]:
    """SUPERVISE: edit, enable or disable as a new version on the tip the
    caller saw (409 when it moved). A new strategy is re-pinned and passes
    the protocol check; the saver becomes the owner the schedule runs as."""
    _require_owner_access(context)
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(
        db, context, f"version:{schedule_id}", data, actor_id
    )
    if replay is not None:
        return await _schedule(db, collection_id, schedule_id), True
    tip = (
        await db.execute(
            _tips().where(
                S.collection_id == collection_id, S.schedule_id == schedule_id
            )
        )
    ).scalar_one_or_none()
    if tip is None:
        raise HTTPException(status_code=404, detail=SCHEDULE_NOT_FOUND)
    if tip.id != data.expected_tip_id:
        raise HTTPException(status_code=409, detail=STALE_TIP)
    cron = (data.cron or tip.cron).strip()
    timezone_name = data.timezone or tip.timezone
    _parse(cron, timezone_name)
    pin = (tip.source_run_id, tip.step_id, tip.strategy_version)
    strategy, query, protocol_id = tip.strategy, tip.query, tip.protocol_version_id
    if data.strategy_version is not None:
        pin = (
            cast(UUID, data.source_run_id),
            cast(str, data.step_id),
            data.strategy_version,
        )
        strategy, query, protocol_id = await _pinned(db, context, *pin)
    row = ResearchSearchSchedule(
        id=uuid4(),
        schedule_id=schedule_id,
        collection_id=collection_id,
        owner_id=actor_id,
        protocol_version_id=protocol_id,
        source_run_id=pin[0],
        step_id=pin[1],
        strategy_version=pin[2],
        strategy=strategy,
        query=query,
        cron=cron,
        timezone=timezone_name,
        enabled=tip.enabled if data.enabled is None else data.enabled,
        supersedes_schedule_version_id=tip.id,
    )
    await _insert_version(db, row, key, fingerprint)
    await db.commit()
    return await _schedule(db, collection_id, schedule_id), False


# --- Worker: claim -----------------------------------------------------------


async def insert_fire(
    db: AsyncSession, tip: Any, fire: tuple[datetime, datetime, int]
) -> UUID | None:
    """Insert one claimed fire; ``None`` when it was already claimed. The
    unique key is the guard even if the tip lock was lost."""
    local, utc, missed = fire
    return cast(
        UUID | None,
        (
            await db.execute(
                pg_insert(E)
                .values(
                    id=uuid4(),
                    collection_id=tip.collection_id,
                    schedule_id=tip.schedule_id,
                    schedule_version_id=tip.id,
                    scheduled_local=local.strftime(rules.LOCAL_FORMAT),
                    scheduled_for=utc,
                    missed_fires=missed,
                )
                .on_conflict_do_nothing(
                    index_elements=["schedule_id", "scheduled_local"]
                )
                .returning(E.id)
            )
        ).scalar_one_or_none(),
    )


async def claim_due(db: AsyncSession, now: datetime | None = None) -> list[UUID]:
    """Claim the due fire of every enabled schedule; commits. Two beats skip
    each other's locked tips; the unique fire key absorbs the rest."""
    now = now or _now()
    tips = await _all(
        db,
        _tips()
        .where(S.enabled.is_(True))
        .order_by(S.created_at, S.id)
        .with_for_update(skip_locked=True, of=S),
    )
    claimed: list[UUID] = []
    for tip in tips:
        last = await db.scalar(
            select(func.max(E.scheduled_local)).where(E.schedule_id == tip.schedule_id)
        )
        cron, tz = rules.parse_schedule(tip.cron, tip.timezone)
        fire = rules.due_fire(
            cron,
            tz,
            None if last is None else datetime.strptime(last, rules.LOCAL_FORMAT),
            rules.local_now(tip.created_at, tz),
            now,
        )
        if fire is not None and (execution := await insert_fire(db, tip, fire)):
            claimed.append(execution)
    await db.commit()
    return claimed


async def resumable(db: AsyncSession, limit: int = 20) -> list[UUID]:
    """Executions with no terminal attempt (never started, or a worker died
    mid-run); ``run_execution`` decides whether one is stale."""
    terminal = exists().where(A.execution_id == E.id, A.outcome.in_(TERMINAL))
    ids = await _all(
        db, select(E.id).where(~terminal).order_by(E.created_at).limit(limit)
    )
    await db.rollback()
    return [cast(UUID, i) for i in ids]


# --- Worker: run -------------------------------------------------------------


async def _attempt(
    db: AsyncSession,
    execution_id: UUID,
    outcome: str,
    worker: str,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        ResearchSearchExecutionAttempt(
            id=uuid4(),
            execution_id=execution_id,
            outcome=outcome,
            reason=reason,
            detail=detail,
            worker=worker,
        )
    )
    await db.flush()


async def _start(
    db: AsyncSession, execution_id: UUID, now: datetime, worker: str
) -> tuple[str, dict[str, Any] | None]:
    """Lock the execution and record ``started`` (or why not); commits."""
    execution = (
        await db.execute(
            select(E)
            .where(E.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if execution is None:
        await db.rollback()
        return "gone", None
    attempts = await _all(
        db, select(A).where(A.execution_id == execution_id).order_by(A.created_at)
    )
    terminal = [a for a in attempts if a.outcome in TERMINAL]
    if terminal:
        outcome = cast(str, terminal[-1].outcome)
        await db.rollback()
        return outcome, None
    started = [a for a in attempts if a.outcome == "started"]
    if started and now - started[-1].created_at < STALE_EXECUTION:
        await db.rollback()
        return "running", None
    version = await db.get(S, execution.schedule_version_id)
    tip = (
        await db.execute(_tips().where(S.schedule_id == execution.schedule_id))
    ).scalar_one()
    if not tip.enabled:
        await _attempt(db, execution_id, "skipped", worker, "schedule_disabled")
        await db.commit()
        return "skipped", None
    others = await _all(
        db,
        select(A)
        .join(E, E.id == A.execution_id)
        .where(E.schedule_id == execution.schedule_id, E.id != execution_id)
        .order_by(A.created_at),
    )
    by_execution: dict[Any, list[Any]] = {}
    for attempt in others:
        by_execution.setdefault(attempt.execution_id, []).append(attempt)
    for other_id, rows in by_execution.items():
        if any(r.outcome in TERMINAL for r in rows):
            continue
        if now - rows[-1].created_at < STALE_EXECUTION:
            await _attempt(
                db,
                execution_id,
                "skipped",
                worker,
                "overlap",
                {"running_execution_id": str(other_id)},
            )
            await db.commit()
            return "skipped", None
    detail = {"retry_of": str(started[-1].id)} if started else None
    await _attempt(db, execution_id, "started", worker, detail=detail)
    pinned = {
        "execution_id": execution_id,
        "collection_id": execution.collection_id,
        "schedule_id": execution.schedule_id,
        "schedule_version_id": execution.schedule_version_id,
        "scheduled_local": execution.scheduled_local,
        "owner_id": version.owner_id,
        "protocol_version_id": str(version.protocol_version_id),
        "strategy_version": version.strategy_version,
        "strategy": dict(version.strategy),
        "query": version.query,
    }
    await db.commit()
    return "started", pinned


async def _recheck(db: AsyncSession, job: Mapping[str, Any]) -> tuple[str | None, Any]:
    """(failure reason, context) as the owner: EDIT on a live project, the
    SUPERVISE role, and the protocol version the schedule was pinned under.
    Holds the project locks on success (the caller ends the transaction)."""
    try:
        context = await resolve_project(
            db, job["collection_id"], job["owner_id"], ResearchAction.EDIT
        )
    except HTTPException as error:
        return (
            PROJECT_ARCHIVED if error.status_code == 409 else OWNER_ACCESS_REVOKED
        ), None
    if ResearchProjectRole.SUPERVISOR not in context.effective_roles:
        return OWNER_ROLE_REVOKED, None
    current = await current_protocol_version_id(db, job["collection_id"])
    if current != job["protocol_version_id"]:
        return PROTOCOL_CHANGED, None
    return None, context


async def _fail(
    db: AsyncSession,
    job: Mapping[str, Any],
    worker: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> str:
    await db.rollback()
    await _attempt(db, job["execution_id"], "failed", worker, reason, detail)
    await db.commit()
    return "failed"


async def _receipt(db: AsyncSession, job: Mapping[str, Any]) -> Any:
    return (
        await db.execute(
            select(ResearchImportReceipt).where(
                ResearchImportReceipt.collection_id == job["collection_id"],
                ResearchImportReceipt.dedup_key == f"scheduled:{job['execution_id']}",
            )
        )
    ).scalar_one_or_none()


def _saved(receipt: Any) -> tuple[Any, dict[str, Any]] | None:
    """(id, coverage) read before the session expires the receipt."""
    if receipt is None:
        return None
    return receipt.id, dict((receipt.observed or {}).get("coverage") or {})


def _records(collection_id: UUID, documents: Sequence[Any]) -> list[Any]:
    """One import record per provider result (workspace documents skipped:
    they keep their own org-scoped identity)."""
    records: list[Any] = []
    for document in documents:
        if document.connector_type == "rag_store":
            continue
        metadata = document.metadata or {}
        parsed = {
            **{
                k: v
                for k, v in rules.source_record(
                    {
                        "title": document.title,
                        "authors": document.authors,
                        "metadata": metadata,
                    }
                ).items()
                if v not in (None, "", [])
            },
            "identifiers": dict(metadata.get("identifiers") or {}),
            "providers": sorted(
                {
                    str(p.get("connector_type"))
                    for p in metadata.get("provenance") or []
                    if p.get("connector_type")
                }
                or {document.connector_type}
            ),
            "url": document.url,
        }
        raw = json.dumps(
            {
                "connector_type": document.connector_type,
                "external_id": document.external_id,
                "parsed": parsed,
            },
            sort_keys=True,
            default=str,
        )
        records.append(
            ResearchImportRecord(
                id=uuid4(),
                collection_id=collection_id,
                record_index=len(records),
                status="accepted" if document.title else "rejected",
                rejection_reason=None if document.title else "missing_title",
                raw=raw,
                parsed=parsed,
            )
        )
    return records


async def _search(
    job: Mapping[str, Any], connectors: Mapping[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    strategy = job["strategy"]
    providers = list((strategy.get("intended") or {}).get("selected_providers") or [])
    limit = int((strategy.get("route_limits") or {})["requested_results_per_provider"])
    return await search_sources(
        dict(connectors),
        providers,
        job["query"],
        limit,
        execution_namespace=(
            f"schedule:{job['execution_id']}:{job['strategy_version']}"
        ),
    )


async def _import(
    db: AsyncSession,
    job: Mapping[str, Any],
    documents: Sequence[Any],
    coverage: dict[str, Any],
) -> Any:
    return await corpus_service.insert_receipt(
        db,
        collection_id=job["collection_id"],
        actor_user_id=job["owner_id"],
        kind=RECEIPT_KIND,
        dedup_key=f"scheduled:{job['execution_id']}",
        lineage_key=f"schedule:{job['schedule_id']}",
        declared={
            "schedule_id": str(job["schedule_id"]),
            "schedule_version_id": str(job["schedule_version_id"]),
            "execution_id": str(job["execution_id"]),
            "scheduled_local": job["scheduled_local"],
            "strategy_version": job["strategy_version"],
            "strategy": job["strategy"],
            "query": job["query"],
        },
        observed={
            "executed_at": _now().isoformat(),
            "provider": "scheduled_search",
            "coverage": coverage,
            "execution_namespace": (
                f"schedule:{job['execution_id']}:{job['strategy_version']}"
            ),
            "protocol_version_id": job["protocol_version_id"],
        },
        records=list(_records(job["collection_id"], documents)),
    )


async def _current(db: AsyncSession, receipt_id: Any) -> dict[str, dict[str, Any]]:
    """``report_id -> parsed record`` for this execution's accepted records."""
    rows = await _all(
        db,
        select(ResearchImportRecord)
        .where(
            ResearchImportRecord.receipt_id == receipt_id,
            ResearchImportRecord.status == "accepted",
        )
        .order_by(ResearchImportRecord.record_index),
    )
    return {str(r.report_id): dict(r.parsed or {}) for r in rows if r.report_id}


async def _baseline(
    db: AsyncSession, job: Mapping[str, Any]
) -> tuple[UUID | None, dict[str, Any]]:
    """The explicit prior snapshot: the latest succeeded execution of this
    schedule, else the root version's baseline."""
    previous = (
        await db.execute(
            select(R)
            .join(E, E.id == R.execution_id)
            .where(E.schedule_id == job["schedule_id"], E.id != job["execution_id"])
            .order_by(R.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is not None:
        return cast(UUID, previous.execution_id), dict(previous.corpus_snapshot)
    root = await db.get(S, job["schedule_id"])
    return None, dict(root.baseline_snapshot)


def _dois(
    baseline: Mapping[str, Any], current: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    found = {
        doi
        for entry in (baseline.get("reports") or {}).values()
        for doi in (entry.get("identifiers") or {}).get("doi") or []
    }
    found |= {
        str(record["identifiers"]["doi"]).lower()
        for record in current.values()
        if (record.get("identifiers") or {}).get("doi")
    }
    return sorted(found)


async def _notices(
    connectors: Mapping[str, Any], dois: Sequence[str]
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """(notices for checked DOIs, DOIs whose check failed). DOIs beyond
    ``MAX_NOTICE_DOIS`` are neither: their check was not performed."""
    checked = list(dois[:MAX_NOTICE_DOIS])
    client: Any = connectors.get("crossref")
    if not hasattr(client, "update_notices"):
        client = CrossrefConnector(mailto=settings.CROSSREF_MAILTO)
    try:
        found = await client.update_notices(checked)
    except Exception as error:  # noqa: BLE001 - an outage is unknown, recorded
        logger.warning("crossref notices failed: %s", type(error).__name__)
        found = {}
    return dict(found), set(checked) - set(found)


async def _chase(
    db: AsyncSession,
    job: Mapping[str, Any],
    seeds: Sequence[str],
    connectors: Mapping[str, Any],
) -> dict[str, Any]:
    """Citation chasing only when the protocol requires it (GOO-300's
    chase, one receipt per seed and direction, each committed)."""
    _version, requirement = await corpus_service._protocol_requirement(
        db, job["collection_id"]
    )
    await db.rollback()
    requirement = requirement if isinstance(requirement, dict) else {}
    if not requirement.get("required"):
        return {"required": False, "status": "not_required"}
    directions = [
        d
        for d in requirement.get("directions") or []
        if d
        in (
            "backward",
            "forward",
        )
    ]
    chased: list[dict[str, Any]] = []
    # The run's OpenAlex connector (a fake in tests), never a fresh client.
    openalex = connectors.get("openalex")
    connector = openalex if hasattr(openalex, "citations") else None
    for seed in seeds[:MAX_CHASE_SEEDS]:
        for direction in directions:
            outcome: dict[str, Any] = {"seed_report_id": seed, "direction": direction}
            try:
                receipt, _created = await corpus_service.chase_citations(
                    db,
                    project_id=job["collection_id"],
                    user_id=job["owner_id"],
                    data=CitationChaseRequest(
                        seed_report_id=UUID(seed),
                        direction=direction,
                        idempotency_key=(
                            f"scheduled:{job['execution_id']}:{seed}:{direction}"
                        ),
                    ),
                    connector=connector,
                )
                await db.commit()
                outcome["receipt_id"] = str(receipt.id)
            except HTTPException as error:
                await db.rollback()
                detail = error.detail
                outcome["error"] = (
                    detail.get("code") if isinstance(detail, dict) else str(detail)
                )
            chased.append(outcome)
    return {
        "required": True,
        "status": "performed",
        "directions": directions,
        "chases": chased,
        "seeds_over_cap": max(len(seeds) - MAX_CHASE_SEEDS, 0),
    }


async def _merged_into(db: AsyncSession, collection_id: UUID) -> dict[str, str]:
    return {
        str(rid): str(into)
        for rid, into in (
            await db.execute(
                select(ResearchReport.id, ResearchReport.merged_into_report_id).where(
                    ResearchReport.collection_id == collection_id,
                    ResearchReport.merged_into_report_id.is_not(None),
                )
            )
        ).all()
    }


def _survivor(merged_into: Mapping[str, str], report_id: str) -> str:
    target, seen = report_id, set()
    while target in merged_into and target not in seen:
        seen.add(target)
        target = merged_into[target]
    return target


def _merges(
    merged_into: Mapping[str, str],
    baseline: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Mapping[str, Any]],
) -> dict[str, str | None]:
    """Baseline reports that are no longer themselves: merged (followed to
    the live survivor) or split (an identifier now on another live report,
    unresolved)."""
    owner = {
        (kind, value): rid
        for rid, entry in snapshot.items()
        for kind, values in (entry.get("identifiers") or {}).items()
        for value in values
    }
    merges: dict[str, str | None] = {}
    for rid, entry in baseline.items():
        target = _survivor(merged_into, rid)
        if target not in snapshot:
            merges[rid] = None
            continue
        pairs = {
            (kind, value)
            for kind, values in (entry.get("identifiers") or {}).items()
            for value in values
        }
        if any(owner.get(pair, target) != target for pair in pairs):
            merges[rid] = None
        elif target != rid:
            merges[rid] = target
    return merges


async def _finish(
    db: AsyncSession,
    job: Mapping[str, Any],
    worker: str,
    receipt_id: Any,
    coverage: dict[str, Any],
    notices: dict[str, list[dict[str, Any]]],
    failed: set[str],
    chasing: dict[str, Any],
) -> str:
    """Snapshot, classify and record, under the owner's recheck; commits."""
    reason, context = await _recheck(db, job)
    if reason is not None:
        return await _fail(
            db, job, worker, reason, {"import_receipt_id": str(receipt_id)}
        )
    await lock_aggregate_stream(
        db,
        collection_id=job["collection_id"],
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=job["collection_id"],
    )
    if await db.get(R, job["execution_id"]) is not None:
        await db.rollback()
        return "succeeded"
    baseline_id, baseline = await _baseline(db, job)
    package = await corpus_export.build_package(db, context)
    snapshot = rules.snapshot_from_package(package["body"])
    records = await _current(db, receipt_id)
    baseline_reports = dict(baseline.get("reports") or {})
    merged_into = await _merged_into(db, job["collection_id"])
    current: dict[str, dict[str, Any]] = {}
    for rid, record in records.items():
        live = _survivor(merged_into, rid)
        if live in snapshot:
            current[live] = rules.snapshot_entry(snapshot[live]["identifiers"], record)
    delta = rules.classify(
        baseline_reports,
        current,
        coverage,
        notices,
        failed,
        merges=_merges(merged_into, baseline_reports, snapshot),
    )
    delta_hash = rules.canonical_sha256(
        {
            "baseline_execution_id": None if baseline_id is None else str(baseline_id),
            "import_receipt_id": str(receipt_id),
            "classes": delta,
        }
    )
    db.add(
        ResearchSearchExecutionResult(
            execution_id=job["execution_id"],
            baseline_execution_id=baseline_id,
            import_receipt_id=receipt_id,
            citation_chasing=chasing,
            coverage=coverage,
            corpus_snapshot={"digest": package["body_sha256"], "reports": snapshot},
            delta=delta,
            delta_hash=delta_hash,
        )
    )
    await _attempt(
        db,
        job["execution_id"],
        "succeeded",
        worker,
        detail={"delta_hash": delta_hash, "counts": delta["counts"]},
    )
    payload = {
        "schedule_id": str(job["schedule_id"]),
        "schedule_version_id": str(job["schedule_version_id"]),
        "execution_id": str(job["execution_id"]),
        "scheduled_local": job["scheduled_local"],
        "import_receipt_id": str(receipt_id),
        "baseline_execution_id": None if baseline_id is None else str(baseline_id),
        "delta_hash": delta_hash,
        "counts": delta["counts"],
    }
    await _append(
        db,
        job["collection_id"],
        event_type="search_update.executed",
        subject_id=job["execution_id"],
        actor_id=job["owner_id"],
        actor_role="machine",
        payload=payload,
        key=f"executed:{job['execution_id']}",
        fingerprint=decision_request_fingerprint(payload),
    )
    await db.commit()
    return "succeeded"


async def run_execution(
    db: AsyncSession,
    execution_id: UUID,
    connectors_factory: ConnectorsFactory,
    *,
    now: datetime | None = None,
    worker: str | None = None,
) -> str:
    """Run one claimed execution to a terminal attempt; returns the outcome
    (``running`` when another live worker holds it). A crash leaves only the
    ``started`` attempt, which goes stale and is retried."""
    worker = worker or _worker()
    state, job = await _start(db, execution_id, now or _now(), worker)
    if job is None:
        return state
    reason, context = await _recheck(db, job)
    if reason is not None:
        return await _fail(db, job, worker, reason)
    organization_id = context.organization_id
    saved = _saved(await _receipt(db, job))
    await db.rollback()  # no project lock is held across provider I/O
    connectors = connectors_factory(organization_id)
    if saved is None:
        try:
            documents, coverage = await _search(job, connectors)
        except (RuntimeError, ValueError) as error:
            # All providers failed, or the strategy names an unknown one.
            return await _fail(
                db, job, worker, "providers_failed", {"error": type(error).__name__}
            )
        reason, _context = await _recheck(db, job)
        if reason is not None:
            return await _fail(db, job, worker, reason)
        saved = _saved(
            await _receipt(db, job) or await _import(db, job, documents, coverage)
        )
        await db.commit()
    receipt_id, coverage = cast(tuple[Any, dict[str, Any]], saved)
    baseline_id, baseline = await _baseline(db, job)
    records = await _current(db, receipt_id)
    await db.rollback()
    notices, failed = await _notices(connectors, _dois(baseline, records))
    known = set((baseline.get("reports") or {}))
    seeds = sorted(rid for rid in records if rid not in known)
    chasing = await _chase(db, job, seeds, connectors)
    return await _finish(
        db, job, worker, receipt_id, coverage, notices, failed, chasing
    )
