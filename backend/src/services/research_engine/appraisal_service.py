"""Blind, protocol-bound study-design appraisal (GOO-309).

Lock order for writers: the route's ``resolve_project(REVIEW | ADJUDICATE)``
(Workspace SHARE -> Collection UPDATE, roles reloaded after the lock), then
this Collection's ``research_appraisal`` stream. ``submit`` and
``adjudicate`` each commit exactly once. Rows are insert-only (a database
trigger refuses UPDATE and DELETE); edits and adjudications are successors.

Status (``appraisal_rules.status``), reveal and staleness are derived on
every read. Another reviewer's row is visible only through
``visible_appraisal_ids``, GOO-302's predicate over the revealed set, and
every read path (list, export, the audit bundle) filters through it.
Nothing here reads model confidence, citation counts or legacy quality marks.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import AbstractSet, Any, Iterable, Mapping, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.extraction_matrix import (
    ExtractionAcceptedValue,
    ExtractionFormVersion,
    ExtractionMatrix,
    ExtractionObservation,
)
from src.models.research_appraisal import AppraisalAssessment
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_report import ResearchReport, ResearchStudy
from src.models.user import User
from src.schemas.research_engine import (
    AppraisalAdjudicate,
    AppraisalEvidenceOption,
    AppraisalInstrument,
    AppraisalInstrumentDomain,
    AppraisalListResponse,
    AppraisalResponse,
    AppraisalResult,
    AppraisalSubmit,
)
from src.services.research import release_rules
from src.services.research.extraction_forms_service import _is_tip
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import appraisal_rules as rules
from src.services.research_engine import protocol_methods
from src.services.research_engine.acquisition_service import document_reports
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import (
    _replayed_event,
    analysis_unit,
    current_protocol_version_id,
    live_reports,
)
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.screening_service import _is_unique_violation

AGGREGATE_TYPE = "research_appraisal"
SUBJECT_TYPE = "appraisal_assessment"
EXPORT_SCHEMA = "nous.academic.appraisal.v1"

NO_PROTOCOL = "Project has no approved protocol"
PROTOCOL_STALE = "Protocol version is stale; reload"
NOT_PROTOCOL_INSTRUMENT = "Instrument is not the protocol's"
UNDECLARED = "Outcome or timepoint is not declared by the protocol"
STUDY_NOT_FOUND = "Study not found"
LINK_UNRESOLVED = "Study link unresolved"
APPRAISE_STUDY = "Report belongs to a study; appraise the study"
EVIDENCE_INVALID = (
    "Evidence must be an accepted-value tip or an anchored observation"
    " of this project"
)
EVIDENCE_FOREIGN = "Evidence is not from this study"
APPRAISAL_STALE = "Appraisal is stale; reload"
REVEALED = "Revealed; changes go through adjudication"
NOT_IN_CONFLICT = "Result is not in conflict"
SELF_ADJUDICATION = "Adjudicator assessed this result"
_DEFAULT_MODE = "dual_independent"  # a protocol without a mode reveals late
_ANCHORED = ("verified", "ambiguous")

Edge = tuple[release_rules.Node, release_rules.Node]


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _key(row: Any) -> rules.Key:
    return (str(row.target_key), str(row.outcome_key), str(row.timepoint))


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


# --- Protocol, unit and evidence ------------------------------------------------


@dataclass(frozen=True)
class _Method:
    version_id: str | None
    instrument: tuple[str, str, str] | None  # (key, version, mode)
    outcomes: dict[str, tuple[str, ...]]

    @property
    def mode(self) -> str:
        return self.instrument[2] if self.instrument else _DEFAULT_MODE


async def _method(db: AsyncSession, collection_id: UUID) -> _Method:
    """The current approved protocol's appraisal method; lenient for reads."""
    version_id = await current_protocol_version_id(db, collection_id)
    if version_id is None:
        return _Method(None, None, {})
    version = await db.get(ResearchProtocolVersion, UUID(version_id))
    snapshot = cast(Mapping[str, Any], version.snapshot if version else {})
    try:
        instrument: tuple[str, str, str] | None = protocol_methods.appraisal_method(
            snapshot
        )
    except ValueError:
        instrument = None
    try:
        outcomes = protocol_methods.declared_outcomes(snapshot)
    except ValueError:
        outcomes = {}
    return _Method(version_id, instrument, outcomes)


async def _require_method(
    db: AsyncSession, collection_id: UUID, data: AppraisalSubmit
) -> tuple[str, Mapping[str, Any]]:
    """(mode, instrument spec) for a write, or the 409/422 that refuses it."""
    version_id = await current_protocol_version_id(db, collection_id)
    if version_id is None:
        raise _conflict(NO_PROTOCOL)
    if str(data.protocol_version_id) != version_id:
        raise _conflict(PROTOCOL_STALE)
    version = await db.get(ResearchProtocolVersion, UUID(version_id))
    snapshot = cast(Mapping[str, Any], version.snapshot if version else {})
    try:
        key, instrument_version, mode = protocol_methods.appraisal_method(snapshot)
        outcomes = protocol_methods.declared_outcomes(snapshot)
    except ValueError as error:
        raise _conflict(str(error)) from error
    if (data.instrument_key, data.instrument_version) != (key, instrument_version):
        raise _unprocessable(NOT_PROTOCOL_INSTRUMENT)
    if data.timepoint not in outcomes.get(data.outcome_key, ()):
        raise _unprocessable(UNDECLARED)
    try:
        return mode, rules.spec(key, instrument_version)
    except ValueError as error:  # the protocol names an instrument not encoded
        raise _conflict(str(error)) from error


async def _target(
    db: AsyncSession, collection_id: UUID, data: AppraisalSubmit
) -> tuple[UUID | None, UUID | None, str]:
    """(study_id, report_id, target_key): a confirmed study appraises the
    study; a report with no study link is its own unit."""
    if data.study_id is not None:
        study = (
            await db.execute(
                select(ResearchStudy.id).where(
                    ResearchStudy.id == data.study_id,
                    ResearchStudy.collection_id == collection_id,
                )
            )
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(status_code=404, detail=STUDY_NOT_FOUND)
        return data.study_id, None, f"study:{data.study_id}"
    report_id = cast(UUID, data.report_id)
    report = (await live_reports(db, collection_id, [report_id]))[report_id]
    if report.study_link_status == "confirmed":
        raise _unprocessable(APPRAISE_STUDY)
    unit = analysis_unit(report)
    if unit is None:
        raise _conflict(LINK_UNRESOLVED)
    return None, report_id, unit


async def document_units(db: AsyncSession, collection_id: UUID) -> dict[UUID, str]:
    """document_id -> target_key; documents with an unresolved link are omitted."""
    by_document = await document_reports(db, collection_id)
    reports = {
        cast(UUID, r.id): r
        for r in await _all(
            db,
            select(ResearchReport).where(
                ResearchReport.collection_id == collection_id,
                ResearchReport.id.in_(set(by_document.values()) or [uuid4()]),
            ),
        )
    }
    units = {}
    for document, report in by_document.items():
        unit = analysis_unit(reports[report]) if report in reports else None
        if unit is not None:
            units[document] = unit
    return units


def _project_versions(collection_id: UUID) -> Any:
    return (
        select(ExtractionFormVersion.id)
        .join(ExtractionMatrix, ExtractionMatrix.id == ExtractionFormVersion.matrix_id)
        .where(ExtractionMatrix.project_id == collection_id)
    )


async def _evidence_pins(
    db: AsyncSession, collection_id: UUID, target_key: str, domains: Mapping[str, Any]
) -> list[list[str | None]]:
    """``[kind, id, document_id, source_hash, text_sha256]`` per cited item."""
    cited = {
        (item["kind"], item["id"])
        for domain in domains.values()
        for item in domain.get("evidence") or []
    }
    if not cited:
        return []
    wanted = {kind: [UUID(i) for k, i in cited if k == kind] for kind, _ in cited}
    rows: list[tuple[str, Any]] = []
    if wanted.get("accepted_value"):
        rows += [
            ("accepted_value", row)
            for row in await _all(
                db,
                select(ExtractionAcceptedValue).where(
                    ExtractionAcceptedValue.id.in_(wanted["accepted_value"]),
                    ExtractionAcceptedValue.form_version_id.in_(
                        _project_versions(collection_id)
                    ),
                    _is_tip(),
                ),
            )
        ]
    if wanted.get("observation"):
        rows += [
            ("observation", row)
            for row in await _all(
                db,
                select(ExtractionObservation).where(
                    ExtractionObservation.id.in_(wanted["observation"]),
                    ExtractionObservation.form_version_id.in_(
                        _project_versions(collection_id)
                    ),
                    ExtractionObservation.anchor_status.in_(_ANCHORED),
                ),
            )
        ]
    if len(rows) != len(cited):
        raise _unprocessable(EVIDENCE_INVALID)
    units = await document_units(db, collection_id)
    if any(units.get(row.document_id) != target_key for _, row in rows):
        raise _unprocessable(EVIDENCE_FOREIGN)
    return sorted(
        [
            kind,
            str(row.id),
            str(row.document_id),
            row.source_hash,
            row.text_sha256,
        ]
        for kind, row in rows
    )


# --- Derived state ---------------------------------------------------------------


def _as_rule_row(row: Any) -> rules.Row:
    return rules.Row(
        id=cast(UUID, row.id),
        assessor_id=cast(UUID, row.assessor_id),
        study_design=str(row.study_design),
        applicability=str(row.applicability),
        domains=cast(Mapping[str, Any], row.domains),
        overall=cast(str | None, row.overall),
        resolves=tuple(UUID(str(v)) for v in row.resolves_assessment_ids or ()),
    )


@dataclass
class _State:
    mode: str
    rows: list[Any]
    superseded: set[UUID]
    by_key: dict[rules.Key, list[Any]] = field(default_factory=dict)
    tips: dict[rules.Key, list[Any]] = field(default_factory=dict)
    adjudicated: dict[rules.Key, Any] = field(default_factory=dict)
    revealed: set[rules.Key] = field(default_factory=set)

    def status(self, key: rules.Key) -> rules.Status:
        adjudicated = self.adjudicated.get(key)
        return rules.status(
            self.mode,
            [_as_rule_row(r) for r in self.tips.get(key, [])],
            None if adjudicated is None else _as_rule_row(adjudicated),
        )

    def revealed_ids(self) -> set[UUID]:
        return {
            cast(UUID, r.id) for key in self.revealed for r in self.by_key.get(key, [])
        }


async def _state(db: AsyncSession, collection_id: UUID, mode: str) -> _State:
    rows = await _all(
        db,
        select(AppraisalAssessment)
        .where(AppraisalAssessment.collection_id == collection_id)
        .order_by(AppraisalAssessment.created_at, AppraisalAssessment.id),
    )
    superseded = {
        cast(UUID, r.supersedes_assessment_id)
        for r in rows
        if r.supersedes_assessment_id is not None
    }
    state = _State(mode, rows, superseded)
    for row in rows:
        key = _key(row)
        state.by_key.setdefault(key, []).append(row)
        if row.id in superseded:
            continue
        if row.kind == "independent":
            state.tips.setdefault(key, []).append(row)
        else:
            state.adjudicated[key] = row
    state.revealed = rules.revealed_keys(
        mode, {k: [_as_rule_row(r) for r in v] for k, v in state.tips.items()}
    )
    return state


async def visible_appraisal_ids(
    db: AsyncSession, collection_id: UUID, viewer_id: UUID | None
) -> set[UUID]:
    """THE reveal predicate: the viewer's own rows plus every row of a
    revealed result. A viewer-less reader (the audit bundle) gets only the
    revealed rows. Every read path filters through this set."""
    state = await _state(db, collection_id, (await _method(db, collection_id)).mode)
    return _visible(state, viewer_id)


def _visible(state: _State, viewer_id: UUID | None) -> set[UUID]:
    revealed = state.revealed_ids()
    return {
        cast(UUID, r.id)
        for r in state.rows
        if rules.visible(
            cast(UUID, r.assessor_id), cast(UUID, r.id), viewer_id, revealed
        )
    }


async def current_appraisals(
    db: AsyncSession, collection_id: UUID
) -> dict[rules.Key, rules.Status]:
    """Revealed keys only, for GOO-310's risk-of-bias domain (read-only)."""
    state = await _state(db, collection_id, (await _method(db, collection_id)).mode)
    return {key: state.status(key) for key in sorted(state.revealed)}


# --- Evidence graph (GOO-307's walk) ----------------------------------------------


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Edge], set[release_rules.Node]]:
    """Edges ``accepted -> appraisal``, ``source revision -> appraisal`` (for
    observation evidence) and ``protocol -> appraisal``; ``changed`` holds
    superseded appraisals and approved protocol versions no longer current.
    Called from ``draft_release_service._graph`` before its source check."""
    rows = await _all(
        db,
        select(AppraisalAssessment).where(
            AppraisalAssessment.collection_id == collection_id
        ),
    )
    if not rows:
        return [], set()
    cited: dict[UUID, list[tuple[str, str]]] = {
        cast(UUID, row.id): [
            (item["kind"], item["id"])
            for domain in (row.domains or {}).values()
            for item in domain.get("evidence") or []
        ]
        for row in rows
    }
    observation_ids = {
        UUID(i) for items in cited.values() for k, i in items if k == "observation"
    }
    observations = {
        str(o.id): o
        for o in await _all(
            db,
            select(ExtractionObservation).where(
                ExtractionObservation.id.in_(observation_ids or [uuid4()])
            ),
        )
    }
    edges: list[Edge] = []
    for row in rows:
        target = release_rules.node("appraisal", row.id)
        edges.append((release_rules.node("protocol", row.protocol_version_id), target))
        for kind, value in cited[cast(UUID, row.id)]:
            if kind == "accepted_value":
                edges.append((release_rules.node("accepted", value), target))
            elif value in observations:
                o = observations[value]
                edges.append(
                    (
                        release_rules.source_node(
                            o.document_id, o.source_hash, o.text_sha256
                        ),
                        target,
                    )
                )
    current = await current_protocol_version_id(db, collection_id)
    changed = {
        release_rules.node("appraisal", row.supersedes_assessment_id)
        for row in rows
        if row.supersedes_assessment_id is not None
    } | {
        release_rules.node("protocol", row.protocol_version_id)
        for row in rows
        if str(row.protocol_version_id) != current
    }
    return edges, changed


async def _stale_nodes(db: AsyncSession, collection_id: UUID) -> set[Any]:
    # Local import: draft_release_service imports this module lazily too.
    from src.services.research import draft_release_service

    return (await draft_release_service._graph(db, collection_id)).stale_nodes()


# --- Writes ----------------------------------------------------------------------


def _fingerprint(operation: str, actor_id: UUID, data: AppraisalSubmit) -> str:
    return decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor_id),
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )


async def _names(db: AsyncSession, user_ids: Iterable[Any]) -> dict[UUID, str]:
    users = await _all(db, select(User).where(User.id.in_(set(user_ids) or [uuid4()])))
    return {
        cast(UUID, u.id): " ".join(p for p in (u.first_name, u.last_name) if p)
        or str(u.email)
        for u in users
    }


def _response(
    row: Any,
    names: Mapping[UUID, str],
    superseded: AbstractSet[UUID] = frozenset(),
    stale: AbstractSet[Any] = frozenset(),
) -> AppraisalResponse:
    response = cast(AppraisalResponse, AppraisalResponse.model_validate(row))
    return cast(
        AppraisalResponse,
        response.model_copy(
            update={
                "assessor_name": names.get(cast(UUID, row.assessor_id)),
                "superseded": row.id in superseded,
                "stale": release_rules.node("appraisal", row.id) in stale,
            }
        ),
    )


async def _row_response(db: AsyncSession, row: Any) -> AppraisalResponse:
    return _response(row, await _names(db, [row.assessor_id]))


async def _write(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: AppraisalSubmit,
    *,
    adjudication: AppraisalAdjudicate | None,
) -> tuple[AppraisalResponse, bool]:
    collection_id = _cid(context)
    stream = await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    operation = "adjudicate" if adjudication else "submit"
    key = f"{operation}:{data.idempotency_key}"
    fingerprint = _fingerprint(operation, actor_id, data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        assessment_id = cast(dict[str, Any], replay.payload)["assessment_id"]
        row = await db.get(AppraisalAssessment, UUID(assessment_id))
        return await _row_response(db, row), True
    mode, spec = await _require_method(db, collection_id, data)
    study_id, report_id, target_key = await _target(db, collection_id, data)
    try:
        domains = rules.validate(
            spec,
            data.study_design,
            data.applicability,
            {k: v.model_dump(mode="json") for k, v in data.domains.items()},
            data.overall,
        )
    except ValueError as error:
        raise _unprocessable(str(error)) from error
    pins = await _evidence_pins(db, collection_id, target_key, domains)
    state = await _state(db, collection_id, mode)
    result = (target_key, data.outcome_key, data.timepoint)
    resolves: list[str] | None = None
    if adjudication is None:
        if mode == "dual_independent" and result in state.revealed:
            raise _conflict(REVEALED)
        mine = next(
            (r for r in state.tips.get(result, []) if r.assessor_id == actor_id), None
        )
        if data.supersedes_assessment_id != (None if mine is None else mine.id):
            raise _conflict(APPRAISAL_STALE)
    else:
        status = state.status(result)
        adjudicated = state.adjudicated.get(result)
        if status.value not in ("conflict", "adjudicated"):
            raise _conflict(NOT_IN_CONFLICT)
        tips = state.tips.get(result, [])
        if set(adjudication.resolves_assessment_ids) != {r.id for r in tips}:
            raise _conflict(APPRAISAL_STALE)
        if data.supersedes_assessment_id != (
            None if adjudicated is None else adjudicated.id
        ):
            raise _conflict(APPRAISAL_STALE)
        assessors = {
            r.assessor_id
            for r in state.by_key.get(result, [])
            if r.kind == "independent"
        }
        if actor_id in assessors:
            raise HTTPException(status_code=403, detail=SELF_ADJUDICATION)
        resolves = sorted(str(r.id) for r in tips)
    row = AppraisalAssessment(
        id=uuid4(),
        collection_id=collection_id,
        protocol_version_id=data.protocol_version_id,
        instrument_key=spec["key"],
        instrument_version=spec["version"],
        instrument_spec_hash=rules.SPEC_HASH,
        study_id=study_id,
        report_id=report_id,
        target_key=target_key,
        outcome_key=data.outcome_key,
        timepoint=data.timepoint,
        study_design=data.study_design,
        applicability=data.applicability,
        domains=domains,
        overall=data.overall,
        kind="adjudicated" if adjudication else "independent",
        actor_role="adjudicator" if adjudication else "reviewer",
        assessor_id=actor_id,
        resolves_assessment_ids=resolves,
        rationale=adjudication.rationale if adjudication else None,
        input_hash=canonical_json_sha256(
            {
                "spec_hash": rules.SPEC_HASH,
                "protocol_version_id": str(data.protocol_version_id),
                "evidence": pins,
            }
        ),
        supersedes_assessment_id=data.supersedes_assessment_id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if _is_unique_violation(error):
            raise _conflict(APPRAISAL_STALE) from error
        raise
    payload: dict[str, Any] = {
        "collection_id": str(collection_id),
        "assessment_id": str(row.id),
        "supersedes_assessment_id": (
            None
            if data.supersedes_assessment_id is None
            else str(data.supersedes_assessment_id)
        ),
        "target_key": target_key,
        "outcome_key": data.outcome_key,
        "timepoint": data.timepoint,
        "instrument_key": spec["key"],
        "instrument_version": spec["version"],
        "instrument_spec_hash": rules.SPEC_HASH,
        "protocol_version_id": str(data.protocol_version_id),
        "mode": mode,
        "study_design": data.study_design,
        "applicability": data.applicability,
        "overall": data.overall,
        "unresolved_domains": sorted(
            d for d, v in domains.items() if v["judgment"] is None
        ),
        "input_hash": row.input_hash,
    }
    if resolves is not None:
        payload["resolves_assessment_ids"] = resolves
    try:
        await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type=(
                "appraisal.adjudicated" if adjudication else "appraisal.submitted"
            ),
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role=cast(str, row.actor_role),
            subject_type=SUBJECT_TYPE,
            subject_id=cast(UUID, row.id),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=adjudication.rationale if adjudication else None,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise _conflict("Idempotency conflict") from exc
    await db.commit()
    await db.refresh(row)
    return await _row_response(db, row), False


async def submit(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: AppraisalSubmit
) -> tuple[AppraisalResponse, bool]:
    """REVIEW: one reviewer's independent assessment; commits once."""
    return await _write(db, context, actor_id, data, adjudication=None)


async def adjudicate(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: AppraisalAdjudicate,
) -> tuple[AppraisalResponse, bool]:
    """ADJUDICATE: resolve a conflict; the adjudicator assessed none of it."""
    return await _write(db, context, actor_id, data, adjudication=data)


# --- Reads -----------------------------------------------------------------------


def _instrument(method: _Method) -> AppraisalInstrument | None:
    if method.instrument is None:
        return None
    try:
        spec = rules.spec(method.instrument[0], method.instrument[1])
    except ValueError:
        return None
    return AppraisalInstrument(
        key=spec["key"],
        version=spec["version"],
        spec_hash=rules.SPEC_HASH,
        mode=method.mode,
        variant=spec["variant"],
        domains=[
            AppraisalInstrumentDomain(id=k, name=v["name"], signals=v["signals"])
            for k, v in spec["domains"].items()
        ],
        responses=list(rules.RESPONSES),
        judgments=list(rules.JUDGMENTS),
        designs=list(rules.DESIGNS),
        applies_to=list(spec["applies_to"]),
        licence=spec["licence"],
        encoding=spec["encoding"],
        source=spec["source"],
    )


def _target_ids(target_key: str) -> dict[str, UUID | None]:
    kind, _, value = target_key.partition(":")
    return {
        "study_id": UUID(value) if kind == "study" else None,
        "report_id": UUID(value) if kind == "report" else None,
    }


async def _units(db: AsyncSession, collection_id: UUID) -> set[str]:
    """Every live report's unit: the space of appraisable results, listed
    whether or not anyone has submitted, so a key never reveals a peer.
    ``# ponytail: O(reports x outcomes); page it if a project outgrows that.``"""
    reports = await _all(
        db,
        select(ResearchReport).where(
            ResearchReport.collection_id == collection_id,
            ResearchReport.merged_into_report_id.is_(None),
        ),
    )
    return {u for u in (analysis_unit(r) for r in reports) if u is not None}


async def _evidence_options(
    db: AsyncSession, collection_id: UUID
) -> dict[str, list[AppraisalEvidenceOption]]:
    units = await document_units(db, collection_id)
    if not units:
        return {}
    accepted = await _all(
        db,
        select(ExtractionAcceptedValue).where(
            ExtractionAcceptedValue.form_version_id.in_(
                _project_versions(collection_id)
            ),
            ExtractionAcceptedValue.document_id.in_(list(units)),
            _is_tip(),
        ),
    )
    quotes = {
        o.id: o.citation
        for o in await _all(
            db,
            select(ExtractionObservation).where(
                ExtractionObservation.id.in_(
                    [
                        a.anchor_observation_id
                        for a in accepted
                        if a.anchor_observation_id
                    ]
                    or [uuid4()]
                )
            ),
        )
    }
    options: dict[str, list[AppraisalEvidenceOption]] = defaultdict(list)
    for row in sorted(accepted, key=lambda a: (str(a.document_id), str(a.field_id))):
        options[units[row.document_id]].append(
            AppraisalEvidenceOption(
                id=row.id,
                document_id=row.document_id,
                field_id=row.field_id,
                value=row.value,
                missingness=row.missingness,
                quote=quotes.get(row.anchor_observation_id),
            )
        )
    return options


async def _results(
    db: AsyncSession,
    collection_id: UUID,
    viewer_id: UUID | None,
    method: _Method,
    *,
    listing: bool,
) -> list[AppraisalResult]:
    state = await _state(db, collection_id, method.mode)
    visible = _visible(state, viewer_id)
    stale = await _stale_nodes(db, collection_id) if state.rows else set()
    keys = {k for k, rows in state.by_key.items() if any(r.id in visible for r in rows)}
    options: dict[str, list[AppraisalEvidenceOption]] = {}
    if listing:
        keys |= {
            (unit, outcome, timepoint)
            for unit in await _units(db, collection_id)
            for outcome, timepoints in method.outcomes.items()
            for timepoint in timepoints
        }
        options = await _evidence_options(db, collection_id)
    names = await _names(db, [r.assessor_id for r in state.rows if r.id in visible])
    results = []
    for key in sorted(keys):
        rows = [
            _response(r, names, state.superseded, stale)
            for r in state.by_key.get(key, [])
            if r.id in visible
        ]
        status = (
            state.status(key)
            if key in state.revealed
            else rules.Status("awaiting_independent", [], [])
        )
        governing = set(status.current_ids) or {r.id for r in rows if not r.superseded}
        results.append(
            AppraisalResult(
                target_key=key[0],
                **_target_ids(key[0]),
                outcome_key=key[1],
                timepoint=key[2],
                status=cast(Any, status.value),
                unresolved_domains=status.unresolved_domains,
                stale=any(r.stale for r in rows if r.id in governing),
                mine=any(r.assessor_id == viewer_id for r in rows),
                rows=rows,
                evidence_options=options.get(key[0], []),
            )
        )
    return results


async def list_appraisals(
    db: AsyncSession, context: ProjectContext, viewer_id: UUID
) -> AppraisalListResponse:
    """VIEW: every appraisable result with its derived status; rows through
    the reveal predicate. An unrevealed result shows only ``awaiting`` and
    whether the viewer submitted, never a peer count."""
    collection_id = _cid(context)
    method = await _method(db, collection_id)
    return AppraisalListResponse(
        protocol_version_id=(
            UUID(method.version_id) if method.version_id is not None else None
        ),
        instrument=_instrument(method),
        outcomes={k: list(v) for k, v in method.outcomes.items()},
        results=await _results(db, collection_id, viewer_id, method, listing=True),
    )


async def export_package(
    db: AsyncSession, context: ProjectContext, viewer_id: UUID | None
) -> dict[str, Any]:
    """``nous.academic.appraisal.v1``: the visible rows of every result the
    viewer can see (the bundle passes ``viewer_id=None``: revealed only)."""
    collection_id = _cid(context)
    method = await _method(db, collection_id)
    instrument = _instrument(method)
    results = await _results(db, collection_id, viewer_id, method, listing=False)
    body = {
        "project_id": str(collection_id),
        "protocol_version_id": method.version_id,
        "instrument": None if instrument is None else instrument.model_dump(),
        "results": [
            r.model_dump(mode="json", exclude={"mine", "evidence_options"})
            for r in results
        ],
    }
    return {
        "schema": EXPORT_SCHEMA,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }
