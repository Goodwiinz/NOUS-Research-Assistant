"""Per-report full-text acquisition: request, attempts, state (GOO-303).

Every function runs inside the caller's transaction and never commits. Writers
arrive with an EDIT ``ProjectContext`` (Workspace SHARE -> Collection UPDATE),
then take the per-Collection ``research_acquisition`` stream lock, replay an
identical retry, validate, insert and append exactly one ledger event.
Acquisition is not eligibility: an ``unavailable`` attempt never excludes a
report, and only a ``retrieved`` attempt (pinned to a project Document's
content hash) lets a report be screened at full text.
"""

from typing import Any, Iterable, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.document import Document
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_fulltext import (
    ResearchFulltextAttempt,
    ResearchFulltextRequest,
)
from src.models.research_report import ResearchReport
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextAttemptResponse,
    FulltextRequestCreate,
    FulltextStateResponse,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import (
    _replayed_event,
    current_protocol_version_id,
    live_reports,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)

AGGREGATE_TYPE = "research_acquisition"
SUBJECT_TYPE = "research_report"

REQUEST_NOT_FOUND = "Full text request not found"
ALREADY_REQUESTED = "Full text already requested"
ATTEMPT_STALE = "Attempt is stale; reload acquisition state"
ALREADY_RETRIEVED = "Full text already retrieved"
DOCUMENT_NOT_FOUND = "Document not found"
DOCUMENT_UNHASHED = "Document has no content hash yet"
_EVENT_TYPES = {
    "requested": "acquisition.attempted",
    "unavailable": "acquisition.unavailable",
    "retrieved": "acquisition.retrieved",
}


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def _lock(db: AsyncSession, collection_id: UUID) -> ResearchDecisionStream:
    return await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )


async def _append(
    db: AsyncSession,
    collection_id: UUID,
    *,
    event_type: str,
    actor_user_id: UUID,
    report_id: UUID,
    payload: dict[str, Any],
    idempotency_key: str,
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
            actor_user_id=actor_user_id,
            actor_role="editor",
            subject_type=SUBJECT_TYPE,
            subject_id=report_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=None,
            payload=payload,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise _conflict("Idempotency conflict") from exc


def _is_head() -> Any:
    """No later attempt follows this one: it is its request's current head."""
    later = aliased(ResearchFulltextAttempt)
    return ~exists().where(later.previous_attempt_id == ResearchFulltextAttempt.id)


async def _states(
    db: AsyncSession, collection_id: UUID, request_ids: Iterable[UUID] | None = None
) -> list[FulltextStateResponse]:
    query = select(ResearchFulltextRequest).where(
        ResearchFulltextRequest.collection_id == collection_id
    )
    if request_ids is not None:
        query = query.where(ResearchFulltextRequest.id.in_(list(request_ids)))
    # Any: legacy Column attributes (see identity_service.live_reports).
    requests: Sequence[Any] = (
        (
            await db.execute(
                query.order_by(
                    ResearchFulltextRequest.requested_at, ResearchFulltextRequest.id
                )
            )
        )
        .scalars()
        .all()
    )
    attempts: Sequence[Any] = (
        (
            await db.execute(
                select(ResearchFulltextAttempt).where(
                    ResearchFulltextAttempt.request_id.in_([r.id for r in requests])
                )
            )
        )
        .scalars()
        .all()
    )
    document_ids = [a.document_id for a in attempts if a.document_id is not None]
    available = set(
        (
            await db.execute(
                project_documents_query(collection_id)
                .with_only_columns(Document.id)
                .where(Document.id.in_(document_ids))
            )
        )
        .scalars()
        .all()
        if document_ids
        else ()
    )
    after: dict[Any, Any] = {(a.request_id, a.previous_attempt_id): a for a in attempts}
    states = []
    for request in requests:
        chain = []
        attempt = after.get((request.id, None))
        while attempt is not None:
            chain.append(attempt)
            attempt = after.get((request.id, attempt.id))
        states.append(
            FulltextStateResponse(
                request_id=request.id,
                report_id=request.report_id,
                protocol_version_id=request.protocol_version_id,
                requested_by_id=request.requested_by_id,
                requested_at=request.requested_at,
                state=chain[-1].outcome if chain else "pending",
                head_attempt_id=chain[-1].id if chain else None,
                attempts=[
                    FulltextAttemptResponse(
                        id=a.id,
                        outcome=a.outcome,
                        reason=a.reason,
                        attempted_on=a.attempted_on,
                        actor_id=a.actor_id,
                        document_id=a.document_id,
                        document_content_hash=a.document_content_hash,
                        document_available=a.document_id in available,
                        previous_attempt_id=a.previous_attempt_id,
                        created_at=a.created_at,
                    )
                    for a in chain
                ],
            )
        )
    return states


async def _state(
    db: AsyncSession, collection_id: UUID, request_id: UUID
) -> FulltextStateResponse:
    states = await _states(db, collection_id, [request_id])
    assert len(states) == 1
    return states[0]


async def request_fulltext(
    db: AsyncSession,
    context: ProjectContext,
    actor_user_id: UUID,
    data: FulltextRequestCreate,
) -> tuple[FulltextStateResponse, bool]:
    """Seek one live report's full text; ``(state, created)``.

    One request per report, so "reports sought" cannot inflate; a retry after
    ``unavailable`` is a new attempt on the same request.
    """
    collection_id = cast(UUID, context.collection.id)
    stream = await _lock(db, collection_id)
    fingerprint = decision_request_fingerprint(
        {
            "operation": "request_fulltext",
            "actor_user_id": str(actor_user_id),
            **data.model_dump(mode="json"),
        }
    )
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        request_id = UUID(cast(dict[str, Any], replayed.payload)["request_id"])
        return await _state(db, collection_id, request_id), False
    await live_reports(db, collection_id, [data.report_id])
    existing = (
        await db.execute(
            select(ResearchFulltextRequest.id).where(
                ResearchFulltextRequest.collection_id == collection_id,
                ResearchFulltextRequest.report_id == data.report_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _conflict(ALREADY_REQUESTED)
    protocol_version_id = await current_protocol_version_id(db, collection_id)
    request = ResearchFulltextRequest(
        id=uuid4(),
        collection_id=collection_id,
        report_id=data.report_id,
        requested_by_id=actor_user_id,
        protocol_version_id=protocol_version_id and UUID(protocol_version_id),
    )
    db.add(request)
    await db.flush()
    await _append(
        db,
        collection_id,
        event_type="acquisition.requested",
        actor_user_id=actor_user_id,
        report_id=data.report_id,
        payload={
            "request_id": str(request.id),
            "report_id": str(data.report_id),
            "protocol_version_id": protocol_version_id,
        },
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _state(db, collection_id, cast(UUID, request.id)), True


async def record_attempt(
    db: AsyncSession,
    context: ProjectContext,
    request_id: UUID,
    actor_user_id: UUID,
    data: FulltextAttemptCreate,
) -> tuple[FulltextStateResponse, bool]:
    """Append one attempt to the request's chain; ``(state, created)``."""
    collection_id = cast(UUID, context.collection.id)
    stream = await _lock(db, collection_id)
    fingerprint = decision_request_fingerprint(
        {
            "operation": "record_attempt",
            "actor_user_id": str(actor_user_id),
            "request_id": str(request_id),
            **data.model_dump(mode="json"),
        }
    )
    replayed = await _replayed_event(db, stream, data.idempotency_key, fingerprint)
    if replayed is not None:
        return await _state(db, collection_id, request_id), False
    request = (
        await db.execute(
            select(ResearchFulltextRequest).where(
                ResearchFulltextRequest.id == request_id,
                ResearchFulltextRequest.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail=REQUEST_NOT_FOUND)
    report_id = cast(UUID, request.report_id)
    await live_reports(db, collection_id, [report_id])
    head = (
        await db.execute(
            select(ResearchFulltextAttempt).where(
                ResearchFulltextAttempt.request_id == request_id, _is_head()
            )
        )
    ).scalar_one_or_none()
    if head is not None and head.outcome == "retrieved":
        raise _conflict(ALREADY_RETRIEVED)
    # The head check: concurrent writers cannot fork the chain.
    if data.previous_attempt_id != (None if head is None else head.id):
        raise _conflict(ATTEMPT_STALE)
    content_hash = None
    if data.document_id is not None:
        document = (
            await db.execute(
                project_documents_query(collection_id).where(
                    Document.id == data.document_id
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise HTTPException(status_code=404, detail=DOCUMENT_NOT_FOUND)
        content_hash = cast(str | None, document.checksum_sha256)
        if content_hash is None:
            raise HTTPException(status_code=422, detail=DOCUMENT_UNHASHED)
    attempt = ResearchFulltextAttempt(
        id=uuid4(),
        request_id=request_id,
        outcome=data.outcome,
        reason=data.reason,
        attempted_on=data.attempted_on,
        actor_id=actor_user_id,
        document_id=data.document_id,
        document_content_hash=content_hash,
        previous_attempt_id=data.previous_attempt_id,
    )
    db.add(attempt)
    # No IntegrityError -> 409 mapping here on purpose: the head check above
    # is the guard, and the partial unique index must stay visible if it fails.
    await db.flush()
    payload: dict[str, Any] = {
        "request_id": str(request_id),
        "report_id": str(report_id),
        "attempt_id": str(attempt.id),
        "previous_attempt_id": (
            None if data.previous_attempt_id is None else str(data.previous_attempt_id)
        ),
        "attempted_on": data.attempted_on.isoformat(),
        "reason": data.reason,
    }
    if data.document_id is not None:
        payload["document_id"] = str(data.document_id)
        payload["document_content_hash"] = content_hash
    await _append(
        db,
        collection_id,
        event_type=_EVENT_TYPES[data.outcome],
        actor_user_id=actor_user_id,
        report_id=report_id,
        payload=payload,
        idempotency_key=data.idempotency_key,
        fingerprint=fingerprint,
    )
    return await _state(db, collection_id, request_id), True


async def list_fulltext(
    db: AsyncSession, context: ProjectContext
) -> list[FulltextStateResponse]:
    """Every request of the Collection with its head and chain (VIEW)."""
    return await _states(db, cast(UUID, context.collection.id))


async def retrieved_report_ids(
    db: AsyncSession, collection_id: UUID, report_ids: Iterable[UUID]
) -> set[UUID]:
    """The given reports whose own request, or a request of a report merged
    into them, has a ``retrieved`` head (the full-text screening gate)."""
    retrieved = (
        (
            await db.execute(
                select(ResearchFulltextRequest.report_id)
                .join(
                    ResearchFulltextAttempt,
                    ResearchFulltextAttempt.request_id == ResearchFulltextRequest.id,
                )
                .where(
                    ResearchFulltextRequest.collection_id == collection_id,
                    ResearchFulltextAttempt.outcome == "retrieved",
                    _is_head(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not retrieved:
        return set()
    merged = await _merged(db, collection_id)
    finals = {_final_report(merged, report_id) for report_id in retrieved}
    return finals & set(report_ids)


async def _merged(db: AsyncSession, collection_id: UUID) -> dict[UUID, UUID]:
    return dict(
        (
            await db.execute(
                select(ResearchReport.id, ResearchReport.merged_into_report_id).where(
                    ResearchReport.collection_id == collection_id,
                    ResearchReport.merged_into_report_id.is_not(None),
                )
            )
        )
        .tuples()
        .all()
    )


def _final_report(merged: dict[UUID, UUID], report_id: UUID) -> UUID:
    """Follow the merge chain to the surviving report (cycle-safe)."""
    seen = set()
    while report_id in merged and report_id not in seen:
        seen.add(report_id)
        report_id = merged[report_id]
    return report_id


async def document_reports(db: AsyncSession, collection_id: UUID) -> dict[UUID, UUID]:
    """document_id -> surviving report, for every ``retrieved`` head attempt
    (GOO-309): only GOO-303's attempt records a document as a report's full
    text, so any other document has no report and no study."""
    rows = (
        (
            await db.execute(
                select(
                    ResearchFulltextAttempt.document_id,
                    ResearchFulltextRequest.report_id,
                )
                .join(
                    ResearchFulltextRequest,
                    ResearchFulltextAttempt.request_id == ResearchFulltextRequest.id,
                )
                .where(
                    ResearchFulltextRequest.collection_id == collection_id,
                    ResearchFulltextAttempt.outcome == "retrieved",
                    _is_head(),
                )
            )
        )
        .tuples()
        .all()
    )
    if not rows:
        return {}
    merged = await _merged(db, collection_id)
    return {document: _final_report(merged, report) for document, report in rows}
