"""Evidence tables, contradiction review and outcome certainty (GOO-310).

Lock order for writers: the route's ``resolve_project`` (Workspace SHARE ->
Collection UPDATE, roles reloaded after the lock), then this Collection's
``research_evidence`` stream. ``create_table``, ``record_contradiction`` and
``assess_certainty`` each commit exactly once. Rows are insert-only (database
triggers refuse UPDATE and DELETE); every change is a successor row.

A table version freezes one row per analysis unit from GOO-304 accepted-value
tips that are not stale under GOO-307's graph walk. Contradiction status,
dissent, certainty unresolved-contradiction lists and every ``stale`` flag
are derived on read; nothing is stamped. Model stance rows are read-only
suggestions: snapshotted into an opened contradiction only when a person
cites them, and never setting any state.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import AbstractSet, Any, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.evidence import StanceClassificationModel
from src.models.extraction_matrix import ExtractionAcceptedValue, ExtractionFormVersion
from src.models.research_evidence_table import (
    EvidenceContradiction,
    EvidenceTableVersion,
    OutcomeCertaintyAssessment,
)
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_report import ResearchReport
from src.schemas.research_engine import (
    CertaintyCreate,
    CertaintyResponse,
    ContradictionCreate,
    ContradictionDissent,
    ContradictionResponse,
    ContradictionRowResponse,
    EvidenceCertaintyMethod,
    EvidenceOutcome,
    EvidenceOutcomeListResponse,
    EvidenceTableCreate,
    EvidenceTablePreview,
    EvidenceTableResponse,
    EvidenceUnreviewedCell,
)
from src.services.research import extraction_rules, release_rules
from src.services.research.extraction_forms_service import (
    _is_tip,
    _matrix,
    cell_view,
    current_version,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import evidence_rules as rules
from src.services.research_engine import protocol_methods
from src.services.research_engine.acquisition_service import document_reports
from src.services.research_engine.appraisal_service import (
    _names,
    _stale_nodes,
    current_appraisals,
)
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import (
    _replayed_event,
    analysis_unit,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.services.research_engine.screening_service import _is_unique_violation

AGGREGATE_TYPE = "research_evidence"
SUBJECT_TYPE = "evidence_outcome"
EXPORT_SCHEMA = "nous.academic.evidence.v1"

NO_PROTOCOL = "Project has no approved protocol"
UNDECLARED = "Outcome or timepoint is not declared by the protocol"
NO_FORM = "Matrix has no form version"
FIELD_NOT_CURRENT = "Field is not in the matrix's current form version"
TABLE_NOT_FOUND = "Evidence table not found"
TABLE_STALE = "Evidence table is stale; reload"
MEMBERS = "Contradiction members must be table cells"
CONTRADICTION_NOT_FOUND = "Contradiction not found"
CONTRADICTION_STALE = "Contradiction is stale; reload"
SUGGESTION_NOT_FOUND = "Suggestion not found"
CERTAINTY_STALE = "Certainty assessment is stale; reload"
_ROLES = {
    "reviewer": ResearchProjectRole.REVIEWER,
    "adjudicator": ResearchProjectRole.ADJUDICATOR,
}

Edge = tuple[release_rules.Node, release_rules.Node]
OutcomeKey = tuple[str, str]  # (outcome_key, timepoint)


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _require_role(context: ProjectContext, actor_role: str) -> None:
    """The service never trusts the route's role label on its own."""
    if _ROLES.get(actor_role) not in context.effective_roles:
        raise HTTPException(status_code=403, detail=f"{actor_role} role required")


# --- Protocol --------------------------------------------------------------------


@dataclass(frozen=True)
class _Method:
    version_id: str | None
    outcomes: dict[str, tuple[str, ...]]
    certainty: tuple[str, str] | None


async def _snapshot(db: AsyncSession, version_id: str) -> Mapping[str, Any]:
    version = await db.get(ResearchProtocolVersion, UUID(version_id))
    return cast(Mapping[str, Any], version.snapshot if version else {})


async def _method(db: AsyncSession, collection_id: UUID) -> _Method:
    """The current approved protocol's outcomes and certainty method; lenient."""
    version_id = await current_protocol_version_id(db, collection_id)
    if version_id is None:
        return _Method(None, {}, None)
    snapshot = await _snapshot(db, version_id)
    try:
        outcomes = protocol_methods.declared_outcomes(snapshot)
    except ValueError:
        outcomes = {}
    try:
        certainty: tuple[str, str] | None = protocol_methods.certainty_method(snapshot)
    except ValueError:
        certainty = None
    return _Method(version_id, outcomes, certainty)


async def _declared(
    db: AsyncSession, collection_id: UUID, outcome_key: str, timepoint: str
) -> str:
    """The approved protocol version id, if it declares (outcome, timepoint)."""
    method = await _method(db, collection_id)
    if method.version_id is None:
        raise _conflict(NO_PROTOCOL)
    if timepoint not in method.outcomes.get(outcome_key, ()):
        raise _unprocessable(UNDECLARED)
    return method.version_id


# --- Building a table -------------------------------------------------------------


@dataclass
class _Units:
    of_document: dict[UUID, tuple[str, UUID]]  # document -> (unit, report)
    reports: dict[str, list[UUID]]  # unit -> report ids
    excluded: list[dict[str, Any]]


async def _units(db: AsyncSession, collection_id: UUID) -> _Units:
    """Every live project document with a unit; the rest are excluded with
    the reason (unresolved study link, or no report identity at all)."""
    documents = {
        cast(UUID, d.id) for d in await _all(db, project_documents_query(collection_id))
    }
    by_document = {
        d: r
        for d, r in (await document_reports(db, collection_id)).items()
        if d in documents
    }
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
    units = _Units({}, defaultdict(list), [])
    for document in sorted(documents, key=str):
        report_id = by_document.get(document)
        report = reports.get(report_id) if report_id is not None else None
        unit = analysis_unit(report) if report is not None else None
        if unit is None:
            units.excluded.append(
                {
                    "document_id": str(document),
                    "report_id": None if report is None else str(report_id),
                    "reason": (
                        "no_report_identity"
                        if report is None
                        else "study_link_unresolved"
                    ),
                }
            )
            continue
        units.of_document[document] = (unit, cast(UUID, report_id))
        if report_id not in units.reports[unit]:
            units.reports[unit].append(cast(UUID, report_id))
    return units


@dataclass(frozen=True)
class _Built:
    protocol_version_id: str
    matrix: Any
    form_version_id: UUID
    field_ids: list[UUID]
    rows: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    content_hash: str
    document_ids: list[UUID]


async def _fields(
    db: AsyncSession, matrix_id: UUID, field_ids: Sequence[UUID], timepoint: str
) -> Any:
    """The matrix's current form version, if every field is on it at
    ``timepoint`` (GOO-304 fields carry their own timepoint)."""
    version = await current_version(db, matrix_id)
    if version is None:
        raise _unprocessable(NO_FORM)
    for field_id in field_ids:
        field = extraction_rules.find_field(version.fields, field_id)
        if field is None:
            raise _unprocessable(FIELD_NOT_CURRENT)
        definition = extraction_rules.field_def(version.fields, field_id) or {}
        if definition.get("timepoint") != timepoint:
            raise _unprocessable(f"Field {field['name']} is not at {timepoint}")
    return version


async def _tips(
    db: AsyncSession,
    matrix_id: UUID,
    field_ids: Sequence[UUID],
    units: _Units,
    stale: AbstractSet[Any],
) -> list[rules.Tip]:
    """Accepted-value tips on the unit documents, minus every one GOO-307's
    walk marks stale: a superseded or source-changed value never enters."""
    rows = await _all(
        db,
        select(ExtractionAcceptedValue).where(
            ExtractionAcceptedValue.form_version_id.in_(
                select(ExtractionFormVersion.id).where(
                    ExtractionFormVersion.matrix_id == matrix_id
                )
            ),
            ExtractionAcceptedValue.field_id.in_(list(field_ids)),
            ExtractionAcceptedValue.document_id.in_(
                list(units.of_document) or [uuid4()]
            ),
            _is_tip(),
        ),
    )
    tips = []
    for row in rows:
        if release_rules.node("accepted", row.id) in stale:
            continue
        unit, report_id = units.of_document[row.document_id]
        tips.append(
            rules.Tip(
                accepted_value_id=cast(UUID, row.id),
                document_id=cast(UUID, row.document_id),
                report_id=report_id,
                unit=unit,
                field_id=cast(UUID, row.field_id),
                source_hash=str(row.source_hash),
                text_sha256=cast(str | None, row.text_sha256),
                value=row.value,
                missingness=cast(str | None, row.missingness),
            )
        )
    return tips


async def _build(
    db: AsyncSession,
    context: ProjectContext,
    outcome_key: str,
    timepoint: str,
    matrix_id: UUID,
    field_ids: Sequence[UUID],
) -> _Built:
    collection_id = _cid(context)
    protocol_version_id = await _declared(db, collection_id, outcome_key, timepoint)
    matrix = await _matrix(db, context, matrix_id)  # 404 unless this project's
    version = await _fields(db, matrix_id, field_ids, timepoint)
    units = await _units(db, collection_id)
    stale = await _stale_nodes(db, collection_id)
    tips = await _tips(db, matrix_id, field_ids, units, stale)
    rows = rules.build_rows(units.reports, tips, field_ids, outcome_key, timepoint)
    content_hash = rules.table_hash(
        protocol_version_id,
        version.id,
        outcome_key,
        timepoint,
        field_ids,
        rows,
        units.excluded,
    )
    return _Built(
        protocol_version_id=protocol_version_id,
        matrix=matrix,
        form_version_id=cast(UUID, version.id),
        field_ids=list(field_ids),
        rows=rows,
        excluded=units.excluded,
        content_hash=content_hash,
        document_ids=sorted(units.of_document, key=str),
    )


# --- Rows and derived state -------------------------------------------------------


@dataclass
class _State:
    tables: list[Any]
    contradictions: list[Any]
    certainty: list[Any]

    def superseded_tables(self) -> set[Any]:
        return {t.supersedes_table_id for t in self.tables if t.supersedes_table_id}

    def table_tip(self, key: OutcomeKey) -> Any:
        superseded = self.superseded_tables()
        return next(
            (
                t
                for t in self.tables
                if (t.outcome_key, t.timepoint) == key and t.id not in superseded
            ),
            None,
        )

    def superseded_certainty(self) -> set[Any]:
        return {
            c.supersedes_certainty_id
            for c in self.certainty
            if c.supersedes_certainty_id
        }

    def certainty_tip(self, key: OutcomeKey) -> Any:
        superseded = self.superseded_certainty()
        return next(
            (
                c
                for c in self.certainty
                if (c.outcome_key, c.timepoint) == key and c.id not in superseded
            ),
            None,
        )

    def chains(self) -> dict[Any, list[Any]]:
        """contradiction_id -> rows in chain order (opened first)."""
        by_group: dict[Any, dict[Any, Any]] = defaultdict(dict)
        for row in self.contradictions:
            by_group[row.contradiction_id][row.previous_id] = row
        ordered: dict[Any, list[Any]] = {}
        for group, by_previous in by_group.items():
            chain, row = [], by_previous.get(None)
            while row is not None:
                chain.append(row)
                row = by_previous.get(row.id)
            ordered[group] = chain
        return ordered


async def _state(db: AsyncSession, collection_id: UUID) -> _State:
    def ordered(model: Any) -> Any:
        return (
            select(model)
            .where(model.collection_id == collection_id)
            .order_by(model.created_at, model.id)
        )

    return _State(
        await _all(db, ordered(EvidenceTableVersion)),
        await _all(db, ordered(EvidenceContradiction)),
        await _all(db, ordered(OutcomeCertaintyAssessment)),
    )


async def table_version(
    db: AsyncSession, collection_id: UUID, table_version_id: UUID
) -> Any:
    """One table version scoped to its Collection; a foreign id is 404
    (GOO-311's only input set)."""
    row = (
        await db.execute(
            select(EvidenceTableVersion).where(
                EvidenceTableVersion.id == table_version_id,
                EvidenceTableVersion.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=TABLE_NOT_FOUND)
    return row


def _chain_rows(chain: Sequence[Any]) -> list[rules.ChainRow]:
    return [
        rules.ChainRow(
            id=cast(UUID, r.id),
            kind=str(r.kind),
            actor_id=cast(UUID, r.actor_id),
            actor_role=str(r.actor_role),
            explanation=str(r.explanation),
        )
        for r in chain
    ]


def _dissent(chain: Sequence[Any]) -> list[ContradictionDissent]:
    return [
        ContradictionDissent(contradiction_id=chain[0].contradiction_id, **item)
        for item in rules.dissent(_chain_rows(chain))
    ]


def _group(
    chain: Sequence[Any], names: Mapping[UUID, str], stale: AbstractSet[Any]
) -> ContradictionResponse:
    opened = chain[0]
    rows = []
    for row in chain:
        response = cast(
            ContradictionRowResponse, ContradictionRowResponse.model_validate(row)
        )
        rows.append(
            cast(
                ContradictionRowResponse,
                response.model_copy(
                    update={"actor_name": names.get(cast(UUID, row.actor_id))}
                ),
            )
        )
    return ContradictionResponse(
        contradiction_id=opened.contradiction_id,
        table_version_id=opened.table_version_id,
        field_id=opened.field_id,
        status=rules.contradiction_status(_chain_rows(chain)),
        rows=rows,
        dissent=_dissent(chain),
        stale=release_rules.node("contradiction", opened.contradiction_id) in stale,
    )


def _table_response(
    row: Any, superseded: AbstractSet[Any], stale: AbstractSet[Any]
) -> EvidenceTableResponse:
    response = cast(EvidenceTableResponse, EvidenceTableResponse.model_validate(row))
    return cast(
        EvidenceTableResponse,
        response.model_copy(
            update={
                "superseded": row.id in superseded,
                "stale": release_rules.node("evidence_table", row.id) in stale,
            }
        ),
    )


def _certainty_response(
    row: Any,
    state: _State,
    names: Mapping[UUID, str],
    stale: AbstractSet[Any],
) -> CertaintyResponse:
    groups = [
        chain
        for chain in state.chains().values()
        if chain and chain[0].table_version_id == row.table_version_id
    ]
    response = cast(CertaintyResponse, CertaintyResponse.model_validate(row))
    return cast(
        CertaintyResponse,
        response.model_copy(
            update={
                "assessor_name": names.get(cast(UUID, row.assessed_by_id)),
                "superseded": row.id in state.superseded_certainty(),
                "stale": release_rules.node("certainty", row.id) in stale,
                "unresolved_contradictions": [
                    chain[0].contradiction_id
                    for chain in groups
                    if rules.contradiction_status(_chain_rows(chain)) == "unresolved"
                ],
                "dissent": [d for chain in groups for d in _dissent(chain)],
            }
        ),
    )


# --- Evidence graph (GOO-307's walk) ----------------------------------------------


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Edge], set[release_rules.Node]]:
    """Edges ``accepted -> evidence_table`` (every cell tip), ``protocol ->
    evidence_table``, ``evidence_table -> certainty``, ``appraisal ->
    certainty`` and ``accepted -> contradiction`` (its members); ``changed``
    holds superseded table and certainty rows and protocol versions no longer
    current. Called from ``draft_release_service._graph``."""
    state = await _state(db, collection_id)
    if not state.tables:
        return [], set()
    node = release_rules.node
    edges: list[Edge] = []
    for table in state.tables:
        target = node("evidence_table", table.id)
        edges.append((node("protocol", table.protocol_version_id), target))
        for row in table.rows:
            for cell in row["cells"].values():
                for tip in cell["tips"]:
                    edges.append((node("accepted", tip["accepted_value_id"]), target))
    for row in state.contradictions:
        for value in row.accepted_value_ids or []:
            edges.append(
                (node("accepted", value), node("contradiction", row.contradiction_id))
            )
    for row in state.certainty:
        target = node("certainty", row.id)
        edges.append((node("evidence_table", row.table_version_id), target))
        for value in row.appraisal_assessment_ids or []:
            edges.append((node("appraisal", value), target))
    current = await current_protocol_version_id(db, collection_id)
    changed = {node("evidence_table", t) for t in state.superseded_tables()}
    changed |= {node("certainty", c) for c in state.superseded_certainty()}
    changed |= {
        node("protocol", t.protocol_version_id)
        for t in state.tables
        if str(t.protocol_version_id) != current
    }
    return edges, changed


# --- Reads -----------------------------------------------------------------------


async def preview(
    db: AsyncSession,
    context: ProjectContext,
    outcome_key: str,
    timepoint: str,
    matrix_id: UUID,
    field_ids: Sequence[UUID],
) -> EvidenceTablePreview:
    """VIEW: exactly what ``create_table`` would freeze, plus the unreviewed
    machine/legacy cells and model stance suggestions it never freezes."""
    built = await _build(db, context, outcome_key, timepoint, matrix_id, field_ids)
    tip = (await _state(db, _cid(context))).table_tip((outcome_key, timepoint))
    wanted = {str(f) for f in field_ids}
    _, cells = await cell_view(db, built.matrix, built.document_ids)
    unreviewed = [
        EvidenceUnreviewedCell(
            document_id=cell["document_id"],
            field_id=cell["field_id"],
            column_name=cell["column_name"],
            value=cell["value"],
            missingness=cell["missingness"],
            source=cell["source"],
        )
        for cell in cells
        if cell["source"] in ("machine", "legacy") and cell["field_id"] in wanted
    ]
    return EvidenceTablePreview(
        protocol_version_id=UUID(built.protocol_version_id),
        outcome_key=outcome_key,
        timepoint=timepoint,
        matrix_id=matrix_id,
        form_version_id=built.form_version_id,
        field_ids=built.field_ids,
        rows=cast(Any, built.rows),
        excluded=cast(Any, built.excluded),
        content_hash=built.content_hash,
        tip_id=None if tip is None else tip.id,
        differs_from_tip=tip is not None and tip.content_hash != built.content_hash,
        unreviewed_cells=unreviewed,
        stance_suggestions=cast(
            Any, rules.stance_groups(await _suggestions(db, context, built))
        ),
    )


def _suggestion_row(row: Any) -> dict[str, Any]:
    stance = row.stance
    return {
        "id": str(row.id),
        "claim_hash": row.claim_hash,
        "claim_text": row.claim_text,
        "source_id": str(row.source_id),
        "source_content_hash": row.source_content_hash,
        "stance": getattr(stance, "value", stance),
        "confidence": row.confidence,
        "model_version": row.model_version,
        "inference_model_version": row.inference_model_version,
    }


async def _suggestions(
    db: AsyncSession, context: ProjectContext, built: _Built
) -> list[dict[str, Any]]:
    """The org's own stance rows on the table's documents; no org fallback."""
    rows = await _all(
        db,
        select(StanceClassificationModel).where(
            StanceClassificationModel.organization_id == context.organization_id,
            StanceClassificationModel.source_id.in_(built.document_ids or [uuid4()]),
        ),
    )
    return [_suggestion_row(row) for row in rows]


async def list_outcomes(
    db: AsyncSession, context: ProjectContext
) -> EvidenceOutcomeListResponse:
    """VIEW: every table version, contradiction chain and certainty row per
    declared outcome and timepoint, with staleness derived on this read."""
    collection_id = _cid(context)
    method = await _method(db, collection_id)
    state = await _state(db, collection_id)
    rows_exist = state.tables or state.contradictions or state.certainty
    stale = await _stale_nodes(db, collection_id) if rows_exist else set()
    names = await _names(
        db,
        [r.actor_id for r in state.contradictions]
        + [r.assessed_by_id for r in state.certainty],
    )
    keys = {(o, t) for o, timepoints in method.outcomes.items() for t in timepoints}
    keys |= {(t.outcome_key, t.timepoint) for t in state.tables}
    superseded = state.superseded_tables()
    chains = state.chains()
    outcomes = []
    for key in sorted(keys):
        tables = [t for t in state.tables if (t.outcome_key, t.timepoint) == key]
        table_ids = {t.id for t in tables}
        outcomes.append(
            EvidenceOutcome(
                outcome_key=key[0],
                timepoint=key[1],
                tables=[_table_response(t, superseded, stale) for t in tables],
                contradictions=[
                    _group(chain, names, stale)
                    for chain in chains.values()
                    if chain and chain[0].table_version_id in table_ids
                ],
                certainty=[
                    _certainty_response(c, state, names, stale)
                    for c in state.certainty
                    if (c.outcome_key, c.timepoint) == key
                ],
            )
        )
    return EvidenceOutcomeListResponse(
        protocol_version_id=(
            None if method.version_id is None else UUID(method.version_id)
        ),
        certainty_method=(
            None
            if method.certainty is None
            else EvidenceCertaintyMethod(
                method=method.certainty[0],
                version=method.certainty[1],
                domains=list(rules.GRADE_DOMAINS),
                levels=list(rules.LEVELS),
                starting_levels=list(rules.STARTING_LEVELS),
            )
        ),
        outcomes=outcomes,
    )


async def export_package(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """``nous.academic.evidence.v1``: every version, stale ones included."""
    listing = await list_outcomes(db, context)
    body = {"project_id": str(_cid(context)), **listing.model_dump(mode="json")}
    return {
        "schema": EXPORT_SCHEMA,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }


# --- Writes ----------------------------------------------------------------------


def _fingerprint(operation: str, actor_id: UUID, actor_role: str, data: Any) -> str:
    return decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor_id),
            "actor_role": actor_role,
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )


async def _lock(db: AsyncSession, collection_id: UUID) -> Any:
    return await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )


async def _insert(db: AsyncSession, row: Any, stale_detail: str) -> None:
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if _is_unique_violation(error):
            raise _conflict(stale_detail) from error
        raise


async def _append(
    db: AsyncSession,
    collection_id: UUID,
    *,
    event_type: str,
    actor_id: UUID,
    actor_role: str,
    subject_id: UUID,
    payload: dict[str, Any],
    reason: str | None,
    key: str,
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
            actor_user_id=actor_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=subject_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise _conflict("Idempotency conflict") from exc


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


async def create_table(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    actor_role: str,
    data: EvidenceTableCreate,
) -> tuple[EvidenceTableResponse, bool]:
    """REVIEW | ADJUDICATE: freeze a table version; an unchanged rebuild
    returns the tip and writes nothing. Commits once."""
    collection_id = _cid(context)
    _require_role(context, actor_role)
    stream = await _lock(db, collection_id)
    key = f"table:{data.idempotency_key}"
    fingerprint = _fingerprint("table", actor_id, actor_role, data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        table_id = cast(dict[str, Any], replay.payload)["table_version_id"]
        row = await table_version(db, collection_id, UUID(table_id))
        return _table_response(row, set(), set()), True
    built = await _build(
        db, context, data.outcome_key, data.timepoint, data.matrix_id, data.field_ids
    )
    tip = (await _state(db, collection_id)).table_tip(
        (data.outcome_key, data.timepoint)
    )
    if tip is not None and tip.content_hash == built.content_hash:
        return _table_response(tip, set(), set()), True
    if data.supersedes_table_id != (None if tip is None else tip.id):
        raise _conflict(TABLE_STALE)
    row = EvidenceTableVersion(
        id=uuid4(),
        collection_id=collection_id,
        protocol_version_id=UUID(built.protocol_version_id),
        outcome_key=data.outcome_key,
        timepoint=data.timepoint,
        matrix_id=data.matrix_id,
        form_version_id=built.form_version_id,
        field_ids=[str(f) for f in built.field_ids],
        rows=built.rows,
        excluded=built.excluded,
        content_hash=built.content_hash,
        created_by_id=actor_id,
        supersedes_table_id=data.supersedes_table_id,
    )
    await _insert(db, row, TABLE_STALE)
    payload = {
        "collection_id": str(collection_id),
        "table_version_id": str(row.id),
        "supersedes_table_id": _str(data.supersedes_table_id),
        "outcome_key": data.outcome_key,
        "timepoint": data.timepoint,
        "protocol_version_id": built.protocol_version_id,
        "matrix_id": str(data.matrix_id),
        "form_version_id": str(built.form_version_id),
        "field_ids": [str(f) for f in built.field_ids],
        "content_hash": built.content_hash,
        "row_count": len(built.rows),
        "excluded_count": len(built.excluded),
    }
    await _append(
        db,
        collection_id,
        event_type="evidence.table_versioned",
        actor_id=actor_id,
        actor_role=actor_role,
        subject_id=cast(UUID, row.id),
        payload=payload,
        reason=None,
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    await db.refresh(row)
    return _table_response(row, set(), set()), False


async def _snapshot_suggestions(
    db: AsyncSession, context: ProjectContext, ids: Sequence[UUID]
) -> dict[str, Any] | None:
    """An attributed copy of the cited stance rows (the table is mutable);
    another organization's row is not found."""
    if not ids:
        return None
    rows = await _all(
        db,
        select(StanceClassificationModel).where(
            StanceClassificationModel.id.in_(list(ids)),
            StanceClassificationModel.organization_id == context.organization_id,
        ),
    )
    if len(rows) != len(set(ids)):
        raise HTTPException(status_code=404, detail=SUGGESTION_NOT_FOUND)
    return {
        "origin": "model_suggestion",
        "review_state": "unreviewed_model_suggestion",
        "rows": sorted((_suggestion_row(r) for r in rows), key=lambda r: r["id"]),
    }


async def record_contradiction(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    actor_role: str,
    data: ContradictionCreate,
) -> tuple[ContradictionResponse, bool]:
    """``opened``/``dissent``: REVIEW | ADJUDICATE; ``resolved``/
    ``acknowledged``: ADJUDICATE only. Commits once."""
    collection_id = _cid(context)
    if data.kind in rules.DECIDING_KINDS and actor_role != "adjudicator":
        raise HTTPException(status_code=403, detail="adjudicator role required")
    _require_role(context, actor_role)
    stream = await _lock(db, collection_id)
    key = f"contradiction:{data.idempotency_key}"
    fingerprint = _fingerprint("contradiction", actor_id, actor_role, data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        group = cast(dict[str, Any], replay.payload)["contradiction_id"]
        return await _group_response(db, collection_id, UUID(group)), True
    row_id = uuid4()
    if data.kind == "opened":
        table = await table_version(
            db, collection_id, cast(UUID, data.table_version_id)
        )
        members = {str(v) for v in data.accepted_value_ids or []}
        if str(data.field_id) not in table.field_ids or not members <= (
            rules.cell_members(table.rows, data.field_id)
        ):
            raise _unprocessable(MEMBERS)
        suggestion = await _snapshot_suggestions(
            db, context, data.stance_classification_ids
        )
        row = EvidenceContradiction(
            id=row_id,
            collection_id=collection_id,
            contradiction_id=row_id,
            table_version_id=table.id,
            field_id=data.field_id,
            accepted_value_ids=sorted(members),
            kind="opened",
            explanation=data.explanation,
            actor_id=actor_id,
            actor_role=actor_role,
            suggestion=suggestion,
            previous_id=None,
        )
    else:
        chain = (await _state(db, collection_id)).chains().get(data.contradiction_id)
        if not chain:
            raise HTTPException(status_code=404, detail=CONTRADICTION_NOT_FOUND)
        if data.previous_id != chain[-1].id:
            raise _conflict(CONTRADICTION_STALE)
        suggestion = None
        row = EvidenceContradiction(
            id=row_id,
            collection_id=collection_id,
            contradiction_id=chain[0].id,
            table_version_id=chain[0].table_version_id,
            field_id=chain[0].field_id,
            accepted_value_ids=None,
            kind=data.kind,
            explanation=data.explanation,
            actor_id=actor_id,
            actor_role=actor_role,
            suggestion=None,
            previous_id=data.previous_id,
        )
    await _insert(db, row, CONTRADICTION_STALE)
    payload = {
        "collection_id": str(collection_id),
        "contradiction_id": str(row.contradiction_id),
        "row_id": str(row.id),
        "previous_id": _str(row.previous_id),
        "kind": data.kind,
        "table_version_id": str(row.table_version_id),
        "field_id": str(row.field_id),
        "accepted_value_ids": row.accepted_value_ids,
        "suggestion_ids": [r["id"] for r in (suggestion or {}).get("rows", [])],
    }
    await _append(
        db,
        collection_id,
        event_type="evidence.contradiction_recorded",
        actor_id=actor_id,
        actor_role=actor_role,
        subject_id=row_id,
        payload=payload,
        reason=data.explanation,
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return (
        await _group_response(db, collection_id, cast(UUID, row.contradiction_id)),
        False,
    )


async def _group_response(
    db: AsyncSession, collection_id: UUID, contradiction_id: UUID
) -> ContradictionResponse:
    chain = (await _state(db, collection_id)).chains()[contradiction_id]
    names = await _names(db, [r.actor_id for r in chain])
    return _group(chain, names, set())


async def assess_certainty(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: CertaintyCreate,
) -> tuple[CertaintyResponse, bool]:
    """REVIEW: GRADE ratings over the current, non-stale table tip. A
    risk-of-bias rating rests on every unit's resolved appraisal; the level
    is always the derived one. Commits once."""
    collection_id = _cid(context)
    _require_role(context, "reviewer")
    stream = await _lock(db, collection_id)
    key = f"certainty:{data.idempotency_key}"
    fingerprint = _fingerprint("certainty", actor_id, "reviewer", data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        certainty_id = cast(dict[str, Any], replay.payload)["certainty_id"]
        return await _certainty_by_id(db, collection_id, UUID(certainty_id)), True
    method = await _method(db, collection_id)
    if method.version_id is None:
        raise _conflict(NO_PROTOCOL)
    try:
        method_key, method_version = protocol_methods.certainty_method(
            await _snapshot(db, method.version_id)
        )
    except ValueError as error:
        raise _conflict(str(error)) from error
    table = await table_version(db, collection_id, data.table_version_id)
    outcome = (str(table.outcome_key), str(table.timepoint))
    state = await _state(db, collection_id)
    tip = state.table_tip(outcome)
    stale = await _stale_nodes(db, collection_id)
    if (
        tip is None
        or tip.id != table.id
        or release_rules.node("evidence_table", table.id) in stale
    ):
        raise _conflict(TABLE_STALE)
    appraisals = await current_appraisals(db, collection_id)
    statuses: dict[str, str | None] = {}
    required: set[str] = set()
    for row in table.rows:
        status = appraisals.get((row["unit"], outcome[0], outcome[1]))
        statuses[row["unit"]] = None if status is None else status.value
        required |= {str(v) for v in (status.current_ids if status else [])}
    groups = [
        chain[0].contradiction_id
        for chain in state.chains().values()
        if chain and chain[0].table_version_id == table.id
    ]
    ratings = data.ratings.model_dump()
    try:
        rules.check_certainty(
            data.starting_level,
            ratings,
            data.level,
            data.appraisal_assessment_ids,
            sorted(required),
            statuses,
            data.contradiction_ids,
            groups,
        )
    except ValueError as error:
        raise _unprocessable(str(error)) from error
    current = state.certainty_tip(outcome)
    if data.supersedes_certainty_id != (None if current is None else current.id):
        raise _conflict(CERTAINTY_STALE)
    appraisal_ids = sorted({str(v) for v in data.appraisal_assessment_ids})
    contradiction_ids = sorted({str(v) for v in data.contradiction_ids})
    input_hash = canonical_json_sha256(
        {
            "table_content_hash": table.content_hash,
            "method": [method_key, method_version],
            "starting_level": data.starting_level,
            "ratings": ratings,
            "appraisal_assessment_ids": appraisal_ids,
            "contradiction_ids": contradiction_ids,
        }
    )
    row = OutcomeCertaintyAssessment(
        id=uuid4(),
        collection_id=collection_id,
        table_version_id=table.id,
        outcome_key=outcome[0],
        timepoint=outcome[1],
        method_key=method_key,
        method_version=method_version,
        starting_level=data.starting_level,
        domains=ratings,
        level=data.level,
        appraisal_assessment_ids=appraisal_ids,
        contradiction_ids=contradiction_ids,
        rationale=data.rationale,
        assessed_by_id=actor_id,
        actor_role="reviewer",
        input_hash=input_hash,
        supersedes_certainty_id=data.supersedes_certainty_id,
    )
    await _insert(db, row, CERTAINTY_STALE)
    payload = {
        "collection_id": str(collection_id),
        "certainty_id": str(row.id),
        "supersedes_certainty_id": _str(data.supersedes_certainty_id),
        "table_version_id": str(table.id),
        "outcome_key": outcome[0],
        "timepoint": outcome[1],
        "method_key": method_key,
        "method_version": method_version,
        "starting_level": data.starting_level,
        "ratings": ratings,
        "level": data.level,
        "appraisal_assessment_ids": appraisal_ids,
        "contradiction_ids": contradiction_ids,
        "input_hash": input_hash,
    }
    await _append(
        db,
        collection_id,
        event_type="evidence.certainty_assessed",
        actor_id=actor_id,
        actor_role="reviewer",
        subject_id=cast(UUID, row.id),
        payload=payload,
        reason=data.rationale,
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _certainty_by_id(db, collection_id, cast(UUID, row.id)), False


async def _certainty_by_id(
    db: AsyncSession, collection_id: UUID, certainty_id: UUID
) -> CertaintyResponse:
    state = await _state(db, collection_id)
    row = next(c for c in state.certainty if c.id == certainty_id)
    names = await _names(db, [row.assessed_by_id])
    return _certainty_response(row, state, names, set())
