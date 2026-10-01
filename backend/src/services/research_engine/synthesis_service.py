"""One protocol-selected quantitative synthesis over a frozen evidence table (GOO-311).

Lock order for ``execute``: the route's ``resolve_project`` (Workspace SHARE
-> Collection UPDATE, roles reloaded after the lock), then this Collection's
``research_synthesis`` stream, then (inside ``invalidate_dependents``, last)
``research_release``. ``execute`` commits exactly once. Rows are insert-only
(a database trigger refuses UPDATE and DELETE): a changed input is a
successor row, an unchanged input returns the tip and writes nothing.

The only input is a GOO-310 ``evidence_table_versions`` row, one row per
analysis unit, so this module never reads accepted values itself. The
arithmetic is ``synthesis_rules`` (stdlib, deterministic); nothing here calls
a model. Staleness is derived on read through GOO-307's graph walk.
"""

import platform
from dataclasses import dataclass
from typing import AbstractSet, Any, Mapping, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.extraction_matrix import ExtractionFormVersion
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_synthesis import SynthesisResult
from src.schemas.research_engine import (
    SynthesisExecute,
    SynthesisListResponse,
    SynthesisPreview,
    SynthesisResultResponse,
    SynthesisSelection,
)
from src.services.research import extraction_rules, release_rules
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import evidence_service, protocol_methods
from src.services.research_engine import synthesis_rules as rules
from src.services.research_engine.appraisal_service import _stale_nodes
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import (
    _replayed_event,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.screening_service import _is_unique_violation

AGGREGATE_TYPE = "research_synthesis"
SUBJECT_TYPE = "synthesis_result"
EXPORT_SCHEMA = "nous.academic.synthesis.v1"

NO_PROTOCOL = "Project has no approved protocol"
TABLE_STALE = evidence_service.TABLE_STALE
TABLE_OTHER_OUTCOME = "Evidence table is not the protocol-selected outcome"
INPUTS_CHANGED = "Inputs changed since preview; reload"
RESULT_STALE = "Synthesis result is stale; reload"
RESULT_NOT_CURRENT = "Synthesis result is not current"
RESULT_NOT_FOUND = "Synthesis result not found"

Edge = tuple[release_rules.Node, release_rules.Node]


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


# --- Protocol selection and inputs ------------------------------------------------


async def _selection(
    db: AsyncSession, collection_id: UUID
) -> tuple[str, tuple[str, str, str, str]]:
    """The approved protocol version id and its synthesis selection; 409s."""
    version_id = await current_protocol_version_id(db, collection_id)
    if version_id is None:
        raise _conflict(NO_PROTOCOL)
    version = await db.get(ResearchProtocolVersion, UUID(version_id))
    snapshot = cast(Mapping[str, Any], version.snapshot if version else {})
    try:
        return version_id, protocol_methods.synthesis_selection(snapshot)
    except ValueError as error:
        raise _conflict(str(error)) from error


@dataclass(frozen=True)
class _Inputs:
    protocol_version_id: str
    selection: tuple[str, str, str, str]
    table: Any
    config: dict[str, Any]
    config_hash: str
    included: list[rules.UnitInput]
    excluded: list[rules.Exclusion]
    failures: list[rules.Exclusion]
    input_hash: str


async def _inputs(
    db: AsyncSession,
    context: ProjectContext,
    table_version_id: UUID,
    roles: Mapping[str, Any],
) -> _Inputs:
    """Everything a run consumes, from one current table version."""
    collection_id = _cid(context)
    version_id, selection = await _selection(db, collection_id)
    table = await evidence_service.table_version(db, collection_id, table_version_id)
    if (table.outcome_key, table.timepoint) != selection[2:]:
        raise _conflict(TABLE_OTHER_OUTCOME)
    stale = await _stale_nodes(db, collection_id)
    if (
        str(table.protocol_version_id) != version_id
        or release_rules.node("evidence_table", table.id) in stale
    ):
        raise _conflict(TABLE_STALE)
    form = await db.get(ExtractionFormVersion, table.form_version_id)
    fields: list[Any] = list(form.fields) if form is not None else []
    fields_by_id = {
        str(field_id): extraction_rules.field_def(fields, str(field_id)) or {}
        for field_id in table.field_ids
    }
    role_ids = {role: str(roles[role]) for role in rules.ROLES}
    config = rules.config(role_ids)
    config_hash = rules.config_hash(config)
    included, excluded = rules.select_inputs(table.rows, role_ids, table.excluded)
    return _Inputs(
        protocol_version_id=version_id,
        selection=selection,
        table=table,
        config=config,
        config_hash=config_hash,
        included=included,
        excluded=excluded,
        failures=rules.check_config(fields_by_id, role_ids, selection[3]),
        input_hash=rules.input_hash(table.content_hash, config_hash, version_id),
    )


def _selection_response(selection: tuple[str, str, str, str]) -> SynthesisSelection:
    measure, model, outcome_key, timepoint = selection
    return SynthesisSelection(
        measure=measure, model=model, outcome_key=outcome_key, timepoint=timepoint
    )


def _split(excluded: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """(unit-level exclusions, run-level failures)."""
    run = [e for e in excluded if e["reason"] in rules.RUN_REASONS]
    return [e for e in excluded if e["reason"] not in rules.RUN_REASONS], run


# --- Rows and derived state ---------------------------------------------------------


async def _results(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(SynthesisResult)
        .where(SynthesisResult.collection_id == collection_id)
        .order_by(SynthesisResult.created_at, SynthesisResult.id),
    )


def _superseded(rows: list[Any]) -> set[Any]:
    return {r.supersedes_result_id for r in rows if r.supersedes_result_id}


def _tip(rows: list[Any], outcome_key: str, timepoint: str) -> Any:
    superseded = _superseded(rows)
    return next(
        (
            r
            for r in rows
            if (r.outcome_key, r.timepoint) == (outcome_key, timepoint)
            and r.id not in superseded
        ),
        None,
    )


def _response(
    row: Any, superseded: AbstractSet[Any], stale: AbstractSet[Any]
) -> SynthesisResultResponse:
    response = cast(
        SynthesisResultResponse, SynthesisResultResponse.model_validate(row)
    )
    return cast(
        SynthesisResultResponse,
        response.model_copy(
            update={
                "superseded": row.id in superseded,
                "stale": release_rules.node("synthesis", row.id) in stale,
            }
        ),
    )


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Edge], set[release_rules.Node]]:
    """Edges ``evidence_table -> synthesis`` and ``protocol -> synthesis``;
    ``changed`` holds superseded results and results whose protocol is no
    longer current. ``synthesis -> link`` comes from the link loop in
    ``draft_release_service._graph``, which calls this."""
    rows = await _results(db, collection_id)
    if not rows:
        return [], set()
    node = release_rules.node
    edges: list[Edge] = []
    for row in rows:
        target = node("synthesis", row.id)
        edges.append((node("evidence_table", row.table_version_id), target))
        edges.append((node("protocol", row.protocol_version_id), target))
    current = await current_protocol_version_id(db, collection_id)
    changed = {node("synthesis", value) for value in _superseded(rows)}
    changed |= {
        node("protocol", r.protocol_version_id)
        for r in rows
        if str(r.protocol_version_id) != current
    }
    return edges, changed


async def current_result(db: AsyncSession, collection_id: UUID, result_id: UUID) -> Any:
    """A computed, unsuperseded, non-stale result of this Collection (the only
    kind a claim may cite); 404 for a foreign id, 409 otherwise."""
    rows = await _results(db, collection_id)
    row = next((r for r in rows if r.id == result_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=RESULT_NOT_FOUND)
    stale = await _stale_nodes(db, collection_id)
    if (
        row.status != "computed"
        or row.id in _superseded(rows)
        or release_rules.node("synthesis", row.id) in stale
    ):
        raise _conflict(RESULT_NOT_CURRENT)
    return row


# --- Reads -----------------------------------------------------------------------


async def preview(
    db: AsyncSession,
    context: ProjectContext,
    table_version_id: UUID,
    roles: Mapping[str, Any],
) -> SynthesisPreview:
    """VIEW, zero writes: the exact included and excluded sets, run-level
    failures and ``input_hash`` that ``execute`` would use."""
    inputs = await _inputs(db, context, table_version_id, roles)
    computed = rules.compute(inputs.included, inputs.excluded, inputs.failures)
    excluded, run_failures = _split(computed["excluded"])
    tip = _tip(await _results(db, _cid(context)), *inputs.selection[2:])
    return SynthesisPreview(
        selection=_selection_response(inputs.selection),
        table_version_id=inputs.table.id,
        protocol_version_id=UUID(inputs.protocol_version_id),
        config=inputs.config,
        config_hash=inputs.config_hash,
        estimator_version=rules.ESTIMATOR_VERSION,
        included=cast(
            Any,
            [
                {
                    k: row[k]
                    for k in ("unit", "report_ids", "accepted_value_ids", "inputs")
                }
                for row in computed["included"]
            ],
        ),
        excluded=excluded,
        run_failures=run_failures,
        input_hash=inputs.input_hash,
        tip_id=None if tip is None else tip.id,
        tip_input_hash=None if tip is None else tip.input_hash,
    )


async def list_results(
    db: AsyncSession, context: ProjectContext
) -> SynthesisListResponse:
    """Every result with its stale and superseded flags, plus the protocol's
    selection (or why there is none)."""
    collection_id = _cid(context)
    selection, error = None, None
    try:
        selection = _selection_response((await _selection(db, collection_id))[1])
    except HTTPException as exc:
        error = str(exc.detail)
    rows = await _results(db, collection_id)
    stale = await _stale_nodes(db, collection_id) if rows else set()
    superseded = _superseded(rows)
    return SynthesisListResponse(
        selection=selection,
        selection_error=error,
        results=[_response(row, superseded, stale) for row in rows],
    )


async def export_package(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """``nous.academic.synthesis.v1``: every result with its inputs, so the
    numbers recompute offline (``tests/fixtures/synthesis/independent_numpy.py``)."""
    listing = await list_results(db, context)
    body = {"project_id": str(_cid(context)), **listing.model_dump(mode="json")}
    return {
        "schema": EXPORT_SCHEMA,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }


# --- Writes ----------------------------------------------------------------------


async def _append(
    db: AsyncSession,
    collection_id: UUID,
    actor_id: UUID,
    row: Any,
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
) -> UUID:
    try:
        result = await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type="synthesis.executed",
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role="reviewer",
            subject_type=SUBJECT_TYPE,
            subject_id=cast(UUID, row.id),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=None,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise _conflict("Idempotency conflict") from exc
    return cast(UUID, result.event.id)


async def execute(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: SynthesisExecute,
) -> tuple[SynthesisResultResponse, bool]:
    """REVIEW: persist one result (``computed`` or ``validation_failed``); an
    unchanged input returns the tip and writes nothing; a successor stales the
    releases that cite its predecessor. Commits once."""
    if ResearchProjectRole.REVIEWER not in context.effective_roles:
        raise HTTPException(status_code=403, detail="reviewer role required")
    collection_id = _cid(context)
    stream = await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    key = f"execute:{data.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": "execute",
            "actor_user_id": str(actor_id),
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        result_id = UUID(cast(dict[str, Any], replay.payload)["result_id"])
        row = await db.get(SynthesisResult, result_id)
        return _response(row, set(), set()), True
    inputs = await _inputs(
        db, context, data.table_version_id, data.roles.model_dump(mode="json")
    )
    if data.expected_input_hash != inputs.input_hash:
        raise _conflict(INPUTS_CHANGED)
    rows = await _results(db, collection_id)
    tip = _tip(rows, *inputs.selection[2:])
    if tip is not None and tip.input_hash == inputs.input_hash:
        return _response(tip, set(), set()), True
    if data.supersedes_result_id != (None if tip is None else tip.id):
        raise _conflict(RESULT_STALE)
    computed = rules.compute(inputs.included, inputs.excluded, inputs.failures)
    numbers = computed["numbers"] or {}
    outcome_key, timepoint = inputs.selection[2:]
    row = SynthesisResult(
        id=uuid4(),
        collection_id=collection_id,
        protocol_version_id=UUID(inputs.protocol_version_id),
        table_version_id=inputs.table.id,
        outcome_key=outcome_key,
        timepoint=timepoint,
        measure=rules.MEASURE,
        model=rules.MODEL,
        config=inputs.config,
        config_hash=inputs.config_hash,
        estimator_version=rules.ESTIMATOR_VERSION,
        software={
            "python": platform.python_version(),
            "estimator": rules.ESTIMATOR_VERSION,
        },
        status=computed["status"],
        included=computed["included"],
        excluded=computed["excluded"],
        estimate=numbers.get("estimate"),
        se=numbers.get("se"),
        ci_low=numbers.get("ci_low"),
        ci_high=numbers.get("ci_high"),
        q=numbers.get("q"),
        df=numbers.get("df"),
        tau2=numbers.get("tau2"),
        i2=numbers.get("i2"),
        input_hash=inputs.input_hash,
        result_hash=rules.result_hash(
            computed["included"], computed["excluded"], computed["numbers"]
        ),
        executed_by_id=actor_id,
        actor_role="reviewer",
        supersedes_result_id=None if tip is None else tip.id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if _is_unique_violation(error):
            raise _conflict(RESULT_STALE) from error
        raise
    payload = {
        "collection_id": str(collection_id),
        "result_id": str(row.id),
        "supersedes_result_id": None if tip is None else str(tip.id),
        "table_version_id": str(inputs.table.id),
        "protocol_version_id": inputs.protocol_version_id,
        "outcome_key": outcome_key,
        "timepoint": timepoint,
        "measure": rules.MEASURE,
        "model": rules.MODEL,
        "config_hash": inputs.config_hash,
        "estimator_version": rules.ESTIMATOR_VERSION,
        "status": computed["status"],
        "input_hash": inputs.input_hash,
        "result_hash": row.result_hash,
        "included_units": (
            [u["unit"] for u in computed["included"]]
            if computed["status"] == "computed"
            else []
        ),
        "excluded": computed["excluded"],
    }
    event_id = await _append(
        db, collection_id, actor_id, row, payload, key, fingerprint
    )
    if tip is not None:
        # Local import: draft_release_service imports this module lazily.
        from src.services.research import draft_release_service

        await draft_release_service.invalidate_dependents(
            db,
            collection_id=collection_id,
            changed={release_rules.node("synthesis", tip.id)},
            actor_id=actor_id,
            actor_role="reviewer",
            cause={
                "family": AGGREGATE_TYPE,
                "event_id": str(event_id),
                "kind": "synthesis.executed",
            },
        )
    await db.commit()
    await db.refresh(row)
    return _response(row, set(), set()), False
