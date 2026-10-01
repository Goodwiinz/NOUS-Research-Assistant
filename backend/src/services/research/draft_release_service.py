"""Verified draft promotion and selective invalidation (GOO-307).

A draft version is ``candidate`` until an adjudicator or supervisor promotes
that exact content; promotion is the only path to ``verified``. Lock order
for ``promote``: the route's ``resolve_project(RELEASE)`` (Workspace SHARE ->
Collection UPDATE, roles reloaded after the lock), then this Collection's
``research_release`` stream. ``invalidate_dependents`` runs inside a writer's
transaction (which already holds the Collection lock and its own stream), so
``research_release`` is always the last lock taken. Status is derived from
``draft_releases`` rows plus the evidence graph; nothing is stored on drafts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document
from src.models.draft_release import DraftRelease
from src.models.draft_task_result import DraftTaskResult
from src.models.extraction_matrix import (
    ExtractionAcceptedValue,
    ExtractionFormVersion,
    ExtractionMatrix,
)
from src.models.generated_draft import GeneratedDraft
from src.models.research_claim import (
    ResearchClaimAssessment,
    ResearchClaimEvidenceLink,
    ResearchClaimStanceObservation,
    ResearchClaimVersion,
)
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_project_role import ResearchProjectRole
from src.models.user import User
from src.services.research import claim_rules, extraction_rules
from src.services.research import release_rules as rules
from src.services.research.extraction_forms_service import _changed, document_pins
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import (
    _replayed_event,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.research_schemas import (
    DraftPromoteRequest,
    DraftReleaseResponse,
    ReleaseBlocker,
    ReleaseCheckResponse,
    ReleaseInvalidation,
)

AGGREGATE_TYPE = "research_release"
SUBJECT_TYPE = "draft_release"
DRAFT_NOT_FOUND = "Draft not found"
CONTENT_CHANGED = "Draft content changed; reload"
TASK_MISMATCH = "Draft does not match its task artifact"

Edge = tuple[rules.Node, rules.Node]


class ReleaseBlocked(Exception):
    """The gate refused; the route answers 409 with every blocker."""

    def __init__(self, blockers: Sequence[rules.Blocker]) -> None:
        super().__init__("release_blocked")
        self.blockers = list(blockers)


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def node_label(value: rules.Node) -> str:
    return f"{value[0]}:{value[1]}"


def blocker_models(blockers: Iterable[rules.Blocker]) -> list[ReleaseBlocker]:
    return [
        ReleaseBlocker(
            code=b.code,
            claim_version_id=b.claim_version_id,
            start=b.start,
            end=b.end,
            text=b.text,
            detail=b.detail,
        )
        for b in blockers
    ]


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


# --- The evidence graph ---


@dataclass(frozen=True)
class Graph:
    edges: list[Edge]
    changed: set[rules.Node]  # derived on read: inputs that moved on

    def stale_nodes(self) -> set[rules.Node]:
        return self.changed | rules.dependents(self.edges, self.changed)


async def load_edges(db: AsyncSession, collection_id: UUID) -> list[Edge]:
    """source -> accepted value -> extraction link; source -> span link;
    link -> assessment (``link_ids``); assessment and claim version ->
    release (the snapshotted ids). One query per table, all project-scoped.
    ``# ponytail: accepted values pin their own source revision, so the
    observation hop adds no reachability; walk it if observations ever
    carry their own dependents.``"""
    return (await _graph(db, collection_id)).edges


async def _graph(db: AsyncSession, collection_id: UUID) -> Graph:
    accepted, versions = await _project_extraction(db, collection_id)
    links = await _all(
        db,
        select(ResearchClaimEvidenceLink).where(
            ResearchClaimEvidenceLink.collection_id == collection_id
        ),
    )
    assessments = await _all(
        db,
        select(ResearchClaimAssessment).where(
            ResearchClaimAssessment.collection_id == collection_id
        ),
    )
    releases = await _all(
        db, select(DraftRelease).where(DraftRelease.collection_id == collection_id)
    )
    superseded_versions = set(
        await _all(
            db,
            select(ResearchClaimVersion.supersedes_claim_version_id).where(
                ResearchClaimVersion.collection_id == collection_id,
                ResearchClaimVersion.supersedes_claim_version_id.is_not(None),
            ),
        )
    )
    edges: list[Edge] = []
    for row in accepted:
        edges.append(
            (
                rules.source_node(row.document_id, row.source_hash, row.text_sha256),
                rules.node("accepted", row.id),
            )
        )
    for row in links:
        if row.kind == "extraction":
            parent = rules.node("accepted", row.accepted_value_id)
        elif row.kind == "source_span":
            parent = rules.source_node(
                row.document_id, row.source_hash, row.text_sha256
            )
        elif row.kind == "synthesis_result":  # GOO-311
            parent = rules.node("synthesis", row.synthesis_result_id)
        elif row.kind == "figure":  # GOO-312
            parent = rules.node("figure", row.figure_id)
        else:
            continue
        edges.append((parent, rules.node("link", row.id)))
    for row in assessments:
        for link_id in row.link_ids or []:
            edges.append(
                (rules.node("link", link_id), rules.node("assessment", row.id))
            )
    for row in releases:
        target = rules.node("release", row.id)
        for value in row.assessment_ids or []:
            edges.append((rules.node("assessment", value), target))
        for value in [
            *(row.claim_version_ids or []),
            *(row.interpretation_claim_version_ids or []),
        ]:
            edges.append((rules.node("claim_version", value), target))
    # GOO-309: appraisals hang off accepted values, sources and protocols.
    # GOO-310: evidence tables, contradictions and certainty hang off accepted
    # values, protocols and appraisals. GOO-311: synthesis results hang off
    # evidence tables and protocols. GOO-312: figures hang off runs, which
    # hang off their input revisions. Local imports: these services read this
    # graph for their stale flags. GOO-315: verified manuscript releases hang
    # off their draft release. GOO-320: the tip review version's changed or
    # corrected reports hang their documents off the report.
    from src.services.research import manuscript_release_service
    from src.services.research_engine import (
        appraisal_service,
        evidence_service,
        experiment_service,
        review_update_service,
        synthesis_service,
    )

    part_changed: set[rules.Node] = set()
    for part in (
        appraisal_service.graph_part,
        evidence_service.graph_part,
        synthesis_service.graph_part,
        experiment_service.graph_part,
        manuscript_release_service.graph_part,
        review_update_service.graph_part,
    ):
        more_edges, more_changed = await part(db, collection_id)
        edges += more_edges
        part_changed |= more_changed
    # GOO-320: a document reaches every source revision pinned from it.
    if any(parent[0] == "document" for parent, _child in edges):
        edges += [
            (rules.node("document", rules.parse_source(source[1])[0]), source)
            for source in sorted({p for p, _c in edges if p[0] == "source"})
        ]
    changed = await _changed_sources(db, edges)
    changed |= part_changed
    changed |= {
        rules.node("accepted", row.id)
        for row in accepted
        if _accepted_moved(row, accepted, versions)
    }
    changed |= {
        rules.node("link", row.supersedes_link_id)
        for row in links
        if row.supersedes_link_id
    }
    changed |= {
        rules.node("assessment", row.supersedes_assessment_id)
        for row in assessments
        if row.supersedes_assessment_id
    }
    changed |= {rules.node("claim_version", value) for value in superseded_versions}
    return Graph(edges, changed)


async def _project_extraction(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Any], dict[UUID, Any]]:
    """The project's accepted values and every form version by id."""
    versions = await _all(
        db,
        select(ExtractionFormVersion)
        .join(ExtractionMatrix, ExtractionMatrix.id == ExtractionFormVersion.matrix_id)
        .where(ExtractionMatrix.project_id == collection_id),
    )
    by_id = {cast(UUID, v.id): v for v in versions}
    accepted = await _all(
        db,
        select(ExtractionAcceptedValue).where(
            ExtractionAcceptedValue.form_version_id.in_(list(by_id))
        ),
    )
    return accepted, by_id


def _accepted_moved(row: Any, accepted: list[Any], versions: dict[UUID, Any]) -> bool:
    """Superseded, or its field definition changed (GOO-304's form rule)."""
    if any(other.supersedes_accepted_value_id == row.id for other in accepted):
        return True
    version = versions[row.form_version_id]
    current = max(
        (v for v in versions.values() if v.matrix_id == version.matrix_id),
        key=lambda v: v.version_no,
    )
    return extraction_rules.field_def(
        version.fields, row.field_id
    ) != extraction_rules.field_def(current.fields, row.field_id)


async def _changed_sources(
    db: AsyncSession, edges: Iterable[Edge], document_id: UUID | None = None
) -> set[rules.Node]:
    """Source revisions in the graph that no longer match the document
    (GOO-305's derive-on-read rule), optionally for one document."""
    sources = {
        parent
        for parent, _child in edges
        if parent[0] == "source"
        and (
            document_id is None or rules.parse_source(parent[1])[0] == str(document_id)
        )
    }
    ids = {UUID(rules.parse_source(value)[0]) for _kind, value in sources}
    documents = {
        str(d.id): d
        for d in await _all(db, select(Document).where(Document.id.in_(ids)))
    }
    changed = set()
    for source in sources:
        document_id_, source_hash, text_hash = rules.parse_source(source[1])
        document = documents.get(document_id_)
        if document is None or _changed(
            document_pins(document), source_hash, text_hash
        ):
            changed.add(source)
    return changed


# --- Gate inputs ---


async def _draft(
    db: AsyncSession, collection_id: UUID, draft_id: UUID, version: int
) -> Any:
    draft = (
        await db.execute(
            select(GeneratedDraft).where(
                GeneratedDraft.id == draft_id,
                GeneratedDraft.project_id == collection_id,
                GeneratedDraft.version == version,
            )
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail=DRAFT_NOT_FOUND)
    return draft


async def gate_inputs(
    db: AsyncSession, collection_id: UUID, draft: Any, graph: Graph
) -> list[rules.ClaimIn]:
    """Each claim's latest version bound to this exact (draft, content hash),
    with its links and assessment tip."""
    content_hash = claim_rules.content_hash(draft.content)
    bound = await _all(
        db,
        select(ResearchClaimVersion)
        .where(
            ResearchClaimVersion.collection_id == collection_id,
            ResearchClaimVersion.draft_id == draft.id,
            ResearchClaimVersion.draft_content_hash == content_hash,
        )
        .order_by(ResearchClaimVersion.version_no),
    )
    latest = {v.claim_id: v for v in bound}  # version_no order: the last wins
    version_ids = [v.id for v in latest.values()]
    superseded = {
        n[1] for n in graph.changed if n[0] == "claim_version"
    }  # superseded versions
    links = await _all(
        db,
        select(ResearchClaimEvidenceLink).where(
            ResearchClaimEvidenceLink.claim_version_id.in_(version_ids)
        ),
    )
    dead = {row.supersedes_link_id for row in links if row.supersedes_link_id}
    observed = set(
        await _all(
            db,
            select(ResearchClaimStanceObservation.link_id).where(
                ResearchClaimStanceObservation.link_id.in_([r.id for r in links])
            ),
        )
    )
    assessments = await _all(
        db,
        select(ResearchClaimAssessment).where(
            ResearchClaimAssessment.claim_version_id.in_(version_ids)
        ),
    )
    closed = {a.supersedes_assessment_id for a in assessments}
    tips = {a.claim_version_id: a for a in assessments if a.id not in closed}
    authors = {
        u.id: " ".join(p for p in (u.first_name, u.last_name) if p) or u.email
        for u in await _all(
            db,
            select(User).where(
                User.id.in_(
                    [v.attributed_to_user_id for v in latest.values()]
                    + [uuid4()]  # never an empty IN
                )
            ),
        )
    }
    claims = []
    for version in sorted(latest.values(), key=lambda v: (v.start_char, str(v.id))):
        tip = tips.get(version.id)
        claims.append(
            rules.ClaimIn(
                claim_version_id=version.id,
                kind=version.kind,
                start=version.start_char,
                end=version.end_char,
                text=version.text,
                attributed_to=authors.get(version.attributed_to_user_id),
                links=tuple(
                    rules.LinkIn(
                        id=row.id,
                        kind=row.kind,
                        live=row.status == "linked" and row.id not in dead,
                        observed=row.id in observed,
                    )
                    for row in links
                    if row.claim_version_id == version.id
                ),
                assessment=(
                    None
                    if tip is None
                    else rules.AssessmentIn(
                        id=tip.id,
                        stance=tip.stance,
                        link_ids=tuple(UUID(str(i)) for i in tip.link_ids or []),
                    )
                ),
                is_tip=str(version.id) not in superseded,
            )
        )
    return claims


async def _gate(
    db: AsyncSession, collection_id: UUID, draft: Any, graph: Graph
) -> rules.GateResult:
    claims = await gate_inputs(db, collection_id, draft, graph)
    review = (draft.generation_params or {}).get("citation_review") or {}
    return rules.check_release(draft.content, claims, review, graph.stale_nodes())


# --- Reads ---


async def _releases(db: AsyncSession, draft_ids: Sequence[UUID]) -> list[Any]:
    return await _all(
        db,
        select(DraftRelease)
        .where(DraftRelease.draft_id.in_(list(draft_ids)))
        .order_by(DraftRelease.created_at, DraftRelease.id),
    )


def _status(rows: Sequence[Any], graph: Graph | None) -> rules.Status:
    live = [r for r in rows if r.stale_at is None]
    derived = (
        graph is not None
        and bool(live)
        and rules.node("release", live[0].id) in graph.stale_nodes()
    )
    return rules.release_status(rows, derived)


async def statuses(
    db: AsyncSession, collection_id: UUID, draft_ids: Sequence[UUID]
) -> dict[UUID, str]:
    """Release status per draft; the graph is loaded once, only if needed."""
    rows = await _releases(db, draft_ids)
    graph = (
        await _graph(db, collection_id)
        if any(r.stale_at is None for r in rows)
        else None
    )
    return {
        draft_id: _status([r for r in rows if r.draft_id == draft_id], graph)
        for draft_id in draft_ids
    }


async def _invalidation(
    db: AsyncSession, latest: Any, graph: Graph
) -> ReleaseInvalidation:
    if latest.stale_at is not None:
        event = await db.get(ResearchDecisionEvent, latest.stale_event_id)
        payload = cast(dict[str, Any], event.payload) if event else {}
        return ReleaseInvalidation(
            stale_at=latest.stale_at,
            cause=payload.get("cause") or {},
            changed_nodes=payload.get("changed_nodes") or [],
            assessment_ids=payload.get("assessment_ids") or [],
        )
    target = rules.node("release", latest.id)
    reaching = sorted(
        node_label(n)
        for n in graph.changed
        if n == target or target in rules.dependents(graph.edges, {n})
    )
    return ReleaseInvalidation(
        cause={"family": "derived", "event_id": None, "kind": "derived_on_read"},
        changed_nodes=reaching,
        assessment_ids=[
            UUID(n[1])
            for n in rules.dependents(graph.edges, graph.changed)
            if n[0] == "assessment" and target in rules.dependents(graph.edges, {n})
        ],
    )


async def check(
    db: AsyncSession, context: ProjectContext, draft_id: UUID, version: int
) -> ReleaseCheckResponse:
    """VIEW: status, blockers, the latest release and why it is stale."""
    collection_id = _cid(context)
    draft = await _draft(db, collection_id, draft_id, version)
    graph = await _graph(db, collection_id)
    gate = await _gate(db, collection_id, draft, graph)
    rows = await _releases(db, [draft.id])
    status = _status(rows, graph)
    latest = rows[-1] if rows else None
    live = next((r for r in rows if r.stale_at is None), latest)
    return ReleaseCheckResponse(
        draft_id=draft.id,
        draft_version=draft.version,
        release_status=status,
        content_hash=claim_rules.content_hash(draft.content),
        blockers=blocker_models(gate.blockers),
        dimensions=gate.dimensions,
        release=None if live is None else DraftReleaseResponse.model_validate(live),
        invalidation=(
            await _invalidation(db, live, graph)
            if status == "stale" and live is not None
            else None
        ),
    )


async def export_header(
    db: AsyncSession, collection_id: UUID, draft: Any
) -> tuple[rules.GateResult, str]:
    """The gate result and status header an export is labelled with."""
    graph = await _graph(db, collection_id)
    gate = await _gate(db, collection_id, draft, graph)
    rows = await _releases(db, [draft.id])
    status = _status(rows, graph)
    live = next((r for r in rows if r.stale_at is None), rows[-1] if rows else None)
    cause = None
    if status == "stale" and live is not None:
        cause = str((await _invalidation(db, live, graph)).cause.get("family"))
    return gate, rules.status_header(
        status,
        gate,
        release_id=None if live is None else str(live.id),
        content_hash=None if live is None else live.content_hash,
        cause=cause,
    )


# --- Promotion (RELEASE) ---


async def _live(db: AsyncSession, draft_id: UUID) -> Any:
    return (
        await db.execute(
            select(DraftRelease).where(
                DraftRelease.draft_id == draft_id, DraftRelease.stale_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def promote(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    draft_id: UUID,
    version: int,
    body: DraftPromoteRequest,
) -> tuple[DraftReleaseResponse, bool]:
    """Promote one exact draft version to verified; commits once.

    Returns (release, replayed). ``ReleaseBlocked`` lists every blocker."""
    collection_id = _cid(context)
    draft = await _draft(db, collection_id, draft_id, version)
    stream = await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    key = f"promote:{body.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": "promote",
            "actor_user_id": str(actor_id),
            "draft_id": str(draft_id),
            "version": version,
            "request": body.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        release_id = cast(dict[str, Any], replay.payload)["release_id"]
        row = await db.get(DraftRelease, UUID(release_id))
        return DraftReleaseResponse.model_validate(row), True
    content_hash = claim_rules.content_hash(draft.content)
    if body.content_hash != content_hash:
        raise HTTPException(status_code=409, detail=CONTENT_CHANGED)
    tasks = await _all(
        db, select(DraftTaskResult).where(DraftTaskResult.artifact_id == draft.id)
    )
    if any(
        (t.artifact_version, t.artifact_hash) != (draft.version, content_hash)
        for t in tasks
    ):
        raise HTTPException(status_code=409, detail=TASK_MISMATCH)
    graph = await _graph(db, collection_id)
    live = await _live(db, draft.id)
    if (
        live is not None
        and live.content_hash == content_hash
        and rules.node("release", live.id) not in graph.stale_nodes()
    ):
        return DraftReleaseResponse.model_validate(live), True
    gate = await _gate(db, collection_id, draft, graph)
    if gate.blockers:
        raise ReleaseBlocked(gate.blockers)
    protocol = await current_protocol_version_id(db, collection_id)
    row = DraftRelease(
        id=uuid4(),
        collection_id=collection_id,
        draft_id=draft.id,
        draft_version=draft.version,
        content_hash=content_hash,
        claim_version_ids=[str(i) for i in gate.claim_version_ids],
        assessment_ids=[str(i) for i in gate.assessment_ids],
        interpretation_claim_version_ids=[str(i) for i in gate.interpretation_ids],
        protocol_version_id=UUID(protocol) if protocol else None,
        policy_version=rules.POLICY_VERSION,
        promoted_by_id=actor_id,
        actor_role=(
            "adjudicator"
            if ResearchProjectRole.ADJUDICATOR in context.effective_roles
            else "supervisor"
        ),
        rationale=body.rationale,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        # A promotion that raced past the live check: return the winner.
        await db.rollback()
        if not _is_unique_violation(error):
            raise
        winner = await _live(db, draft_id)  # rollback expired `draft`
        if winner is None:
            raise
        return DraftReleaseResponse.model_validate(winner), True
    payload = {
        "collection_id": str(collection_id),
        "release_id": str(row.id),
        "draft_id": str(draft.id),
        "draft_version": draft.version,
        "content_hash": content_hash,
        "claim_version_ids": row.claim_version_ids,
        "assessment_ids": row.assessment_ids,
        "interpretation_claim_version_ids": row.interpretation_claim_version_ids,
        "protocol_version_id": protocol,
        "policy_version": rules.POLICY_VERSION,
        "dimensions": gate.dimensions,
    }
    try:
        await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type="release.promoted",
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role=cast(str, row.actor_role),
            subject_type=SUBJECT_TYPE,
            subject_id=cast(UUID, row.id),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=body.rationale,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc
    await db.commit()
    await db.refresh(row)
    return DraftReleaseResponse.model_validate(row), False


# --- Invalidation (called by the upstream writers) ---


async def source_nodes(
    db: AsyncSession, collection_id: UUID, document_id: UUID
) -> set[rules.Node]:
    """This document's graph source revisions that its text no longer matches."""
    edges = (await _graph(db, collection_id)).edges
    return await _changed_sources(db, edges, document_id)


async def invalidate_dependents(
    db: AsyncSession,
    *,
    collection_id: UUID,
    changed: Iterable[rules.Node],
    actor_id: UUID,
    actor_role: str,
    cause: dict[str, Any],
) -> list[UUID]:
    """Stamp every live release reachable from ``changed`` stale and append
    one ``release.staled``. Runs in the caller's transaction; never commits.
    The ``stale_at IS NULL`` guard makes a release stale exactly once."""
    changed = set(changed)
    if not changed:
        return []
    graph = await _graph(db, collection_id)
    reached = rules.dependents(graph.edges, changed)
    candidates = [UUID(value) for kind, value in reached if kind == "release"]
    if not candidates:
        return []
    event_id = uuid4()
    stamped = list(
        (
            await db.execute(
                update(DraftRelease)
                .where(
                    DraftRelease.id.in_(candidates),
                    DraftRelease.collection_id == collection_id,
                    DraftRelease.stale_at.is_(None),
                )
                .values(stale_at=datetime.now(timezone.utc), stale_event_id=event_id)
                .returning(DraftRelease.id)
                .execution_options(synchronize_session=False)
            )
        )
        .scalars()
        .all()
    )
    if not stamped:
        return []
    payload = {
        "collection_id": str(collection_id),
        "release_ids": sorted(str(i) for i in stamped),
        "cause": cause,
        "changed_nodes": sorted(node_label(n) for n in changed),
        "assessment_ids": sorted(
            value for kind, value in reached if kind == "assessment"
        ),
    }
    await append_decision(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
        event_type="release.staled",
        event_schema_version=1,
        actor_user_id=actor_id,
        actor_role=actor_role,
        subject_type=SUBJECT_TYPE,
        subject_id=collection_id,
        subject_version_id=None,
        subject_hash=decision_request_fingerprint(payload),
        reason=None,
        payload=payload,
        idempotency_key=f"staled:{event_id}",
        request_fingerprint=decision_request_fingerprint(payload),
        event_id=event_id,
    )
    return cast(list[UUID], stamped)
