"""Versioned extraction forms, observations and accepted values (GOO-304).

Lock order for writers: the route's ``resolve_project`` (Workspace SHARE ->
Collection UPDATE, roles reloaded after the lock), then this matrix's
``research_extraction`` stream ``FOR UPDATE``. The Celery worker holds
``lock_active_project`` (Collection UPDATE) then the stream - the same order
minus the Workspace SHARE.

``observe``, ``accept_value`` and ``create_version`` each commit exactly once
(``accept_value`` on a changed source commits its ``staled`` event, then 409s).
GOO-305: every value row carries a source anchor into ``content_text`` as
stored, pinned by ``text_sha256``; events are written as schema v2.
``append_machine_observations`` is the only function the worker may call to
write; it never commits and has no ``ProjectContext``, so it can never reach
``accept_value`` (which requires ``ResearchProjectRole.ADJUDICATOR``).
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.document import Document
from src.models.extraction_matrix import (
    ExtractionAcceptedValue,
    ExtractionCell,
    ExtractionFormVersion,
    ExtractionMatrix,
    ExtractionObservation,
)
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_project_role import ResearchProjectRole
from src.services.research import extraction_rules as rules
from src.services.research import source_anchors as anchors
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import (
    _replayed_event,
    _require_role,
    current_protocol_version_id,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionAcceptedValueResponse,
    ExtractionAnchor,
    ExtractionAnchorOccurrence,
    ExtractionCellObservationsResponse,
    ExtractionFormVersionResponse,
    ExtractionObservationCreate,
    ExtractionObservationResponse,
)

AGGREGATE_TYPE = "research_extraction"
SUBJECT_TYPE = "extraction_matrix"

MATRIX_NOT_FOUND = "Matrix not found"
DOCUMENT_NOT_FOUND = "Document not found"
NO_FORM_VERSION = "Matrix has no form version"
FORM_STALE = "Form version is stale"
FIELD_NOT_IN_FORM = "Field is not in this form version"
SOURCE_CHANGED = "Source changed; re-extract"
ACCEPTED_STALE = "Accepted value is stale; reload"
CITATIONS_OTHER_CELL = "Cited observations must belong to this cell"
FORM_CHANGED = "Form version changed; reload"
ANCHOR_AMBIGUOUS = "Anchor ambiguous; choose an occurrence"
ANCHOR_UNVERIFIED = "Anchor unverified; confirm to accept"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUS_RANK = {"verified": 0, "ambiguous": 1, "unverified": 2}


def document_source_hash(document: Any) -> str:
    """The document's content version: its checksum, else sha256 of its text."""
    checksum = document.checksum_sha256
    if isinstance(checksum, str) and _SHA256_RE.fullmatch(checksum.lower()):
        return checksum.lower()
    text = document.content_text or ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_pins(document: Any) -> tuple[str, str]:
    """(source_hash, text_sha256): offsets are valid only against this text."""
    text_hash = anchors.text_sha256(document.content_text or "")
    assert text_hash is not None
    return document_source_hash(document), text_hash


def _changed(pins: tuple[str, str], source_hash: str, text_hash: str | None) -> bool:
    """A NULL text hash (pre-anchor row) compares on the source hash only."""
    return source_hash != pins[0] or (text_hash is not None and text_hash != pins[1])


def _sha(text: str | None) -> str | None:
    return None if text is None else anchors.text_sha256(text)


def _anchor_columns(anchor: anchors.Anchor | None) -> dict[str, Any]:
    if anchor is None:
        return {}
    return {
        "anchor_status": anchor.status,
        "anchor_start_char": anchor.start_char,
        "anchor_end_char": anchor.end_char,
        "anchor_page": anchor.page,
        "anchor_occurrences": list(anchor.occurrences),
        "occurrences_in_text": anchor.occurrences_in_text,
    }


def _anchor_event(row: Any) -> dict[str, Any]:
    """The per-observation v2 event object; the quote is bound by hash."""
    return {
        "field_id": str(row.field_id),
        "anchor_status": row.anchor_status,
        "citation_sha256": _sha(row.citation),
        "start": row.anchor_start_char,
        "end": row.anchor_end_char,
        "page": row.anchor_page,
        "occurrences": list(row.anchor_occurrences or []),
    }


def _unprocessable(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def _project(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


async def _lock(
    db: AsyncSession, collection_id: UUID, matrix_id: UUID
) -> ResearchDecisionStream:
    return await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=matrix_id,
    )


async def _append(
    db: AsyncSession,
    *,
    collection_id: UUID,
    matrix_id: UUID,
    event_type: str,
    actor_user_id: UUID,
    actor_role: str,
    reason: str | None,
    payload: dict[str, Any],
    idempotency_key: str,
    fingerprint: str,
    version: int = 1,
) -> None:
    """Append one event; a reused key with other content is a 409."""
    payload = {
        "collection_id": str(collection_id),
        "matrix_id": str(matrix_id),
        **payload,
    }
    try:
        await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=matrix_id,
            event_type=event_type,
            event_schema_version=version,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=matrix_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


async def current_version(db: AsyncSession, matrix_id: UUID) -> Any:
    """The highest version_no, or None (only raw-SQL fixtures lack v1)."""
    return (
        await db.execute(
            select(ExtractionFormVersion)
            .where(ExtractionFormVersion.matrix_id == matrix_id)
            .order_by(ExtractionFormVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def matrix_project_id(db: AsyncSession, matrix_id: UUID) -> UUID:
    """The live matrix's Collection id (for ``resolve_project``), else 404."""
    project_id = (
        await db.execute(
            select(ExtractionMatrix.project_id).where(
                ExtractionMatrix.id == matrix_id,
                ExtractionMatrix.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail=MATRIX_NOT_FOUND)
    return cast(UUID, project_id)


async def _matrix(db: AsyncSession, context: ProjectContext, matrix_id: UUID) -> Any:
    matrix = (
        await db.execute(
            select(ExtractionMatrix).where(
                ExtractionMatrix.id == matrix_id,
                ExtractionMatrix.project_id == _project(context),
                ExtractionMatrix.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if matrix is None:
        raise HTTPException(status_code=404, detail=MATRIX_NOT_FOUND)
    return matrix


async def _versions(db: AsyncSession, matrix_id: UUID) -> list[Any]:
    return list(
        (
            await db.execute(
                select(ExtractionFormVersion)
                .where(ExtractionFormVersion.matrix_id == matrix_id)
                .order_by(ExtractionFormVersion.version_no)
            )
        )
        .scalars()
        .all()
    )


def _is_tip() -> Any:
    newer = aliased(ExtractionAcceptedValue)
    return ~exists().where(
        newer.supersedes_accepted_value_id == ExtractionAcceptedValue.id
    )


async def _tips(db: AsyncSession, version_ids: Sequence[UUID]) -> list[Any]:
    return list(
        (
            await db.execute(
                select(ExtractionAcceptedValue).where(
                    ExtractionAcceptedValue.form_version_id.in_(version_ids),
                    _is_tip(),
                )
            )
        )
        .scalars()
        .all()
    )


async def _staled_ids(db: AsyncSession, stream: ResearchDecisionStream) -> set[str]:
    staled: set[str] = set()
    for payload in (
        await db.execute(
            select(ResearchDecisionEvent.payload).where(
                ResearchDecisionEvent.stream_id == stream.id,
                ResearchDecisionEvent.event_type == "extraction.staled",
            )
        )
    ).scalars():
        staled.update(payload["accepted_value_ids"])
    return staled


async def create_version(
    db: AsyncSession,
    context: ProjectContext,
    matrix: Any,
    columns: Sequence[Mapping[str, Any]],
    actor_id: UUID,
) -> Any:
    """Append an authored form version (EDIT; the route resolved it).

    Identical content (same hash as the current version) creates nothing.
    ``matrix.columns`` mirrors the current version. Accepted tips whose field
    definition changed (or whose field was removed) are named in one
    ``extraction.staled`` event; no row is rewritten. Commits once, so a
    pending ``matrix`` (new, or renamed by the route) lands with it.
    """
    try:
        fields = rules.build_fields(cast(UUID, matrix.id), columns)
    except ValueError as error:
        raise _unprocessable(error) from error
    protocol = await current_protocol_version_id(db, _project(context))
    protocol_version_id = UUID(protocol) if protocol else None
    content_hash = rules.form_hash("authored", protocol_version_id, fields)
    await db.flush()  # a new matrix row must exist before its stream and FK
    stream = await _lock(db, _project(context), matrix.id)
    current = await current_version(db, matrix.id)
    if current is not None and current.content_hash == content_hash:
        await db.commit()
        return current
    version = ExtractionFormVersion(
        id=uuid4(),
        matrix_id=matrix.id,
        version_no=(0 if current is None else current.version_no) + 1,
        provenance="authored",
        fields=fields,
        protocol_version_id=protocol_version_id,
        content_hash=content_hash,
        created_by_id=actor_id,
    )
    db.add(version)
    matrix.columns = [
        {"name": f["name"], "description": f["description"]} for f in fields
    ]
    await _flush_or_conflict(db, FORM_CHANGED)
    versions = {cast(UUID, v.id): v for v in await _versions(db, matrix.id)}
    already = await _staled_ids(db, stream)
    staled = sorted(
        str(tip.id)
        for tip in await _tips(db, [v for v in versions if v != version.id])
        if str(tip.id) not in already
        and rules.field_def(versions[tip.form_version_id].fields, tip.field_id)
        != rules.field_def(fields, tip.field_id)
    )
    if staled:
        payload = {
            "new_form_version_id": str(version.id),
            "accepted_value_ids": staled,
            "reason": "form_changed",
            "document_id": None,
            "new_source_hash": None,
            "new_text_sha256": None,
        }
        await _append(
            db,
            collection_id=_project(context),
            matrix_id=matrix.id,
            event_type="extraction.staled",
            actor_user_id=actor_id,
            actor_role="editor",
            reason=None,
            payload=payload,
            idempotency_key=f"staled:{version.id}",
            fingerprint=decision_request_fingerprint(payload),
            version=2,
        )
    await db.commit()
    await db.refresh(version)
    return version


async def removed_field_document_ids(
    db: AsyncSession, matrix: Any, removed_names: Sequence[str]
) -> list[str]:
    """Documents holding a value (frozen cell or observation) on a removed field.

    Read-only: nothing is soft-deleted; prior values stay readable.
    """
    if not removed_names:
        return []
    field_ids = [rules.field_id(matrix.id, name) for name in removed_names]
    cells = select(ExtractionCell.document_id).where(
        ExtractionCell.matrix_id == matrix.id,
        ExtractionCell.column_name.in_(removed_names),
        ExtractionCell.is_deleted.is_(False),
    )
    observed = select(ExtractionObservation.document_id).where(
        ExtractionObservation.field_id.in_(field_ids)
    )
    rows = (await db.execute(cells.union(observed))).scalars().all()
    return sorted(str(document_id) for document_id in rows)


async def _flush_or_conflict(db: AsyncSession, detail: str) -> None:
    """A unique-index hit is a stable 409 after rollback, never a 500."""
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_unique_violation(error):
            raise
        raise HTTPException(status_code=409, detail=detail) from error


async def extraction_task_kwargs(
    db: AsyncSession,
    matrix: Any,
    document_ids: Sequence[UUID],
    actor_id: UUID,
    task_id: str,
) -> dict[str, Any]:
    """Pin the current form version, each document's source hash and the
    initiating user into the Celery payload."""
    version = await current_version(db, matrix.id)
    if version is None:
        raise HTTPException(status_code=409, detail=NO_FORM_VERSION)
    documents = (
        (
            await db.execute(
                project_documents_query(matrix.project_id).where(
                    Document.id.in_(document_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "matrix_id": str(matrix.id),
        "document_ids": [str(document_id) for document_id in document_ids],
        "columns": matrix.columns,  # ignored by the worker; kept for old signatures
        "task_id": task_id,
        "form_version_id": str(version.id),
        "initiated_by_user_id": str(actor_id),
        "source_hashes": {str(d.id): document_source_hash(d) for d in documents},
    }


async def _stale_changed_source(
    db: AsyncSession,
    stream: ResearchDecisionStream,
    *,
    matrix: Any,
    document: Any,
    actor_id: UUID,
    actor_role: str,
) -> list[str]:
    """Name this document's accepted tips pinned to an older source in one
    ``extraction.staled`` v2 event (``source_changed``). Rows are never
    updated; tips staled before are skipped. The caller holds the stream."""
    pins = document_pins(document)
    versions = [cast(UUID, v.id) for v in await _versions(db, matrix.id)]
    already = await _staled_ids(db, stream)
    staled = sorted(
        str(tip.id)
        for tip in await _tips(db, versions)
        if tip.document_id == document.id
        and str(tip.id) not in already
        and _changed(pins, tip.source_hash, tip.text_sha256)
    )
    if staled:
        payload = {
            "new_form_version_id": None,
            "accepted_value_ids": staled,
            "reason": "source_changed",
            "document_id": str(document.id),
            "new_source_hash": pins[0],
            "new_text_sha256": pins[1],
        }
        await _append(
            db,
            collection_id=matrix.project_id,
            matrix_id=matrix.id,
            event_type="extraction.staled",
            actor_user_id=actor_id,
            actor_role=actor_role,
            reason=None,
            payload=payload,
            idempotency_key=f"staled:source:{uuid4()}",
            fingerprint=decision_request_fingerprint(payload),
            version=2,
        )
    return staled


async def append_machine_observations(
    db: AsyncSession,
    *,
    matrix: Any,
    version: Any,
    document: Any,
    observations: Mapping[str, Sequence[anchors.Candidate]],
    coverage: Sequence[tuple[int, int]],
    actor_id: UUID,
    run_id: str,
    model: str,
) -> None:
    """Worker-only: the anchored machine rows of one document plus one
    ``extraction.observed`` v2 event, after staling this document's accepted
    tips pinned to an older source. The caller holds ``lock_active_project``
    and commits. ``observations`` maps field_id -> aggregated candidates
    (``source_anchors.read_whole_text``); a field it lacks is recorded as
    ``extraction_error``.
    """
    stream_key = f"{run_id}:{document.id}"
    source_hash, text_hash = document_pins(document)
    text_length = len(document.content_text or "")
    inspected = [[lo, hi] for lo, hi in coverage]
    stream = await _lock(db, matrix.project_id, matrix.id)
    await _stale_changed_source(
        db,
        stream,
        matrix=matrix,
        document=document,
        actor_id=actor_id,
        actor_role="machine",
    )
    events: dict[str, dict[str, Any]] = {}
    for field in version.fields:
        found = observations.get(field["field_id"]) or [
            anchors.Candidate(
                field["field_id"], None, "extraction_error", "valid", None, None
            )
        ]
        for candidate in found:
            row = ExtractionObservation(
                id=uuid4(),
                form_version_id=version.id,
                field_id=UUID(field["field_id"]),
                document_id=document.id,
                kind="machine",
                actor_user_id=actor_id,
                extractor_run_id=run_id,
                extractor_model=model,
                value=candidate.value,
                missingness=candidate.missingness,
                validation_state=candidate.validation_state,
                citation=candidate.citation,
                source_hash=source_hash,
                text_sha256=text_hash,
                text_length=text_length,
                inspected_coverage=inspected,
                **_anchor_columns(candidate.anchor),
            )
            db.add(row)
            events[str(row.id)] = _anchor_event(row)
    await db.flush()
    # A retry reusing this key carries new row ids, so append_decision rejects
    # it as an idempotency conflict and the caller rolls the rows back.
    request = {"run_id": run_id, "document_id": str(document.id)}
    await _append(
        db,
        collection_id=matrix.project_id,
        matrix_id=matrix.id,
        event_type="extraction.observed",
        actor_user_id=actor_id,
        actor_role="machine",
        reason=None,
        payload={
            "form_version_id": str(version.id),
            "form_content_hash": version.content_hash,
            "document_id": str(document.id),
            "source_hash": source_hash,
            "kind": "machine",
            "observations": events,
            "extractor_run_id": run_id,
            "extractor_model": model,
            "text_sha256": text_hash,
            "inspected_coverage": inspected,
        },
        idempotency_key=stream_key,
        fingerprint=decision_request_fingerprint(request),
        version=2,
    )


async def _document(
    db: AsyncSession, context: ProjectContext, document_id: UUID
) -> Any:
    document = (
        await db.execute(
            project_documents_query(_project(context)).where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail=DOCUMENT_NOT_FOUND)
    return document


async def _current_field(
    db: AsyncSession, matrix_id: UUID, form_version_id: UUID, field_id: UUID
) -> tuple[Any, Mapping[str, Any]]:
    version = await current_version(db, matrix_id)
    if version is None:
        raise HTTPException(status_code=409, detail=NO_FORM_VERSION)
    if version.id != form_version_id:
        raise HTTPException(status_code=409, detail=FORM_STALE)
    field = rules.find_field(version.fields, field_id)
    if field is None:
        raise HTTPException(status_code=422, detail=FIELD_NOT_IN_FORM)
    return version, field


def _fingerprint(operation: str, matrix_id: UUID, actor_id: UUID, data: Any) -> str:
    return decision_request_fingerprint(
        {
            "operation": operation,
            "matrix_id": str(matrix_id),
            "actor_user_id": str(actor_id),
            "request": data.model_dump(mode="json"),
        }
    )


def _anchor(row: Any, text: str | None) -> ExtractionAnchor | None:
    """Pre-anchor value rows read as ``unverified``; missingness has none.
    ``text`` (the current text, only while unchanged) adds occurrence context."""
    if row.missingness is not None:
        return None
    occurrences = list(row.anchor_occurrences or [])
    contexts = []
    if text and row.citation and row.anchor_status == "ambiguous":
        for start in occurrences:
            end = start + len(row.citation)
            before, after = anchors.context(text, start, end, radius=120)
            contexts.append(
                ExtractionAnchorOccurrence(
                    start_char=start,
                    end_char=end,
                    page=anchors.page_at(text, start),
                    context_before=before,
                    context_after=after,
                )
            )
    return ExtractionAnchor(
        status=row.anchor_status or "unverified",
        start_char=row.anchor_start_char,
        end_char=row.anchor_end_char,
        page=row.anchor_page,
        occurrences=occurrences,
        occurrences_in_text=row.occurrences_in_text,
        occurrence_contexts=contexts,
    )


def _observation(row: Any, document: Any = None) -> ExtractionObservationResponse:
    """Evidence view of one row; context is sliced from the current text only
    while the pinned source still matches (``document`` given)."""
    response: ExtractionObservationResponse = (
        ExtractionObservationResponse.model_validate(row)
    )
    changed = document is not None and _changed(
        document_pins(document), row.source_hash, row.text_sha256
    )
    text = None if document is None or changed else document.content_text
    before = after = None
    if text and row.anchor_start_char is not None:
        before, after = anchors.context(
            text, row.anchor_start_char, row.anchor_end_char
        )
    coverage = row.inspected_coverage
    return response.model_copy(
        update={
            "anchor": _anchor(row, text),
            "context_before": before,
            "context_after": after,
            "coverage_complete": (
                None
                if coverage is None or row.text_length is None
                else coverage == [[0, row.text_length]]
            ),
            "source_changed": changed,
        }
    )


def _accepted(row: Any, document: Any = None) -> ExtractionAcceptedValueResponse:
    response: ExtractionAcceptedValueResponse = (
        ExtractionAcceptedValueResponse.model_validate(row)
    )
    return response.model_copy(
        update={
            "anchor_resolution": row.anchor_resolution or "legacy",
            "source_changed": document is not None
            and _changed(document_pins(document), row.source_hash, row.text_sha256),
        }
    )


async def observe(
    db: AsyncSession,
    context: ProjectContext,
    matrix_id: UUID,
    actor_id: UUID,
    data: ExtractionObservationCreate,
) -> ExtractionObservationResponse:
    """A reviewer's own observation (REVIEW). Never overwrites anything."""
    _require_role(context, ResearchProjectRole.REVIEWER)
    matrix = await _matrix(db, context, matrix_id)
    stream = await _lock(db, _project(context), matrix_id)
    key = f"observe:{data.idempotency_key}"
    fingerprint = _fingerprint("observe", matrix_id, actor_id, data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        (observation_id,) = cast(dict[str, Any], replay.payload)["observations"]
        row = await db.get(ExtractionObservation, UUID(observation_id))
        return _observation(row)
    document = await _document(db, context, data.document_id)
    version, field = await _current_field(
        db, matrix_id, data.form_version_id, data.field_id
    )
    try:
        value, missingness, state = rules.normalize(
            "human", field, data.value, data.missingness
        )
    except ValueError as error:
        raise _unprocessable(error) from error
    source_hash, text_hash = document_pins(document)
    # A person sees the whole document: the quote must be unique in it or
    # carry a start. Ambiguous is stored; the block happens at accept.
    anchor = (
        None
        if missingness is not None
        else anchors.verify_anchor(
            document.content_text, data.citation, start_hint=data.anchor_start
        )
    )
    row = ExtractionObservation(
        id=uuid4(),
        form_version_id=version.id,
        field_id=data.field_id,
        document_id=document.id,
        kind="human",
        actor_user_id=actor_id,
        value=value,
        missingness=missingness,
        validation_state=state,
        citation=data.citation,
        source_hash=source_hash,
        text_sha256=text_hash,
        text_length=len(document.content_text or ""),
        **_anchor_columns(anchor),
    )
    db.add(row)
    await db.flush()
    await _append(
        db,
        collection_id=_project(context),
        matrix_id=matrix.id,
        event_type="extraction.observed",
        actor_user_id=actor_id,
        actor_role="reviewer",
        reason=None,
        payload={
            "form_version_id": str(version.id),
            "form_content_hash": version.content_hash,
            "document_id": str(document.id),
            "source_hash": source_hash,
            "kind": "human",
            "observations": {str(row.id): _anchor_event(row)},
            "extractor_run_id": None,
            "extractor_model": None,
            "text_sha256": text_hash,
            "inspected_coverage": None,
        },
        idempotency_key=key,
        fingerprint=fingerprint,
        version=2,
    )
    await db.commit()
    await db.refresh(row)
    return _observation(row, document)


def _rule_obs(row: Any) -> rules.Obs:
    return rules.Obs(
        id=row.id,
        kind=row.kind,
        value=row.value,
        missingness=row.missingness,
        validation_state=row.validation_state,
        form_version_id=row.form_version_id,
        citation=row.citation,
        source_hash=row.source_hash,
        created_at=row.created_at,
    )


@dataclass(frozen=True)
class AnchorResolution:
    resolution: str
    observation_id: UUID | None
    start: int | None
    end: int | None


@dataclass(frozen=True)
class SourceChanged:
    staled: list[str]


async def assert_anchor_acceptable(
    db: AsyncSession,
    context: ProjectContext,
    stream: ResearchDecisionStream,
    matrix: Any,
    document: Any,
    cited: Sequence[Any],
    data: ExtractionAcceptCreate,
    actor_id: UUID,
) -> AnchorResolution | SourceChanged:
    """GOO-304 ``accept_value`` step 7, re-checked at decision time.

    ``document`` was re-fetched through ``project_documents_query`` after the
    project lock. In order: a source that changed since any cited observation
    stales this document's tips and returns ``SourceChanged``; then the anchor
    observation (the best-anchored cited row equal to the value) is re-verified
    on the current text and resolved, or refused with a stable 409.
    """
    pins = document_pins(document)
    if any(_changed(pins, row.source_hash, row.text_sha256) for row in cited):
        staled = await _stale_changed_source(
            db,
            stream,
            matrix=matrix,
            document=document,
            actor_id=actor_id,
            actor_role="adjudicator",
        )
        return SourceChanged(staled)
    if data.missingness is not None:
        return AnchorResolution("not_applicable", None, None, None)
    matching = [
        row
        for row in cited
        if row.validation_state == "valid"
        and row.missingness is None
        and rules._same(row.value, data.value)
    ]
    row = min(
        matching,
        key=lambda r: (
            _STATUS_RANK.get(r.anchor_status or "unverified", 3),
            r.created_at,
            str(r.id),
        ),
    )
    status = row.anchor_status or "unverified"
    start = data.anchor_start
    if status == "ambiguous":
        # Only a recorded occurrence that still re-verifies resolves it.
        anchor = anchors.verify_anchor(
            document.content_text, row.citation, start_hint=start
        )
        if start not in (row.anchor_occurrences or []) or anchor.status != "verified":
            raise HTTPException(status_code=409, detail=ANCHOR_AMBIGUOUS)
        return AnchorResolution(
            "disambiguated", row.id, anchor.start_char, anchor.end_char
        )
    anchor = anchors.verify_anchor(
        document.content_text,
        row.citation,
        start_hint=row.anchor_start_char if start is None else start,
    )
    if status == "verified" and anchor.status == "verified":
        return AnchorResolution("verified", row.id, anchor.start_char, anchor.end_char)
    if not data.accept_unverified:
        raise HTTPException(status_code=409, detail=ANCHOR_UNVERIFIED)
    return AnchorResolution("accepted_unverified", row.id, None, None)


async def accept_value(
    db: AsyncSession,
    context: ProjectContext,
    matrix_id: UUID,
    actor_id: UUID,
    data: ExtractionAcceptCreate,
) -> ExtractionAcceptedValueResponse:
    """Record the accepted value for one cell (ADJUDICATE only).

    The value must equal one valid cited observation of this exact cell, form
    version and current source, or be ``unresolved_disagreement`` over >=2
    that differ. ``supersedes_accepted_value_id`` must name the current tip.
    Its anchor must be verified, disambiguated by ``anchor_start``, or
    accepted unverified on request (GOO-305). On a changed source this is the
    only commit-then-raise path: the ``staled`` event commits, then 409.
    """
    if ResearchProjectRole.ADJUDICATOR not in context.effective_roles:
        raise HTTPException(status_code=403, detail="adjudicator role required")
    matrix = await _matrix(db, context, matrix_id)
    stream = await _lock(db, _project(context), matrix_id)
    key = f"accept:{data.idempotency_key}"
    fingerprint = _fingerprint("accept", matrix_id, actor_id, data)
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        accepted_id = cast(dict[str, Any], replay.payload)["accepted_value_id"]
        row = await db.get(ExtractionAcceptedValue, UUID(accepted_id))
        return _accepted(row)
    document = await _document(db, context, data.document_id)
    version, field = await _current_field(
        db, matrix_id, data.form_version_id, data.field_id
    )
    # --- Acceptance checks, in order. ---
    tip = (
        await db.execute(
            select(ExtractionAcceptedValue.id).where(
                ExtractionAcceptedValue.document_id == document.id,
                ExtractionAcceptedValue.field_id == data.field_id,
                _is_tip(),
            )
        )
    ).scalar_one_or_none()
    if data.supersedes_accepted_value_id != tip:
        raise HTTPException(status_code=409, detail=ACCEPTED_STALE)
    if len(set(data.observation_ids)) != len(data.observation_ids):
        raise HTTPException(status_code=422, detail="observation_ids must be unique")
    cited = list(
        (
            await db.execute(
                select(ExtractionObservation).where(
                    ExtractionObservation.id.in_(data.observation_ids),
                    ExtractionObservation.document_id == document.id,
                    ExtractionObservation.field_id == data.field_id,
                    ExtractionObservation.form_version_id == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(cited) != len(data.observation_ids):
        raise HTTPException(status_code=422, detail=CITATIONS_OTHER_CELL)
    source_hash, text_hash = document_pins(document)
    try:
        value, missingness = data.value, data.missingness
        if value is not None:
            value, _, _ = rules.normalize("accepted", field, value, missingness)
        rules.check_acceptance(value, missingness, [_rule_obs(r) for r in cited])
    except ValueError as error:
        raise _unprocessable(error) from error
    guard = await assert_anchor_acceptable(
        db,
        context,
        stream,
        matrix,
        document,
        cited,
        data.model_copy(update={"value": value}),
        actor_id,
    )
    if isinstance(guard, SourceChanged):
        await db.commit()  # persist the staled event, then refuse
        raise HTTPException(status_code=409, detail=SOURCE_CHANGED)
    # --- end of acceptance checks ---
    row = ExtractionAcceptedValue(
        id=uuid4(),
        form_version_id=version.id,
        field_id=data.field_id,
        document_id=document.id,
        value=value,
        missingness=missingness,
        observation_ids=[str(i) for i in data.observation_ids],
        accepted_by_id=actor_id,
        rationale=data.rationale,
        source_hash=source_hash,
        supersedes_accepted_value_id=data.supersedes_accepted_value_id,
        anchor_observation_id=guard.observation_id,
        anchor_resolution=guard.resolution,
        anchor_start_char=guard.start,
        anchor_end_char=guard.end,
        text_sha256=text_hash,
    )
    db.add(row)
    await _flush_or_conflict(db, ACCEPTED_STALE)
    await _append(
        db,
        collection_id=_project(context),
        matrix_id=matrix.id,
        event_type="extraction.accepted",
        actor_user_id=actor_id,
        actor_role="adjudicator",
        reason=data.rationale,
        payload={
            "accepted_value_id": str(row.id),
            "form_version_id": str(version.id),
            "document_id": str(document.id),
            "field_id": str(data.field_id),
            "observation_ids": [str(i) for i in data.observation_ids],
            "value": value,
            "missingness": missingness,
            "supersedes_accepted_value_id": (
                None
                if data.supersedes_accepted_value_id is None
                else str(data.supersedes_accepted_value_id)
            ),
            "source_hash": source_hash,
            "anchor_observation_id": (
                None if guard.observation_id is None else str(guard.observation_id)
            ),
            "anchor_resolution": guard.resolution,
            "anchor_start_char": guard.start,
            "text_sha256": text_hash,
        },
        idempotency_key=key,
        fingerprint=fingerprint,
        version=2,
    )
    await db.commit()
    await db.refresh(row)
    return _accepted(row)


async def list_observations(
    db: AsyncSession,
    context: ProjectContext,
    matrix_id: UUID,
    document_id: UUID,
    field_id: UUID,
) -> ExtractionCellObservationsResponse:
    """Every observation (any version) and the accepted chain for one cell (VIEW).

    The evidence fetch re-checks the document through ``project_documents_query``
    (404, no citation), and reads its text once for context and staleness.
    """
    await _matrix(db, context, matrix_id)
    document = await _document(db, context, document_id)
    version_ids = select(ExtractionFormVersion.id).where(
        ExtractionFormVersion.matrix_id == matrix_id
    )
    observations = (
        (
            await db.execute(
                select(ExtractionObservation)
                .where(
                    ExtractionObservation.form_version_id.in_(version_ids),
                    ExtractionObservation.document_id == document_id,
                    ExtractionObservation.field_id == field_id,
                )
                .order_by(ExtractionObservation.created_at, ExtractionObservation.id)
            )
        )
        .scalars()
        .all()
    )
    chain = (
        (
            await db.execute(
                select(ExtractionAcceptedValue)
                .where(
                    ExtractionAcceptedValue.form_version_id.in_(version_ids),
                    ExtractionAcceptedValue.document_id == document_id,
                    ExtractionAcceptedValue.field_id == field_id,
                )
                .order_by(
                    ExtractionAcceptedValue.created_at, ExtractionAcceptedValue.id
                )
            )
        )
        .scalars()
        .all()
    )
    return ExtractionCellObservationsResponse(
        observations=[_observation(o, document) for o in observations],
        accepted_chain=[_accepted(a, document) for a in chain],
    )


async def list_versions(
    db: AsyncSession, context: ProjectContext, matrix_id: UUID
) -> list[ExtractionFormVersionResponse]:
    await _matrix(db, context, matrix_id)
    return [
        ExtractionFormVersionResponse.model_validate(v)
        for v in await _versions(db, matrix_id)
    ]


def _summary(version: Any) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "fields": version.fields,
        "version_no": version.version_no,
        "provenance": version.provenance,
        "content_hash": version.content_hash,
        "protocol_version_id": (
            None
            if version.protocol_version_id is None
            else str(version.protocol_version_id)
        ),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _cell(
    document_id: Any,
    name: str,
    field_id: str | None,
    view: Any,
    anchor_status: str | None = None,
) -> dict:
    if view.source == "legacy":
        anchor_status = "legacy_unanchored"  # never stored (GOO-305)
    return {
        "document_id": str(document_id),
        "column_name": name,
        "value": view.value,
        "citation_snippet": view.citation_snippet,
        "confidence": view.confidence,
        "field_id": field_id,
        "form_version_id": (
            None if view.form_version_id is None else str(view.form_version_id)
        ),
        "source": view.source,
        "missingness": view.missingness,
        "validation_state": view.validation_state,
        "stale": view.stale,
        "anchor_status": anchor_status,
        # A legacy confidence was a constant, never measured.
        "confidence_calibration": (None if view.confidence is None else "uncalibrated"),
    }


async def cell_view(
    db: AsyncSession, matrix: Any, allowed_document_ids: Sequence[UUID]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Grid cells for the current fields: accepted tip -> latest machine
    observation of the field in any version -> frozen legacy cell."""
    allowed = list(allowed_document_ids)
    legacy_rows: list[Any] = list(
        (
            await db.execute(
                select(ExtractionCell).where(
                    ExtractionCell.matrix_id == matrix.id,
                    ExtractionCell.is_deleted.is_(False),
                    ExtractionCell.document_id.in_(allowed),
                )
            )
        )
        .scalars()
        .all()
    )
    versions = await _versions(db, matrix.id)
    if not versions:
        names = [c["name"] for c in matrix.columns]
        return None, [
            _cell(
                cell.document_id,
                cell.column_name,
                None,
                rules.pick_cell(
                    [],
                    [],
                    rules.LegacyCell(
                        cell.value, cell.citation_snippet, cell.confidence, None
                    ),
                    None,
                    "",
                ),
            )
            for cell in legacy_rows
            if cell.column_name in names
        ]
    current = versions[-1]
    by_id = {cast(UUID, v.id): v for v in versions}
    legacy_version = (
        versions[0].id if versions[0].provenance == "legacy_unversioned" else None
    )
    legacy: dict[tuple[Any, str], Any] = {
        (c.document_id, c.column_name): c for c in legacy_rows
    }
    field_ids = [UUID(f["field_id"]) for f in current.fields]
    machine: dict[tuple[Any, str], list[rules.Obs]] = {}
    status: dict[tuple[Any, str], str | None] = {}  # latest machine / tip anchor
    values: dict[tuple[Any, str], set[str]] = {}  # distinct observed values
    for row in (
        await db.execute(
            select(ExtractionObservation)
            .where(
                ExtractionObservation.form_version_id.in_(list(by_id)),
                ExtractionObservation.document_id.in_(allowed),
                ExtractionObservation.field_id.in_(field_ids),
            )
            .order_by(ExtractionObservation.created_at, ExtractionObservation.id)
        )
    ).scalars():
        key = (row.document_id, str(row.field_id))
        if row.missingness is None:
            values.setdefault(key, set()).add(rules.display(row.value) or "")
        if row.kind != "machine":
            continue
        machine.setdefault(key, []).append(_rule_obs(row))
        status[key] = None if row.missingness else row.anchor_status or "unverified"
    # ponytail: loads text to hash it; add a stored text hash if that ever
    # gets slow.
    pins: dict[Any, tuple[str, str]] = {
        document.id: document_pins(document)
        for document in (
            await db.execute(select(Document).where(Document.id.in_(allowed)))
        ).scalars()
    }
    hashes = {document_id: pin[0] for document_id, pin in pins.items()}
    accepted: dict[tuple[Any, str], list[rules.Accepted]] = {}
    for tip in await _tips(db, list(by_id)):
        if tip.document_id in allowed:
            key = (tip.document_id, str(tip.field_id))
            pin = pins.get(tip.document_id, ("", ""))
            accepted.setdefault(key, []).append(
                rules.Accepted(
                    id=tip.id,
                    value=tip.value,
                    missingness=tip.missingness,
                    form_version_id=tip.form_version_id,
                    # A changed text is a changed source (GOO-305): "" never
                    # equals a current hash, so the tip reads stale.
                    source_hash=(
                        ""
                        if _changed(pin, tip.source_hash, tip.text_sha256)
                        else tip.source_hash
                    ),
                    field_def=rules.field_def(
                        by_id[tip.form_version_id].fields, tip.field_id
                    ),
                )
            )
            status[key] = tip.anchor_resolution or "legacy"
    cells = []
    for document_id in allowed:
        for field in current.fields:
            key = (document_id, field["field_id"])
            cell = legacy.get((document_id, field["name"]))
            view = rules.pick_cell(
                accepted.get(key, []),
                machine.get(key, []),
                (
                    None
                    if cell is None
                    else rules.LegacyCell(
                        cell.value,
                        cell.citation_snippet,
                        cell.confidence,
                        legacy_version,
                    )
                ),
                rules.field_def(current.fields, field["field_id"]),
                hashes.get(document_id, ""),
            )
            if view is not None:
                cells.append(
                    _cell(
                        document_id,
                        field["name"],
                        field["field_id"],
                        view,
                        status.get(key),
                    )
                    # Distinct observed values: >1 is a disagreement badge.
                    | {"observed_values": len(values.get(key, ()))}
                )
    return _summary(current), cells
