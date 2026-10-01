"""Superseding review versions with reconciled update accounting (GOO-320).

A supervisor freezes an immutable **review version**. The root (number 1)
freezes the project as it stands: live report ids, every current screening
resolution tip, the governing protocol version and the PRISMA body hash. A
**successor** names its parent and accepts exactly one GOO-319 delta by
``(execution_id, delta_hash)``; the delta's baseline must be the execution
the parent accepted (none for the root's first successor). It is never a
``ResearchRun`` (one engine execution) nor ``GeneratedDraft.version``.

Writers follow the route's ``resolve_project`` (SUPERVISE: Workspace SHARE ->
Collection UPDATE), then this Collection's ``research_review_update`` stream,
replay an identical retry, read, apply ``review_update_rules`` and insert the
version plus its event in **one commit**. ``UNIQUE(parent_review_version_id)``
is the only fork guard: a second successor of one parent loses (409 stale).

Targeted work goes through GOO-301/302 unchanged: after the version commit,
``screening_service.create_queue`` (key ``review-version:{id}:{stage}``) and
``assign`` per named reviewer, each re-locked and committed on its own; the
queue is looked up by that key first, so a retry (``ensure_work``) never makes
a second queue. Full-text work waits until the title/abstract work is settled
(its includes join the full-text set). Parent queues are never touched: a
re-reviewed report's parent resolution stays the tip of its parent queue and
is exported as the ``predecessor``.

Derived on read, never stamped: work status, accounting (each version's
flow from ``prisma.derive_prisma_flow`` over retained rows restricted to the
records and decisions it froze) and staleness (``graph_part`` adds
``report -> document`` edges for the tip's changed or corrected reports;
``draft_release_service._graph`` bridges each document to its pinned source
revisions, so GOO-307's walk stales only their dependents).

# ponytail: out of scope, each at its seam: automatic delta acceptance;
# carrying extraction or appraisal values (they re-stale and are re-accepted
# through GOO-304/309); branching versions; a persisted stale flag;
# living-review cadence rules; depositing a superseding release as a Zenodo
# new version (GOO-318's seam).
"""

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.manuscript_release import ManuscriptRelease
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportRecord
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_report import ResearchReport, ResearchReportIdentifier
from src.models.research_review_version import (
    ResearchReviewReleaseLink,
    ResearchReviewVersion,
)
from src.models.research_search_update import ResearchSearchSchedule
from src.models.screening import (
    ScreeningAssignment,
    ScreeningObservation,
    ScreeningQueue,
    ScreeningResolution,
)
from src.models.user import User
from src.schemas.research_engine import (
    ReviewDeltaOption,
    ReviewReleaseLinkCreate,
    ReviewReleaseLinkResponse,
    ReviewVersionCreate,
    ReviewVersionListResponse,
    ReviewVersionResponse,
    ReviewWorkStatus,
    ScreeningAssignmentCreate,
    ScreeningQueueCreate,
    UpdateAccountingResponse,
)
from src.services.research import release_rules
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import corpus_export, prisma, prisma_service
from src.services.research_engine import review_update_rules as rules
from src.services.research_engine import (
    screening_rules,
    screening_service,
    search_update_service,
)
from src.services.research_engine.acquisition_service import document_reports
from src.services.research_engine.identity_service import (
    _replayed_event,
    _require_role,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)

logger = logging.getLogger(__name__)

AGGREGATE_TYPE = "research_review_update"
SUBJECT_TYPE = "review_version"
EXPORT_SCHEMA = "nous.academic.review-version.v1"
BUNDLE_SCHEMA = "nous.academic.review-versions.v1"
TA, FT = rules.STAGES

VERSION_NOT_FOUND = "Review version not found"
STALE_VERSION = "Review version is stale; reload"
ROOT_EXISTS = "Review versions already exist; create a successor"
DELTA_CHANGED = "Delta changed; reload"
DELTA_OUT_OF_ORDER = "Delta does not follow the parent version's delta"
DELTA_ACCEPTED = "Delta already accepted by a review version"
PARENT_UNRESOLVED = "Parent review work is unresolved"
NO_PROTOCOL = "Approve a protocol before versioning the review"
NOT_UNKNOWN = "Only unknown reports can be carried with uncertainty"
NOT_REVIEWER = "User is not an eligible reviewer"
NOT_RECONCILED = "Update accounting does not reconcile"
RELEASE_NOT_FOUND = "Release not found"
RELEASE_NOT_VERIFIED = "Only a verified release can be linked"
RELEASE_TOO_OLD = "A successor's release must be created after the version"
RELEASE_LINKED = "Review version already has a release"
WRONG_SUPERSEDED = "The release must supersede the parent version's release"

V = ResearchReviewVersion
L = ResearchReviewReleaseLink


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _work_key(version_id: Any, stage: str) -> str:
    return f"review-version:{version_id}:{stage}"


def _ids(values: Iterable[Any]) -> set[str]:
    return {str(v) for v in values}


# --- Screening decisions ------------------------------------------------------


class _Decisions:
    """Every resolution of the Collection with its stage and event seq."""

    def __init__(self, rows: Sequence[tuple[Any, str, int]]) -> None:
        self.rows = {str(r.id): (r, stage, seq) for r, stage, seq in rows}
        superseded = {str(r.supersedes_resolution_id) for r, _, _ in rows}
        self.next = {
            str(r.supersedes_resolution_id): str(r.id)
            for r, _, _ in rows
            if r.supersedes_resolution_id is not None
        }
        self.tips = {rid for rid in self.rows if rid not in superseded}

    def tip_of(self, resolution_id: str) -> str:
        """Follow a frozen reference to the current tip of its own chain
        (a later reopen/adjudication in the same queue)."""
        seen: set[str] = set()
        while resolution_id in self.next and resolution_id not in seen:
            seen.add(resolution_id)
            resolution_id = self.next[resolution_id]
        return resolution_id

    def chain(self, resolution_id: str) -> list[str]:
        out = []
        current: str | None = resolution_id
        while current is not None and current in self.rows:
            out.append(current)
            previous = self.rows[current][0].supersedes_resolution_id
            current = None if previous is None else str(previous)
        return out

    def parent(self, resolution_id: str) -> rules.ParentDecision:
        row, stage, _seq = self.rows[resolution_id]
        return rules.ParentDecision(
            report_id=cast(UUID, row.report_id),
            stage=stage,
            resolution_id=cast(UUID, row.id),
            event_id=cast(UUID, row.event_id),
            outcome=cast(str | None, row.outcome),
            basis=cast(str, row.basis),
            criteria_hash=cast(str, row.criteria_hash),
        )

    def outcomes(self, tips: Iterable[str]) -> tuple[prisma.Outcome, ...]:
        """PRISMA ``Outcome`` rows for every chain ending at ``tips``."""
        out: dict[str, prisma.Outcome] = {}
        for tip in tips:
            for rid in self.chain(tip):
                row, stage, seq = self.rows[rid]
                resolved = row.basis in rules.RESOLVED_BASES
                out[rid] = prisma.Outcome(
                    stage,
                    row.report_id,
                    row.outcome if resolved else None,
                    row.exclusion_reason if resolved else None,
                    row.event_id,
                    seq,
                    row.supersedes_resolution_id is not None,
                    row.basis,
                    row.created_at,
                )
        return tuple(out.values())


async def _decisions(db: AsyncSession, collection_id: UUID) -> _Decisions:
    rows = (
        await db.execute(
            select(ScreeningResolution, ScreeningQueue.stage, ResearchDecisionEvent.seq)
            .join(ScreeningQueue, ScreeningQueue.id == ScreeningResolution.queue_id)
            .join(
                ResearchDecisionEvent,
                ResearchDecisionEvent.id == ScreeningResolution.event_id,
            )
            .where(ScreeningQueue.collection_id == collection_id)
        )
    ).all()
    return _Decisions([(r, str(stage), int(seq)) for r, stage, seq in rows])


async def _queues(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(ScreeningQueue)
        .where(ScreeningQueue.collection_id == collection_id)
        .order_by(ScreeningQueue.created_at, ScreeningQueue.id),
    )


def _chosen_tips(queues: Sequence[Any], decisions: _Decisions) -> list[str]:
    """``prisma_service.read_inputs``'s rule: per (stage, report) the latest
    non-superseded queue containing it, and that queue's tip for it."""
    superseded = {str(q.supersedes_queue_id) for q in queues}
    chosen: dict[tuple[str, str], str] = {}
    for queue in queues:
        if str(queue.id) not in superseded:
            for report in queue.report_ids:
                chosen[(str(queue.stage), str(report))] = str(queue.id)
    return sorted(
        rid
        for rid in decisions.tips
        if chosen.get((decisions.rows[rid][1], str(decisions.rows[rid][0].report_id)))
        == str(decisions.rows[rid][0].queue_id)
    )


async def _work_queues(
    db: AsyncSession, collection_id: UUID, version_ids: Iterable[Any]
) -> dict[str, Any]:
    """``work key -> queue`` for the targeted queues of these versions."""
    keys = [_work_key(v, s) for v in version_ids for s in rules.STAGES]
    if not keys:
        return {}
    events = (
        await db.execute(
            select(ResearchDecisionEvent.idempotency_key, ResearchDecisionEvent.payload)
            .join(
                ResearchDecisionStream,
                ResearchDecisionStream.id == ResearchDecisionEvent.stream_id,
            )
            .where(
                ResearchDecisionStream.collection_id == collection_id,
                ResearchDecisionEvent.event_type == "screening.queue_created",
                ResearchDecisionEvent.idempotency_key.in_(keys),
            )
        )
    ).all()
    by_id = {str(payload["queue_id"]): str(key) for key, payload in events}
    if not by_id:
        return {}
    queues = await _all(
        db,
        select(ScreeningQueue).where(
            ScreeningQueue.id.in_([UUID(q) for q in by_id]),
            ScreeningQueue.collection_id == collection_id,
        ),
    )
    return {by_id[str(q.id)]: q for q in queues}


def _queue_tips(queue: Any, decisions: _Decisions) -> dict[str, str]:
    """``report_id -> tip resolution id`` inside one queue."""
    return {
        str(decisions.rows[rid][0].report_id): rid
        for rid in decisions.tips
        if str(decisions.rows[rid][0].queue_id) == str(queue.id)
    }


async def _assignments(db: AsyncSession, queue_ids: Sequence[Any]) -> dict[str, list]:
    out: dict[str, list[Any]] = {str(q): [] for q in queue_ids}
    if queue_ids:
        for row in await _all(
            db,
            select(ScreeningAssignment).where(
                ScreeningAssignment.queue_id.in_(list(queue_ids)),
                ScreeningAssignment.revoked_at.is_(None),
            ),
        ):
            out[str(row.queue_id)].append(row.reviewer_id)
    return out


def _work(
    version: Any,
    queues: Mapping[str, Any],
    decisions: _Decisions,
    assigned: Mapping[str, list[Any]],
) -> list[ReviewWorkStatus]:
    """Derived per stage. Full text = the frozen full-text set plus this
    version's new title/abstract includes, once title/abstract is settled."""
    required_work = cast(dict[str, list[str]], version.required_work)
    statuses: list[ReviewWorkStatus] = []
    ta_settled = True
    ta_includes: set[str] = set()
    for stage in rules.STAGES:
        queue = queues.get(_work_key(version.id, stage))
        tips = {} if queue is None else _queue_tips(queue, decisions)
        if stage == TA:
            required = set(required_work.get(TA) or [])
        else:
            required = set(required_work.get(FT) or []) | ta_includes
            if not ta_settled:
                statuses.append(
                    ReviewWorkStatus(
                        stage=cast(Any, stage),
                        status="waiting_on_title_abstract",
                        required_report_ids=[UUID(r) for r in sorted(required)],
                    )
                )
                continue
        resolved = {
            report
            for report, rid in tips.items()
            if report in required
            and decisions.rows[rid][0].basis in rules.RESOLVED_BASES
        }
        if not required:
            state = "none"
        elif queue is None:
            state = "queue_missing"
        elif _ids(queue.report_ids) != required:
            state = "queue_mismatch"
        else:
            state = "queued"
        statuses.append(
            ReviewWorkStatus(
                stage=cast(Any, stage),
                status=cast(Any, state),
                required_report_ids=[UUID(r) for r in sorted(required)],
                queue_id=None if queue is None else queue.id,
                assigned_reviewer_ids=(
                    [] if queue is None else assigned.get(str(queue.id), [])
                ),
                resolved_count=len(resolved),
                unresolved_count=len(required) - len(resolved),
            )
        )
        if stage == TA:
            ta_settled = state == "none" or (state == "queued" and resolved == required)
            ta_includes = {
                report
                for report in resolved
                if decisions.rows[tips[report]][0].outcome == "include"
            }
    return statuses


def _settled(statuses: Sequence[ReviewWorkStatus]) -> bool:
    return all(
        s.status == "none" or (s.status == "queued" and s.unresolved_count == 0)
        for s in statuses
    )


def _decided_tips(
    version: Any,
    successor: Any | None,
    decisions: _Decisions,
    queues: Mapping[str, Any],
) -> list[str]:
    """The decisions a version rests on: frozen at its successor's creation
    once it has one; otherwise its own references (followed to their chain
    tips) plus the tips of its targeted queues."""
    if successor is not None:
        return list(successor.input_versions["parent_decision_tips"])
    tips = {
        decisions.tip_of(str(rid))
        for rid in version.input_versions["decision_tips"]
        if str(rid) in decisions.rows
    }
    for stage in rules.STAGES:
        queue = queues.get(_work_key(version.id, stage))
        if queue is not None:
            tips |= set(_queue_tips(queue, decisions).values())
    return sorted(tips)


# --- PRISMA flows -------------------------------------------------------------


def _restricted(
    inputs: prisma.PrismaInputs,
    collection_id: UUID,
    version: Any,
    decisions: _Decisions,
    tips: Sequence[str],
    closing: Mapping[str, Any] | None,
) -> prisma.PrismaInputs:
    """The retained rows a version froze: its record keys and decisions;
    full-text attempts and merges up to its successor's creation (if any)."""
    keys = set(version.input_versions["record_keys"])
    attempts, merges = inputs.attempts, inputs.merges
    if closing is not None:
        heads = closing["stream_heads"]
        acq = heads.get(f"research_acquisition:{collection_id}", 0)
        ident = heads.get(f"research_identity:{collection_id}", 0)
        attempts = tuple(a for a in attempts if a.seq <= acq)
        merges = tuple(m for m in merges if m.seq <= ident)
    return replace(
        inputs,
        records=tuple(r for r in inputs.records if r.key in keys),
        outcomes=decisions.outcomes(tips),
        attempts=attempts,
        merges=merges,
        versions={
            "protocol_version_ids": version.input_versions["protocol_version_ids"],
            "stream_heads": version.input_versions["stream_heads"],
        },
    )


def _heads(inputs: prisma.PrismaInputs, collection_id: UUID) -> dict[str, int]:
    """Stream heads without this family's own stream (it moves on append)."""
    own = f"{AGGREGATE_TYPE}:{collection_id}"
    return {k: v for k, v in dict(inputs.versions["stream_heads"]).items() if k != own}


# --- Reads --------------------------------------------------------------------


async def _versions(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(V).where(V.collection_id == collection_id).order_by(V.version_number),
    )


async def _links(db: AsyncSession, collection_id: UUID) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(L, ManuscriptRelease.package_sha256)
            .join(ManuscriptRelease, ManuscriptRelease.id == L.release_id)
            .where(L.collection_id == collection_id)
        )
    ).all()
    return {str(link.review_version_id): (link, sha) for link, sha in rows}


def _link_response(entry: Any) -> ReviewReleaseLinkResponse | None:
    if entry is None:
        return None
    link, sha = entry
    return ReviewReleaseLinkResponse(
        id=link.id,
        review_version_id=link.review_version_id,
        release_id=link.release_id,
        supersedes_release_id=link.supersedes_release_id,
        package_sha256=sha,
        linked_by_id=link.linked_by_id,
        created_at=link.created_at,
    )


class _State:
    """Everything the derived reads need, loaded once."""

    def __init__(self) -> None:
        self.versions: list[Any] = []
        self.decisions = _Decisions([])
        self.queues: dict[str, Any] = {}
        self.assigned: dict[str, list[Any]] = {}
        self.links: dict[str, Any] = {}

    def successor(self, version: Any) -> Any | None:
        return next(
            (
                v
                for v in self.versions
                if v.parent_review_version_id is not None
                and str(v.parent_review_version_id) == str(version.id)
            ),
            None,
        )

    def get(self, version_id: Any) -> Any:
        for version in self.versions:
            if str(version.id) == str(version_id):
                return version
        raise HTTPException(status_code=404, detail=VERSION_NOT_FOUND)

    def work(self, version: Any) -> list[ReviewWorkStatus]:
        return _work(version, self.queues, self.decisions, self.assigned)

    def tips(self, version: Any) -> list[str]:
        return _decided_tips(
            version, self.successor(version), self.decisions, self.queues
        )


async def _state(db: AsyncSession, collection_id: UUID) -> _State:
    state = _State()
    state.versions = await _versions(db, collection_id)
    state.decisions = await _decisions(db, collection_id)
    state.queues = await _work_queues(db, collection_id, [v.id for v in state.versions])
    state.assigned = await _assignments(db, [q.id for q in state.queues.values()])
    state.links = await _links(db, collection_id)
    return state


def _response(
    state: _State, version: Any, stale: Mapping[str, int] | None = None
) -> ReviewVersionResponse:
    return ReviewVersionResponse(
        id=version.id,
        collection_id=version.collection_id,
        version_number=version.version_number,
        parent_review_version_id=version.parent_review_version_id,
        accepted_execution_id=version.accepted_execution_id,
        delta_hash=version.delta_hash,
        protocol_version_id=version.protocol_version_id,
        strategy_version=version.strategy_version,
        report_ids=version.report_ids,
        carried=version.carried,
        required_work=version.required_work,
        needs_attention=version.needs_attention,
        missing_history=version.missing_history,
        prisma_body_hash=version.prisma_body_hash,
        content_hash=version.content_hash,
        rationale=version.rationale,
        created_by_id=version.created_by_id,
        created_at=version.created_at,
        is_tip=state.successor(version) is None,
        work=state.work(version),
        release=_link_response(state.links.get(str(version.id))),
        stale_counts=dict(stale or {}),
    )


async def _stale_counts(db: AsyncSession, collection_id: UUID) -> dict[str, int]:
    """Nodes the tip's accepted delta stales through GOO-307's walk."""
    from src.services.research import draft_release_service

    _edges, changed = await graph_part(db, collection_id)
    if not changed:
        return {}
    graph = await draft_release_service._graph(db, collection_id)
    counts: dict[str, int] = {}
    for kind, _value in release_rules.dependents(graph.edges, changed):
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


async def _delta_options(
    db: AsyncSession, collection_id: UUID, state: _State
) -> list[ReviewDeltaOption]:
    """Succeeded executions a successor of the tip could accept."""
    if not state.versions:
        return []
    accepted = {str(v.accepted_execution_id) for v in state.versions}
    tip = state.versions[-1]
    expected = (
        None if tip.accepted_execution_id is None else str(tip.accepted_execution_id)
    )
    options = []
    for execution in await search_update_service._executions(db, collection_id):
        baseline = execution.baseline_execution_id
        if (
            execution.status == "succeeded"
            and execution.delta_hash is not None
            and execution.counts is not None
            and str(execution.id) not in accepted
            and (None if baseline is None else str(baseline)) == expected
        ):
            options.append(
                ReviewDeltaOption(
                    execution_id=execution.id,
                    schedule_id=execution.schedule_id,
                    scheduled_local=execution.scheduled_local,
                    baseline_execution_id=baseline,
                    delta_hash=execution.delta_hash,
                    counts=cast(Any, execution.counts),
                )
            )
    return options


async def list_versions(
    db: AsyncSession, context: ProjectContext
) -> ReviewVersionListResponse:
    """VIEW: the chain with derived work status, the tip's stale counts and
    the deltas a successor could accept."""
    collection_id = _cid(context)
    state = await _state(db, collection_id)
    stale = await _stale_counts(db, collection_id) if state.versions else {}
    return ReviewVersionListResponse(
        versions=[
            _response(state, v, stale if state.successor(v) is None else None)
            for v in state.versions
        ],
        deltas=await _delta_options(db, collection_id, state),
    )


async def _flow(
    db: AsyncSession,
    collection_id: UUID,
    state: _State,
    version: Any,
    inputs: prisma.PrismaInputs,
) -> tuple[dict[str, Any], prisma.PrismaInputs]:
    successor = state.successor(version)
    restricted = _restricted(
        inputs,
        collection_id,
        version,
        state.decisions,
        state.tips(version),
        None if successor is None else successor.input_versions,
    )
    return prisma.derive_prisma_flow(restricted), restricted


async def _accounting(
    db: AsyncSession, collection_id: UUID, state: _State, version: Any
) -> UpdateAccountingResponse:
    inputs = await prisma_service.read_inputs(db, collection_id)
    flow, restricted = await _flow(db, collection_id, state, version, inputs)
    frozen = _restricted(
        inputs,
        collection_id,
        version,
        state.decisions,
        version.input_versions["decision_tips"],
        version.input_versions,
    )
    matches = (
        rules.version_hash(prisma.derive_prisma_flow(frozen))
        == version.prisma_body_hash
    )
    response = UpdateAccountingResponse(
        review_version_id=version.id,
        parent_review_version_id=version.parent_review_version_id,
        flow=flow,
        flow_matches_frozen_hash=matches,
    )
    if version.parent_review_version_id is None:
        return response
    parent = state.get(version.parent_review_version_id)
    parent_flow, parent_inputs = await _flow(db, collection_id, state, parent, inputs)
    response.parent_flow = parent_flow
    if not _settled(state.work(version)):
        response.error = "Review work is unresolved"
        return response
    delta = cast(dict[str, Any], version.input_versions["accepted_delta"])
    try:
        response.boxes = rules.accounting(
            parent_flow=parent_flow,
            successor_flow=flow,
            delta_counts=delta["counts"],
            carried=cast(list[dict[str, Any]], version.carried),
            parent_keys=parent.input_versions["record_keys"],
            successor_keys=version.input_versions["record_keys"],
            parent_units=rules.included_units(parent_inputs),
            successor_units=rules.included_units(restricted),
            parent_reports=_ids(parent.report_ids),
            withheld={str(a["report_id"]) for a in version.needs_attention},
        )
    except prisma.PrismaInconsistency as error:
        logger.warning("review accounting failed: %s", error)
        response.error = NOT_RECONCILED
    return response


async def accounting(
    db: AsyncSession, context: ProjectContext, version_id: UUID
) -> UpdateAccountingResponse:
    """VIEW: both PRISMA bodies and the update boxes (409 when they do not
    reconcile)."""
    collection_id = _cid(context)
    state = await _state(db, collection_id)
    response = await _accounting(db, collection_id, state, state.get(version_id))
    if response.error == NOT_RECONCILED:
        raise HTTPException(status_code=409, detail=NOT_RECONCILED)
    return response


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[
    list[tuple[release_rules.Node, release_rules.Node]], set[release_rules.Node]
]:
    """``report -> document`` for each document of a report the tip version's
    delta classed ``changed`` or ``corrected_retracted``; those reports are
    the changed nodes. Unrelated reports add nothing."""
    tip = (
        await db.execute(
            select(V)
            .where(V.collection_id == collection_id)
            .order_by(V.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if tip is None or tip.accepted_execution_id is None:
        return [], set()
    delta = cast(dict[str, Any], tip.input_versions["accepted_delta"])
    merged_into = await search_update_service._merged_into(db, collection_id)
    affected = {
        search_update_service._survivor(merged_into, str(item["report_id"]))
        for item in delta["items"]
        if item["class"] in ("changed", "corrected_retracted")
    }
    if not affected:
        return [], set()
    edges = [
        (release_rules.node("report", report), release_rules.node("document", doc))
        for doc, report in (await document_reports(db, collection_id)).items()
        if str(report) in affected
    ]
    return edges, {release_rules.node("report", r) for r in affected}


# --- Writers ------------------------------------------------------------------


async def _begin(
    db: AsyncSession,
    collection_id: UUID,
    operation: str,
    data: Any,
    actor: UUID,
) -> tuple[str, str, dict[str, Any] | None]:
    stream = await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
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
    version_id: UUID,
    actor_id: UUID,
    reason: str | None,
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
            actor_role="supervisor",
            subject_type=SUBJECT_TYPE,
            subject_id=version_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


async def _check_reviewers(
    db: AsyncSession, context: ProjectContext, reviewers: Sequence[UUID]
) -> None:
    """GOO-301's assignment eligibility, checked before anything commits."""
    if not reviewers:
        return
    workspace = context.workspace
    members = {workspace.owner_id} | {
        m.user_id for m in workspace.members if not m.is_deleted
    }
    with_role = set(
        await _all(
            db,
            select(ResearchProjectRoleAssignment.user_id).where(
                ResearchProjectRoleAssignment.collection_id == _cid(context),
                ResearchProjectRoleAssignment.user_id.in_(list(reviewers)),
                ResearchProjectRoleAssignment.role == ResearchProjectRole.REVIEWER,
                ResearchProjectRoleAssignment.is_deleted.is_(False),
            ),
        )
    )
    same_org = set(
        await _all(
            db,
            select(User.id).where(
                User.id.in_(list(reviewers)),
                User.organization_id == context.organization_id,
            ),
        )
    )
    if any(
        r not in members or r not in with_role or r not in same_org for r in reviewers
    ):
        raise HTTPException(status_code=422, detail=NOT_REVIEWER)


async def _criteria(db: AsyncSession, protocol_version_id: UUID) -> dict[str, str]:
    version = await db.get(ResearchProtocolVersion, protocol_version_id)
    if version is None:
        raise HTTPException(status_code=409, detail=NO_PROTOCOL)
    value = screening_rules.criteria_hash(cast(dict[str, Any], version.snapshot))
    return {stage: value for stage in rules.STAGES}


async def _live_reports(db: AsyncSession, collection_id: UUID) -> list[UUID]:
    return sorted(
        await _all(
            db,
            select(ResearchReport.id).where(
                ResearchReport.collection_id == collection_id,
                ResearchReport.merged_into_report_id.is_(None),
            ),
        ),
        key=str,
    )


async def _unattributed(
    db: AsyncSession, collection_id: UUID, parents: Sequence[rules.ParentDecision]
) -> set[UUID]:
    """Tips whose event is not a recorded event of this Collection."""
    events = set(
        await _all(
            db,
            select(ResearchDecisionEvent.id).where(
                ResearchDecisionEvent.collection_id == collection_id,
                ResearchDecisionEvent.id.in_([p.event_id for p in parents]),
            ),
        )
    )
    return {p.resolution_id for p in parents if p.event_id not in events}


async def _identity_changed(
    db: AsyncSession, collection_id: UUID, since: int
) -> set[UUID]:
    """Reports merged or split after the parent froze its identity head."""
    rows = (
        await db.execute(
            select(ResearchDecisionEvent.event_type, ResearchDecisionEvent.payload)
            .join(
                ResearchDecisionStream,
                ResearchDecisionStream.id == ResearchDecisionEvent.stream_id,
            )
            .where(
                ResearchDecisionStream.collection_id == collection_id,
                ResearchDecisionStream.aggregate_type == "research_identity",
                ResearchDecisionEvent.seq > since,
                ResearchDecisionEvent.event_type.in_(
                    ["identity.report_merged", "identity.report_split"]
                ),
            )
        )
    ).all()
    changed: set[UUID] = set()
    for event_type, payload in rows:
        if event_type == "identity.report_merged":
            changed.add(UUID(payload["surviving_report_id"]))
            changed |= {UUID(r) for r in payload["merged_report_ids"]}
        else:
            changed |= {
                UUID(payload["source_report_id"]),
                UUID(payload["new_report_id"]),
            }
    return changed


async def _receipt_keys(db: AsyncSession, receipts: Sequence[UUID]) -> set[str]:
    """PRISMA record keys of the accepted delta's own import receipts (the
    scheduled search and its citation chases): nothing imported elsewhere
    joins the version, and a retried import reuses its receipt."""
    ids = await _all(
        db,
        select(ResearchImportRecord.id).where(
            ResearchImportRecord.receipt_id.in_(list(receipts)),
            ResearchImportRecord.status == "accepted",
        ),
    )
    return {f"import:{record_id}" for record_id in ids}


async def _insert(
    db: AsyncSession,
    row: ResearchReviewVersion,
    *,
    key: str,
    fingerprint: str,
) -> None:
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if "execution" in str(error.orig):
            raise HTTPException(status_code=409, detail=DELTA_ACCEPTED) from error
        raise HTTPException(status_code=409, detail=STALE_VERSION) from error
    required = cast(dict[str, list[str]], row.required_work)
    await _append(
        db,
        cast(UUID, row.collection_id),
        event_type="review_update.versioned",
        version_id=cast(UUID, row.id),
        actor_id=cast(UUID, row.created_by_id),
        reason=cast(str, row.rationale),
        payload={
            "review_version_id": str(row.id),
            "parent_review_version_id": (
                None
                if row.parent_review_version_id is None
                else str(row.parent_review_version_id)
            ),
            "version_number": row.version_number,
            "accepted_execution_id": (
                None
                if row.accepted_execution_id is None
                else str(row.accepted_execution_id)
            ),
            "delta_hash": row.delta_hash,
            "protocol_version_id": str(row.protocol_version_id),
            "carried_count": len(cast(list[Any], row.carried)),
            "required_work_counts": {s: len(required[s]) for s in rules.STAGES},
            "needs_attention_count": len(cast(list[Any], row.needs_attention)),
            "content_hash": row.content_hash,
        },
        key=key,
        fingerprint=fingerprint,
    )


def _row(
    *,
    version_id: UUID,
    collection_id: UUID,
    actor_id: UUID,
    body: dict[str, Any],
) -> ResearchReviewVersion:
    columns = {k: v for k, v in body.items() if k != "collection_id"}
    for name in ("parent_review_version_id", "accepted_execution_id"):
        columns[name] = None if body[name] is None else UUID(body[name])
    columns["protocol_version_id"] = UUID(body["protocol_version_id"])
    return ResearchReviewVersion(
        id=version_id,
        collection_id=collection_id,
        created_by_id=actor_id,
        actor_role="supervisor",
        content_hash=rules.version_hash(body),
        **columns,
    )


async def _version_response(
    db: AsyncSession, collection_id: UUID, version_id: Any
) -> ReviewVersionResponse:
    state = await _state(db, collection_id)
    return _response(state, state.get(version_id))


async def create_version(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: ReviewVersionCreate,
) -> tuple[ReviewVersionResponse, bool]:
    """SUPERVISE: a root (no parent) or a successor; ``(version, replayed)``."""
    if data.parent_review_version_id is None:
        return await create_root(db, context, actor_id, data)
    return await create_successor(db, context, actor_id, data)


async def create_root(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: ReviewVersionCreate,
) -> tuple[ReviewVersionResponse, bool]:
    """Freeze the project as it stands as version 1. One commit."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, collection_id, "create", data, actor_id)
    if replay is not None:
        return (
            await _version_response(db, collection_id, replay["review_version_id"]),
            True,
        )
    if await _versions(db, collection_id):
        raise HTTPException(status_code=409, detail=ROOT_EXISTS)
    protocol = await current_protocol_version_id(db, collection_id)
    if protocol is None:
        raise HTTPException(status_code=409, detail=NO_PROTOCOL)
    await _check_reviewers(db, context, data.reviewer_user_ids)
    inputs = await prisma_service.read_inputs(db, collection_id)
    decisions = await _decisions(db, collection_id)
    tips = _chosen_tips(await _queues(db, collection_id), decisions)
    reports = await _live_reports(db, collection_id)
    live = _ids(reports)
    parents = [decisions.parent(t) for t in tips]
    current = [p for p in parents if str(p.report_id) in live]
    heads = _heads(inputs, collection_id)
    input_versions = {
        "record_keys": sorted(r.key for r in inputs.records),
        "stream_heads": heads,
        "protocol_version_ids": inputs.versions["protocol_version_ids"],
        "decision_tips": tips,
        "reviewer_user_ids": [str(r) for r in data.reviewer_user_ids],
    }
    flow = prisma.derive_prisma_flow(
        replace(
            inputs,
            outcomes=decisions.outcomes(tips),
            versions={**inputs.versions, "stream_heads": heads},
        )
    )
    body: dict[str, Any] = {
        "collection_id": str(collection_id),
        "version_number": 1,
        "parent_review_version_id": None,
        "accepted_execution_id": None,
        "delta_hash": None,
        "protocol_version_id": protocol,
        "strategy_version": None,
        "input_versions": input_versions,
        "report_ids": [str(r) for r in reports],
        "carried": [p.ref() for p in current if p.basis in rules.RESOLVED_BASES],
        "required_work": {stage: [] for stage in rules.STAGES},
        "needs_attention": [],
        "missing_history": rules.missing_history(
            reports,
            current,
            unattributed=await _unattributed(db, collection_id, current),
        ),
        "prisma_body_hash": rules.version_hash(flow),
        "rationale": data.rationale,
    }
    version_id = uuid4()
    await _insert(
        db,
        _row(
            version_id=version_id,
            collection_id=collection_id,
            actor_id=actor_id,
            body=body,
        ),
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _version_response(db, collection_id, version_id), False


async def create_successor(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: ReviewVersionCreate,
) -> tuple[ReviewVersionResponse, bool]:
    """Accept one delta on top of ``parent``: carry unchanged decisions by
    reference, freeze the required work, commit once; then create and assign
    the targeted queues (each its own idempotent commit)."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, collection_id, "create", data, actor_id)
    if replay is not None:
        return (
            await _version_response(db, collection_id, replay["review_version_id"]),
            True,
        )
    state = await _state(db, collection_id)
    parent = state.get(data.parent_review_version_id)
    execution_id = cast(UUID, data.execution_id)
    delta = await search_update_service.accepted_delta(db, collection_id, execution_id)
    if delta.delta_hash != data.delta_hash:
        raise HTTPException(status_code=409, detail=DELTA_CHANGED)
    expected = parent.accepted_execution_id
    if (None if expected is None else str(expected)) != (
        None
        if delta.baseline_execution_id is None
        else str(delta.baseline_execution_id)
    ):
        raise HTTPException(status_code=409, detail=DELTA_OUT_OF_ORDER)
    if not _settled(state.work(parent)):
        raise HTTPException(status_code=409, detail=PARENT_UNRESOLVED)
    protocol = await current_protocol_version_id(db, collection_id)
    if protocol is None:
        raise HTTPException(status_code=409, detail=NO_PROTOCOL)
    items = {i.report_id: i.model_dump(mode="json", by_alias=True) for i in delta.items}
    uncertain = set(data.carry_with_uncertainty)
    if any(items.get(str(r), {}).get("class") != "unknown" for r in uncertain):
        raise HTTPException(status_code=422, detail=NOT_UNKNOWN)
    await _check_reviewers(db, context, data.reviewer_user_ids)
    schedule = await db.get(ResearchSearchSchedule, delta.schedule_version_id)
    inputs = await prisma_service.read_inputs(db, collection_id)
    parent_tips = state.tips(parent)
    parents = [state.decisions.parent(t) for t in parent_tips]
    reports = await _live_reports(db, collection_id)
    since = parent.input_versions["stream_heads"].get(
        f"research_identity:{collection_id}", 0
    )
    carried, required, attention = rules.carry_forward(
        items,
        parents,
        await _criteria(db, UUID(protocol)),
        await _identity_changed(db, collection_id, since),
        reports=reports,
        uncertain=uncertain,
    )
    skip = {a["report_id"] for a in attention} | _ids(required[TA])
    candidates = [r for r in reports if str(r) not in skip]
    candidate_ids = _ids(candidates)
    kept = [p for p in parents if str(p.report_id) in candidate_ids]
    heads = _heads(inputs, collection_id)
    tips = [c["resolution_id"] for c in carried]
    receipts = [delta.import_receipt_id] + [
        UUID(c["receipt_id"])
        for c in delta.citation_chasing.get("chases") or []
        if c.get("receipt_id")
    ]
    keys = set(parent.input_versions["record_keys"]) | await _receipt_keys(db, receipts)
    records = tuple(r for r in inputs.records if r.key in keys)
    input_versions = {
        "record_keys": sorted(r.key for r in records),
        "stream_heads": heads,
        "protocol_version_ids": inputs.versions["protocol_version_ids"],
        "decision_tips": tips,
        "parent_decision_tips": parent_tips,
        "reviewer_user_ids": [str(r) for r in data.reviewer_user_ids],
        "accepted_delta": {
            "execution_id": str(delta.execution_id),
            "delta_hash": delta.delta_hash,
            "baseline_execution_id": (
                None
                if delta.baseline_execution_id is None
                else str(delta.baseline_execution_id)
            ),
            "import_receipt_id": str(delta.import_receipt_id),
            "corpus_snapshot_digest": delta.corpus_snapshot_digest,
            "counts": dict(delta.counts),
            "items": [
                {
                    "report_id": i["report_id"],
                    "class": i["class"],
                    "reason": i["reason"],
                }
                for i in items.values()
            ],
        },
    }
    flow = prisma.derive_prisma_flow(
        replace(
            inputs,
            records=records,
            outcomes=state.decisions.outcomes(tips),
            versions={
                "protocol_version_ids": inputs.versions["protocol_version_ids"],
                "stream_heads": heads,
            },
        )
    )
    body: dict[str, Any] = {
        "collection_id": str(collection_id),
        "version_number": int(parent.version_number) + 1,
        "parent_review_version_id": str(parent.id),
        "accepted_execution_id": str(execution_id),
        "delta_hash": delta.delta_hash,
        "protocol_version_id": protocol,
        "strategy_version": None if schedule is None else schedule.strategy_version,
        "input_versions": input_versions,
        "report_ids": [str(r) for r in reports],
        "carried": carried,
        "required_work": {s: [str(r) for r in required[s]] for s in rules.STAGES},
        "needs_attention": attention,
        "missing_history": rules.missing_history(
            candidates,
            kept,
            unattributed=await _unattributed(db, collection_id, kept),
        ),
        "prisma_body_hash": rules.version_hash(flow),
        "rationale": data.rationale,
    }
    version_id = uuid4()
    await _insert(
        db,
        _row(
            version_id=version_id,
            collection_id=collection_id,
            actor_id=actor_id,
            body=body,
        ),
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _ensure(db, collection_id, actor_id, version_id), False


async def _ensure(
    db: AsyncSession, collection_id: UUID, actor_id: UUID, version_id: UUID
) -> ReviewVersionResponse:
    """Create and assign every missing targeted queue, each stage in its own
    commit under a re-resolved SUPERVISE context; a refused stage is left
    ``queue_missing`` for a retry."""
    for stage in rules.STAGES:
        context = await resolve_project(
            db, collection_id, actor_id, ResearchAction.SUPERVISE
        )
        await lock_aggregate_stream(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
        )
        state = await _state(db, collection_id)
        version = state.get(version_id)
        status = next(s for s in state.work(version) if s.stage == stage)
        if status.status not in ("queue_missing", "queued"):
            await db.rollback()
            continue
        key = _work_key(version_id, stage)
        try:
            queue_id = status.queue_id
            if queue_id is None:
                queue = await screening_service.create_queue(
                    db,
                    context,
                    actor_id,
                    ScreeningQueueCreate(
                        protocol_version_id=version.protocol_version_id,
                        stage=cast(Any, stage),
                        report_ids=status.required_report_ids,
                        idempotency_key=key,
                    ),
                )
                queue_id = queue.id
            for reviewer in version.input_versions["reviewer_user_ids"]:
                if UUID(reviewer) in status.assigned_reviewer_ids:
                    continue
                await screening_service.assign(
                    db,
                    context,
                    queue_id,
                    actor_id,
                    ScreeningAssignmentCreate(
                        reviewer_user_id=UUID(reviewer),
                        idempotency_key=f"{key}:assign:{reviewer}",
                    ),
                )
            await db.commit()
        except HTTPException as error:
            await db.rollback()
            logger.warning(
                "review work for %s %s not created: %s",
                version_id,
                stage,
                error.detail,
            )
    return await _version_response(db, collection_id, version_id)


async def ensure_work(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, version_id: UUID
) -> ReviewVersionResponse:
    """SUPERVISE: retry the targeted queues with the same keys (never a
    second queue)."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    collection_id = _cid(context)
    state = await _state(db, collection_id)
    state.get(version_id)
    await db.rollback()
    return await _ensure(db, collection_id, actor_id, version_id)


async def link_release(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    version_id: UUID,
    data: ReviewReleaseLinkCreate,
) -> tuple[ReviewReleaseLinkResponse, bool]:
    """SUPERVISE: link one verified GOO-315 release, superseding exactly the
    release linked to the parent version. No release row is touched."""
    _require_role(context, ResearchProjectRole.SUPERVISOR)
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(
        db, collection_id, f"link:{version_id}", data, actor_id
    )
    state = await _state(db, collection_id)
    if replay is not None:
        return (
            cast(
                ReviewReleaseLinkResponse,
                _link_response(state.links[str(replay["review_version_id"])]),
            ),
            True,
        )
    version = state.get(version_id)
    if str(version.id) in state.links:
        raise HTTPException(status_code=409, detail=RELEASE_LINKED)
    release = (
        await db.execute(
            select(ManuscriptRelease).where(
                ManuscriptRelease.id == data.release_id,
                ManuscriptRelease.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if release is None:
        raise HTTPException(status_code=404, detail=RELEASE_NOT_FOUND)
    if release.stage != "verified":
        raise HTTPException(status_code=409, detail=RELEASE_NOT_VERIFIED)
    parent_id = version.parent_review_version_id
    if parent_id is not None and release.created_at < version.created_at:
        raise HTTPException(status_code=409, detail=RELEASE_TOO_OLD)
    parent_link = None if parent_id is None else state.links.get(str(parent_id))
    expected = None if parent_link is None else str(parent_link[0].release_id)
    given = (
        None if data.supersedes_release_id is None else str(data.supersedes_release_id)
    )
    if given != expected:
        raise HTTPException(status_code=409, detail=WRONG_SUPERSEDED)
    link = ResearchReviewReleaseLink(
        id=uuid4(),
        collection_id=collection_id,
        review_version_id=version.id,
        release_id=release.id,
        supersedes_release_id=data.supersedes_release_id,
        linked_by_id=actor_id,
    )
    db.add(link)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail=WRONG_SUPERSEDED) from error
    await _append(
        db,
        collection_id,
        event_type="review_update.release_linked",
        version_id=cast(UUID, version.id),
        actor_id=actor_id,
        reason=None,
        payload={
            "review_version_id": str(version.id),
            "release_id": str(release.id),
            "supersedes_release_id": given,
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return (
        cast(
            ReviewReleaseLinkResponse,
            _link_response((await _links(db, collection_id))[str(version.id)]),
        ),
        False,
    )


# --- Exports ------------------------------------------------------------------


async def _resolution_rows(
    db: AsyncSession, state: _State, resolution_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Resolution rows with their actors: the event's actor and the
    reviewers of the input observations (attribution by reference)."""
    ids = sorted(set(resolution_ids) & set(state.decisions.rows))
    rows = [state.decisions.rows[i][0] for i in ids]
    events = {
        str(e.id): e
        for e in await _all(
            db,
            select(ResearchDecisionEvent).where(
                ResearchDecisionEvent.id.in_([r.event_id for r in rows])
            ),
        )
    }
    observation_ids = [UUID(str(o)) for r in rows for o in r.input_observation_ids]
    reviewers = {
        str(o.id): str(o.reviewer_id)
        for o in await _all(
            db,
            select(ScreeningObservation).where(
                ScreeningObservation.id.in_(observation_ids)
            ),
        )
    }
    out = {}
    for row in rows:
        event = events.get(str(row.event_id))
        out[str(row.id)] = {
            "resolution_id": str(row.id),
            "queue_id": str(row.queue_id),
            "report_id": str(row.report_id),
            "stage": state.decisions.rows[str(row.id)][1],
            "basis": row.basis,
            "outcome": row.outcome,
            "exclusion_reason": row.exclusion_reason,
            "criteria_hash": row.criteria_hash,
            "supersedes_resolution_id": (
                None
                if row.supersedes_resolution_id is None
                else str(row.supersedes_resolution_id)
            ),
            "event_id": str(row.event_id),
            "event_actor_id": None if event is None else str(event.actor_user_id),
            "event_actor_role": None if event is None else event.actor_role,
            "event_reason": None if event is None else event.reason,
            "reviewer_ids": sorted(
                {
                    reviewers[str(o)]
                    for o in row.input_observation_ids
                    if str(o) in reviewers
                }
            ),
        }
    return out


async def _corpus(db: AsyncSession, report_ids: Sequence[str]) -> list[dict[str, Any]]:
    ids = [UUID(r) for r in report_ids]
    reports = {
        str(r.id): r
        for r in await _all(
            db, select(ResearchReport).where(ResearchReport.id.in_(ids))
        )
    }
    identifiers: dict[str, list[dict[str, str]]] = {r: [] for r in report_ids}
    for row in await _all(
        db,
        select(ResearchReportIdentifier)
        .where(ResearchReportIdentifier.report_id.in_(ids))
        .order_by(ResearchReportIdentifier.kind, ResearchReportIdentifier.value),
    ):
        identifiers[str(row.report_id)].append({"kind": row.kind, "value": row.value})
    return [
        {
            "report_id": r,
            "title": None if r not in reports else reports[r].title_snapshot,
            "study_id": (
                None
                if r not in reports or reports[r].study_id is None
                else str(reports[r].study_id)
            ),
            "identifiers": identifiers[r],
        }
        for r in report_ids
    ]


async def export_version(
    db: AsyncSession, context: ProjectContext, version_id: UUID
) -> dict[str, Any]:
    """VIEW: the sealed ``nous.academic.review-version.v1`` package, built
    only from rows this version references (read-only)."""
    collection_id = _cid(context)
    state = await _state(db, collection_id)
    version = state.get(version_id)
    work = state.work(version)
    queue_ids = {str(s.queue_id) for s in work if s.queue_id is not None}
    work_tips = [
        rid
        for rid in state.decisions.tips
        if str(state.decisions.rows[rid][0].queue_id) in queue_ids
    ]
    carried_ids = [str(c["resolution_id"]) for c in version.carried]
    parent_tips = list(version.input_versions.get("parent_decision_tips") or [])
    rows = await _resolution_rows(db, state, [*carried_ids, *work_tips, *parent_tips])
    reviewed = {
        (str(s.stage), str(report)) for s in work for report in s.required_report_ids
    }
    predecessors = [
        rows[rid]
        for rid in parent_tips
        if rid in rows and (rows[rid]["stage"], rows[rid]["report_id"]) in reviewed
    ]
    accounting_body = await _accounting(db, collection_id, state, version)
    link = _link_response(state.links.get(str(version.id)))
    body = {
        "project_id": str(collection_id),
        "version": {
            "id": str(version.id),
            "version_number": version.version_number,
            "parent_review_version_id": (
                None
                if version.parent_review_version_id is None
                else str(version.parent_review_version_id)
            ),
            "accepted_execution_id": (
                None
                if version.accepted_execution_id is None
                else str(version.accepted_execution_id)
            ),
            "delta_hash": version.delta_hash,
            "protocol_version_id": str(version.protocol_version_id),
            "strategy_version": version.strategy_version,
            "input_versions": version.input_versions,
            "content_hash": version.content_hash,
            "prisma_body_hash": version.prisma_body_hash,
            "rationale": version.rationale,
            "created_by_id": str(version.created_by_id),
            "created_at": version.created_at.isoformat(),
        },
        "corpus": await _corpus(db, [str(r) for r in version.report_ids]),
        "carried": [
            {**c, "resolution": rows.get(str(c["resolution_id"]))}
            for c in version.carried
        ],
        "work": [
            {
                **s.model_dump(mode="json"),
                "resolutions": [
                    rows[rid]
                    for rid in sorted(work_tips)
                    if rid in rows and str(rows[rid]["queue_id"]) == str(s.queue_id)
                ],
            }
            for s in work
        ],
        "predecessors": predecessors,
        "needs_attention": version.needs_attention,
        "missing_history": version.missing_history,
        "accounting": accounting_body.model_dump(mode="json"),
        "release": None if link is None else link.model_dump(mode="json"),
        "statement": (
            "Carried decisions are references to the original resolutions; "
            "re-reviewed reports keep their parent decision as predecessor. "
            "Release bytes are GOO-315's package by release id and hash."
        ),
    }
    return {**corpus_export.seal(body, _now().isoformat()), "schema": EXPORT_SCHEMA}


async def export_part(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``review-versions.json`` body."""
    collection_id = _cid(context)
    state = await _state(db, collection_id)
    return {
        "project_id": str(collection_id),
        "versions": [
            _response(state, v).model_dump(mode="json") for v in state.versions
        ],
    }
