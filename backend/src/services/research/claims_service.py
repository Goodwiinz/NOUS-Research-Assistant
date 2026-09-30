"""Versioned claims, evidence links, stance snapshots and assessments (GOO-306).

Lock order for every writer: the route's ``resolve_project`` (Workspace SHARE
-> Collection UPDATE, roles reloaded), then this Collection's
``research_claims`` stream ``FOR UPDATE``. The idempotency replay check runs
under that lock before any validation. Every writer inserts, appends one
decision event and commits exactly once; the five tables are insert-only.
A unique-index hit (a lost race past the tip checks) is a stable 409.
"""

from typing import Any, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.document import Document
from src.models.draft_citation import DraftCitation
from src.models.draft_review import DraftReview
from src.models.evidence import StanceClassificationModel
from src.models.extraction_matrix import (
    ExtractionAcceptedValue,
    ExtractionFormVersion,
    ExtractionMatrix,
    ExtractionObservation,
)
from src.models.generated_draft import GeneratedDraft
from src.models.research_claim import (
    ResearchClaim,
    ResearchClaimAssessment,
    ResearchClaimEvidenceLink,
    ResearchClaimStanceObservation,
    ResearchClaimVersion,
)
from src.models.research_project_role import ResearchProjectRole
from src.services.evidence.stance_classifier import StanceClassifier
from src.services.research import claim_rules, extraction_rules
from src.services.research import source_anchors as anchors
from src.services.research.extraction_forms_service import (
    _changed,
    _flush_or_conflict,
    current_version,
    document_pins,
)
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.shared.claim_schemas import (
    ClaimAssessmentCreate,
    ClaimAssessmentResponse,
    ClaimCounts,
    ClaimCreate,
    ClaimDetailResponse,
    ClaimLinkCreate,
    ClaimLinkResponse,
    ClaimListResponse,
    ClaimResponse,
    ClaimSummary,
    ClaimVersionCreate,
    ClaimVersionResponse,
    StanceObservationCreate,
    StanceObservationResponse,
)

AGGREGATE_TYPE = "research_claims"
SUBJECT_TYPE = "research_claim"

CLAIM_NOT_FOUND = "Claim not found"
DRAFT_NOT_FOUND = "Draft not found"
EVIDENCE_NOT_FOUND = "Evidence not found"
VERSION_STALE = "Claim version is stale; reload"
LINK_STALE = "Link is stale; reload"
ASSESSMENT_STALE = "Assessment is stale; reload"
EVIDENCE_STALE = "Evidence is stale"
NO_CHANGE = "No change"
SPAN_MISMATCH = "Span does not match the source"
CITATION_OTHER_DRAFT = "Citation belongs to another draft version"
LEGACY_UNASSESSABLE = "Legacy links cannot be assessed"
NO_STANCE = (
    "No stance classification for this claim and source revision; "
    "run the evidence meter"
)
# The request fields each link kind accepts; the server fills the rest.
_REQUEST_FIELDS = {
    "extraction": ("accepted_value_id",),
    "source_span": ("document_id", "start_char", "end_char", "quote"),
    "legacy_unanchored": ("draft_citation_id",),
}
_TARGET_FIELDS = (
    "accepted_value_id",
    "document_id",
    "start_char",
    "end_char",
    "quote",
    "draft_citation_id",
)
# The meter's classifier fingerprint (``api/evidence/router._classifier_version``).
_classifier = StanceClassifier()


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _fingerprint(operation: str, actor_id: UUID, data: Any, **ids: Any) -> str:
    return decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor_id),
            **{name: str(value) for name, value in ids.items()},
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )


async def _begin(
    db: AsyncSession,
    context: ProjectContext,
    operation: str,
    data: Any,
    actor_id: UUID,
    **ids: Any,
) -> tuple[str, str, dict[str, Any] | None]:
    """Lock the stream, then return (key, fingerprint, replayed payload)."""
    stream = await lock_aggregate_stream(
        db,
        collection_id=_cid(context),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=_cid(context),
    )
    key = f"{operation}:{data.idempotency_key}"
    fingerprint = _fingerprint(operation, actor_id, data, **ids)
    replay = await _replayed_event(db, stream, key, fingerprint)
    return key, fingerprint, None if replay is None else dict(replay.payload)


async def _append(
    db: AsyncSession,
    context: ProjectContext,
    *,
    event_type: str,
    claim_id: UUID,
    actor_id: UUID,
    actor_role: str,
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
    reason: str | None = None,
) -> None:
    payload = {
        "collection_id": str(_cid(context)),
        "claim_id": str(claim_id),
        **payload,
    }
    try:
        await append_decision(
            db,
            collection_id=_cid(context),
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=_cid(context),
            event_type=event_type,
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role=actor_role,
            subject_type=SUBJECT_TYPE,
            subject_id=claim_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


def _unprocessable(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


# --- Loads, all scoped to the Collection; a foreign id looks missing. ---


async def _claim(db: AsyncSession, context: ProjectContext, claim_id: UUID) -> Any:
    claim = (
        await db.execute(
            select(ResearchClaim).where(
                ResearchClaim.id == claim_id,
                ResearchClaim.collection_id == _cid(context),
            )
        )
    ).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail=CLAIM_NOT_FOUND)
    return claim


async def _version(
    db: AsyncSession, context: ProjectContext, claim_id: UUID, version_id: UUID
) -> Any:
    version = (
        await db.execute(
            select(ResearchClaimVersion).where(
                ResearchClaimVersion.id == version_id,
                ResearchClaimVersion.claim_id == claim_id,
                ResearchClaimVersion.collection_id == _cid(context),
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail=CLAIM_NOT_FOUND)
    return version


def _version_is_tip() -> Any:
    newer = aliased(ResearchClaimVersion)
    return ~exists().where(newer.supersedes_claim_version_id == ResearchClaimVersion.id)


def _link_is_tip() -> Any:
    newer = aliased(ResearchClaimEvidenceLink)
    return ~exists().where(newer.supersedes_link_id == ResearchClaimEvidenceLink.id)


def _assessment_is_tip() -> Any:
    newer = aliased(ResearchClaimAssessment)
    return ~exists().where(newer.supersedes_assessment_id == ResearchClaimAssessment.id)


def _accepted_is_tip() -> Any:
    newer = aliased(ExtractionAcceptedValue)
    return ~exists().where(
        newer.supersedes_accepted_value_id == ExtractionAcceptedValue.id
    )


async def _version_tip(db: AsyncSession, claim_id: UUID) -> Any:
    return (
        await db.execute(
            select(ResearchClaimVersion).where(
                ResearchClaimVersion.claim_id == claim_id, _version_is_tip()
            )
        )
    ).scalar_one()


async def _draft(db: AsyncSession, context: ProjectContext, draft_id: UUID) -> Any:
    draft = (
        await db.execute(
            select(GeneratedDraft).where(
                GeneratedDraft.id == draft_id,
                GeneratedDraft.project_id == _cid(context),
            )
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail=DRAFT_NOT_FOUND)
    return draft


async def _visible_document(
    db: AsyncSession, context: ProjectContext, document_id: UUID | None
) -> Any:
    document = (
        await db.execute(
            project_documents_query(_cid(context)).where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail=EVIDENCE_NOT_FOUND)
    return document


async def _draft_review_id(
    db: AsyncSession, context: ProjectContext, draft: Any, draft_hash: str
) -> UUID | None:
    """The draft's citation review, reused only if it reviewed this content."""
    review_id = (draft.generation_params or {}).get("citation_review_id")
    if not review_id:
        return None
    try:
        review_uuid = UUID(str(review_id))
    except ValueError:
        return None
    return cast(
        UUID | None,
        (
            await db.execute(
                select(DraftReview.id).where(
                    DraftReview.id == review_uuid,
                    DraftReview.project_id == _cid(context),
                    DraftReview.candidate_content_hash == draft_hash,
                )
            )
        ).scalar_one_or_none(),
    )


async def _accepted_state(db: AsyncSession, accepted: Any) -> tuple[bool, bool]:
    """(is chain tip, is stale) for an accepted extraction value, derived as
    GOO-304's cell view does: a changed field definition or source text."""
    is_tip = (
        await db.execute(
            select(ExtractionAcceptedValue.id).where(
                ExtractionAcceptedValue.id == accepted.id, _accepted_is_tip()
            )
        )
    ).scalar_one_or_none() is not None
    form_version: Any = await db.get(ExtractionFormVersion, accepted.form_version_id)
    current = await current_version(db, form_version.matrix_id)
    document = await db.get(Document, accepted.document_id)
    pins = document_pins(document)
    source = (
        ""
        if _changed(pins, accepted.source_hash, accepted.text_sha256)
        else accepted.source_hash
    )
    stale = extraction_rules.is_stale(
        extraction_rules.field_def(form_version.fields, accepted.field_id),
        (
            None
            if current is None
            else extraction_rules.field_def(current.fields, accepted.field_id)
        ),
        source,
        pins[0],
    )
    return is_tip, stale


# --- Versions ---


async def _new_version(
    db: AsyncSession,
    context: ProjectContext,
    *,
    claim_id: UUID,
    data: ClaimCreate,
    actor_id: UUID,
    version_no: int,
    supersedes: UUID | None,
) -> Any:
    draft = await _draft(db, context, data.draft_id)
    try:
        claim_rules.check_passage(
            draft.content, data.start_char, data.end_char, data.text
        )
    except ValueError as error:
        raise _unprocessable(error) from error
    draft_hash = claim_rules.content_hash(draft.content)
    return ResearchClaimVersion(
        id=uuid4(),
        collection_id=_cid(context),
        claim_id=claim_id,
        version_no=version_no,
        kind=data.kind,
        attributed_to_user_id=actor_id if data.kind == "interpretation" else None,
        text=data.text,
        text_sha256=claim_rules.content_hash(data.text),
        normalized_hash=claim_rules.normalized_hash(data.text),
        draft_id=draft.id,
        draft_version=draft.version,
        draft_content_hash=draft_hash,
        start_char=data.start_char,
        end_char=data.end_char,
        draft_review_id=await _draft_review_id(db, context, draft, draft_hash),
        created_by_id=actor_id,
        supersedes_claim_version_id=supersedes,
    )


async def _append_versioned(
    db: AsyncSession,
    context: ProjectContext,
    row: Any,
    actor_id: UUID,
    key: str,
    fingerprint: str,
) -> None:
    await _append(
        db,
        context,
        event_type="claim.versioned",
        claim_id=row.claim_id,
        actor_id=actor_id,
        actor_role="editor",
        payload={
            "claim_version_id": str(row.id),
            "version_no": row.version_no,
            "supersedes_claim_version_id": _str(row.supersedes_claim_version_id),
            "kind": row.kind,
            "attributed_to_user_id": _str(row.attributed_to_user_id),
            "text_sha256": row.text_sha256,
            "normalized_hash": row.normalized_hash,
            "draft_id": str(row.draft_id),
            "draft_version": row.draft_version,
            "draft_content_hash": row.draft_content_hash,
            "start_char": row.start_char,
            "end_char": row.end_char,
            "draft_review_id": _str(row.draft_review_id),
        },
        key=key,
        fingerprint=fingerprint,
    )


def _claim_response(claim: Any, version: Any) -> ClaimResponse:
    return ClaimResponse(
        id=claim.id,
        collection_id=claim.collection_id,
        created_by_id=claim.created_by_id,
        created_at=claim.created_at,
        version=ClaimVersionResponse.model_validate(version),
    )


async def create_claim(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: ClaimCreate
) -> tuple[ClaimResponse, bool]:
    """A new claim identity and its v1 over an exact draft passage (EDIT)."""
    key, fingerprint, replay = await _begin(db, context, "claim", data, actor_id)
    if replay is not None:
        version = await db.get(ResearchClaimVersion, UUID(replay["claim_version_id"]))
        claim = await db.get(ResearchClaim, UUID(replay["claim_id"]))
        return _claim_response(claim, version), True
    claim_id = uuid4()
    claim = ResearchClaim(
        id=claim_id, collection_id=_cid(context), created_by_id=actor_id
    )
    row = await _new_version(
        db,
        context,
        claim_id=claim_id,
        data=data,
        actor_id=actor_id,
        version_no=1,
        supersedes=None,
    )
    db.add(claim)
    await db.flush()
    db.add(row)
    await _flush_or_conflict(db, VERSION_STALE)
    await _append_versioned(db, context, row, actor_id, key, fingerprint)
    # ponytail: GOO-307 seam - invalidate_dependents(...) goes here.
    await db.commit()
    await db.refresh(claim)
    await db.refresh(row)
    return _claim_response(claim, row), False


async def create_version(
    db: AsyncSession,
    context: ProjectContext,
    claim_id: UUID,
    actor_id: UUID,
    data: ClaimVersionCreate,
) -> tuple[ClaimVersionResponse, bool]:
    """Append a reworded passage superseding the claim's tip (EDIT)."""
    key, fingerprint, replay = await _begin(
        db, context, "version", data, actor_id, claim_id=claim_id
    )
    if replay is not None:
        version = await db.get(ResearchClaimVersion, UUID(replay["claim_version_id"]))
        return ClaimVersionResponse.model_validate(version), True
    await _claim(db, context, claim_id)
    tip = await _version_tip(db, claim_id)
    if data.supersedes_claim_version_id != tip.id:
        raise HTTPException(status_code=409, detail=VERSION_STALE)
    if (data.draft_id, data.start_char, data.end_char, data.text, data.kind) == (
        tip.draft_id,
        tip.start_char,
        tip.end_char,
        tip.text,
        tip.kind,
    ):
        raise HTTPException(status_code=409, detail=NO_CHANGE)
    row = await _new_version(
        db,
        context,
        claim_id=claim_id,
        data=data,
        actor_id=actor_id,
        version_no=tip.version_no + 1,
        supersedes=tip.id,
    )
    db.add(row)
    await _flush_or_conflict(db, VERSION_STALE)
    await _append_versioned(db, context, row, actor_id, key, fingerprint)
    # ponytail: GOO-307 seam - invalidate_dependents(...) goes here.
    await db.commit()
    await db.refresh(row)
    return ClaimVersionResponse.model_validate(row), False


# --- Links ---


def _check_request_shape(data: ClaimLinkCreate) -> None:
    """A new link names only its kind's target fields; the server fills the
    hashes. A withdrawal names no target: it copies the superseded link."""
    if data.status == "withdrawn" and data.supersedes_link_id is None:
        raise HTTPException(
            status_code=422, detail="A withdrawal must supersede a link"
        )
    allowed = () if data.status == "withdrawn" else _REQUEST_FIELDS[data.kind]
    extra = [
        f for f in _TARGET_FIELDS if f not in allowed and getattr(data, f) is not None
    ]
    if extra:
        raise HTTPException(
            status_code=422,
            detail=f"A {data.kind} link must not carry {', '.join(extra)}",
        )
    missing = [f for f in allowed if getattr(data, f) is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"A {data.kind} link requires {', '.join(missing)}",
        )


async def _extraction_target(
    db: AsyncSession, context: ProjectContext, accepted_value_id: UUID
) -> dict[str, Any]:
    accepted: Any = (
        await db.execute(
            select(ExtractionAcceptedValue)
            .join(
                ExtractionFormVersion,
                ExtractionFormVersion.id == ExtractionAcceptedValue.form_version_id,
            )
            .join(
                ExtractionMatrix, ExtractionMatrix.id == ExtractionFormVersion.matrix_id
            )
            .where(
                ExtractionAcceptedValue.id == accepted_value_id,
                ExtractionMatrix.project_id == _cid(context),
                ExtractionMatrix.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if accepted is None:
        raise HTTPException(status_code=404, detail=EVIDENCE_NOT_FOUND)
    await _visible_document(db, context, accepted.document_id)
    is_tip, stale = await _accepted_state(db, accepted)
    if not is_tip or stale:
        raise HTTPException(status_code=409, detail=EVIDENCE_STALE)
    # The anchor observation's text hash, else the first cited observation's.
    observation_id = accepted.anchor_observation_id or next(
        iter(accepted.observation_ids or []), None
    )
    observation = (
        None
        if observation_id is None
        else await db.get(ExtractionObservation, UUID(str(observation_id)))
    )
    return {
        "accepted_value_id": accepted.id,
        "document_id": accepted.document_id,
        "source_hash": accepted.source_hash,
        "text_sha256": None if observation is None else observation.text_sha256,
    }


async def _span_target(
    db: AsyncSession, context: ProjectContext, data: ClaimLinkCreate
) -> dict[str, Any]:
    document = await _visible_document(db, context, data.document_id)
    anchor = anchors.verify_anchor(
        document.content_text, data.quote, start_hint=data.start_char
    )
    if anchor.status != "verified" or anchor.end_char != data.end_char:
        raise HTTPException(status_code=422, detail=SPAN_MISMATCH)
    source_hash, text_hash = document_pins(document)
    return {
        "document_id": document.id,
        "source_hash": source_hash,
        "text_sha256": text_hash,
        "start_char": data.start_char,
        "end_char": data.end_char,
        "quote": data.quote,
    }


async def _legacy_target(
    db: AsyncSession, version: Any, draft_citation_id: UUID
) -> dict[str, Any]:
    citation = (
        await db.execute(
            select(DraftCitation).where(
                DraftCitation.id == draft_citation_id,
                DraftCitation.draft_id == version.draft_id,
            )
        )
    ).scalar_one_or_none()
    if citation is None:
        raise HTTPException(status_code=422, detail=CITATION_OTHER_DRAFT)
    return {"draft_citation_id": citation.id, "document_id": citation.document_id}


async def link(
    db: AsyncSession,
    context: ProjectContext,
    claim_id: UUID,
    actor_id: UUID,
    data: ClaimLinkCreate,
) -> tuple[ClaimLinkResponse, bool]:
    """Append a link from the claim's tip version to evidence (EDIT)."""
    key, fingerprint, replay = await _begin(
        db, context, "link", data, actor_id, claim_id=claim_id
    )
    if replay is not None:
        row = await db.get(ResearchClaimEvidenceLink, UUID(replay["link_id"]))
        return ClaimLinkResponse.model_validate(row), True
    await _claim(db, context, claim_id)
    version = await _version(db, context, claim_id, data.claim_version_id)
    if (await _version_tip(db, claim_id)).id != version.id:
        raise HTTPException(status_code=409, detail=VERSION_STALE)
    _check_request_shape(data)
    prior = None
    if data.supersedes_link_id is not None:
        prior = (
            await db.execute(
                select(ResearchClaimEvidenceLink).where(
                    ResearchClaimEvidenceLink.id == data.supersedes_link_id,
                    ResearchClaimEvidenceLink.claim_version_id == version.id,
                    _link_is_tip(),
                )
            )
        ).scalar_one_or_none()
        if prior is None:
            raise HTTPException(status_code=409, detail=LINK_STALE)
    kind: str = data.kind
    if data.status == "withdrawn":
        assert prior is not None  # _check_request_shape: a withdrawal supersedes
        kind = str(prior.kind)
        target = {name: getattr(prior, name) for name in _TARGET_FIELDS}
        target |= {"source_hash": prior.source_hash, "text_sha256": prior.text_sha256}
    elif kind == "extraction":
        assert data.accepted_value_id is not None
        target = await _extraction_target(db, context, data.accepted_value_id)
    elif kind == "source_span":
        target = await _span_target(db, context, data)
    else:
        assert data.draft_citation_id is not None
        target = await _legacy_target(db, version, data.draft_citation_id)
    columns: dict[str, Any] = {
        name: None for name in (*_TARGET_FIELDS, "source_hash", "text_sha256")
    } | target
    try:
        claim_rules.check_link_shape(
            kind,
            status=data.status,
            supersedes_link_id=data.supersedes_link_id,
            **columns,
        )
    except ValueError as error:
        raise _unprocessable(error) from error
    row = ResearchClaimEvidenceLink(
        id=uuid4(),
        collection_id=_cid(context),
        claim_version_id=version.id,
        kind=kind,
        status=data.status,
        supersedes_link_id=data.supersedes_link_id,
        created_by_id=actor_id,
        **columns,
    )
    db.add(row)
    await _flush_or_conflict(db, LINK_STALE)
    await _append(
        db,
        context,
        event_type="claim.linked",
        claim_id=claim_id,
        actor_id=actor_id,
        actor_role="editor",
        payload={
            "claim_version_id": str(version.id),
            "link_id": str(row.id),
            "supersedes_link_id": _str(row.supersedes_link_id),
            "status": row.status,
            "kind": kind,
            "accepted_value_id": _str(row.accepted_value_id),
            "draft_citation_id": _str(row.draft_citation_id),
            "document_id": _str(row.document_id),
            "source_hash": row.source_hash,
            "text_sha256": row.text_sha256,
            "start_char": row.start_char,
            "end_char": row.end_char,
            "quote_sha256": anchors.text_sha256(columns["quote"]),
        },
        key=key,
        fingerprint=fingerprint,
    )
    # ponytail: GOO-307 seam - invalidate_dependents(...) goes here.
    await db.commit()
    await db.refresh(row)
    return ClaimLinkResponse.model_validate(row), False


# --- Stance observations ---


async def _live_link(
    db: AsyncSession, context: ProjectContext, claim_id: UUID, link_id: UUID
) -> tuple[Any, Any]:
    found = (
        await db.execute(
            select(ResearchClaimEvidenceLink, ResearchClaimVersion)
            .join(
                ResearchClaimVersion,
                ResearchClaimVersion.id == ResearchClaimEvidenceLink.claim_version_id,
            )
            .where(
                ResearchClaimEvidenceLink.id == link_id,
                ResearchClaimEvidenceLink.collection_id == _cid(context),
                ResearchClaimVersion.claim_id == claim_id,
            )
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail=CLAIM_NOT_FOUND)
    row, version = found
    if row.kind == "legacy_unanchored":
        raise HTTPException(status_code=409, detail=LEGACY_UNASSESSABLE)
    superseded = (
        await db.execute(
            select(ResearchClaimEvidenceLink.id).where(
                ResearchClaimEvidenceLink.supersedes_link_id == row.id
            )
        )
    ).first()
    if superseded is not None or row.status != "linked":
        raise HTTPException(status_code=409, detail=LINK_STALE)
    return row, version


async def observe_stance(
    db: AsyncSession,
    context: ProjectContext,
    claim_id: UUID,
    link_id: UUID,
    actor_id: UUID,
    data: StanceObservationCreate,
) -> tuple[StanceObservationResponse, bool]:
    """Snapshot the meter's stance row for this claim and exact source
    revision (EDIT). No LLM call: the meter must have classified it."""
    key, fingerprint, replay = await _begin(
        db, context, "observe", data, actor_id, claim_id=claim_id, link_id=link_id
    )
    if replay is not None:
        row = await db.get(
            ResearchClaimStanceObservation, UUID(replay["observation_id"])
        )
        return StanceObservationResponse.model_validate(row), True
    await _claim(db, context, claim_id)
    evidence, version = await _live_link(db, context, claim_id, link_id)
    # ponytail: classify inline if users won't open the meter first.
    stance = (
        (
            await db.execute(
                select(StanceClassificationModel)
                .where(
                    StanceClassificationModel.organization_id
                    == context.organization_id,
                    StanceClassificationModel.claim_hash == version.normalized_hash,
                    StanceClassificationModel.source_id == evidence.document_id,
                    StanceClassificationModel.model_version
                    == _classifier.classifier_version,
                    StanceClassificationModel.source_content_hash
                    == evidence.text_sha256,
                )
                .order_by(StanceClassificationModel.updated_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if stance is None:
        raise HTTPException(status_code=409, detail=NO_STANCE)
    row = ResearchClaimStanceObservation(
        id=uuid4(),
        collection_id=_cid(context),
        link_id=evidence.id,
        stance=getattr(stance.stance, "value", stance.stance),
        classifier_confidence=stance.confidence,
        justification_excerpt=stance.justification_excerpt,
        classifier_version=stance.model_version,
        inference_model_version=stance.inference_model_version,
        source_content_hash=stance.source_content_hash,
        stance_classification_id=stance.id,
        classified_at=stance.updated_at,
        observed_by_id=actor_id,
    )
    db.add(row)
    await db.flush()
    await _append(
        db,
        context,
        event_type="claim.observed",
        claim_id=claim_id,
        actor_id=actor_id,
        actor_role="machine",
        payload={
            "link_id": str(evidence.id),
            "observation_id": str(row.id),
            "stance": row.stance,
            "stance_classification_id": str(stance.id),
            "classifier_version": row.classifier_version,
            "inference_model_version": row.inference_model_version,
            "source_content_hash": row.source_content_hash,
            "classified_at": (
                None if row.classified_at is None else row.classified_at.isoformat()
            ),
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    await db.refresh(row)
    return StanceObservationResponse.model_validate(row), False


# --- Assessments ---


async def _live_link_ids(db: AsyncSession, version_id: UUID) -> list[UUID]:
    return list(
        (
            await db.execute(
                select(ResearchClaimEvidenceLink.id).where(
                    ResearchClaimEvidenceLink.claim_version_id == version_id,
                    ResearchClaimEvidenceLink.status == "linked",
                    ResearchClaimEvidenceLink.kind != "legacy_unanchored",
                    _link_is_tip(),
                )
            )
        )
        .scalars()
        .all()
    )


async def assess(
    db: AsyncSession,
    context: ProjectContext,
    claim_id: UUID,
    actor_id: UUID,
    data: ClaimAssessmentCreate,
) -> tuple[ClaimAssessmentResponse, bool]:
    """An adjudicator's judgement of the tip version (ADJUDICATE only),
    citing its live links and their stance snapshots."""
    if ResearchProjectRole.ADJUDICATOR not in context.effective_roles:
        raise HTTPException(status_code=403, detail="adjudicator role required")
    key, fingerprint, replay = await _begin(
        db, context, "assess", data, actor_id, claim_id=claim_id
    )
    if replay is not None:
        row = await db.get(ResearchClaimAssessment, UUID(replay["assessment_id"]))
        return ClaimAssessmentResponse.model_validate(row), True
    await _claim(db, context, claim_id)
    version = await _version(db, context, claim_id, data.claim_version_id)
    if (await _version_tip(db, claim_id)).id != version.id:
        raise HTTPException(status_code=409, detail=VERSION_STALE)
    observed: dict[Any, Any] = {
        observation_id: link_id
        for observation_id, link_id in (
            await db.execute(
                select(
                    ResearchClaimStanceObservation.id,
                    ResearchClaimStanceObservation.link_id,
                ).where(
                    ResearchClaimStanceObservation.id.in_(data.stance_observation_ids),
                    ResearchClaimStanceObservation.collection_id == _cid(context),
                )
            )
        ).all()
    }
    if len(observed) != len(data.stance_observation_ids):  # unknown or repeated
        raise HTTPException(
            status_code=422, detail="Every observation must belong to a cited link"
        )
    try:
        claim_rules.check_assessment(
            data.stance,
            data.link_ids,
            list(observed.values()),
            await _live_link_ids(db, version.id),
        )
    except ValueError as error:
        raise _unprocessable(error) from error
    tip = (
        await db.execute(
            select(ResearchClaimAssessment.id).where(
                ResearchClaimAssessment.claim_version_id == version.id,
                _assessment_is_tip(),
            )
        )
    ).scalar_one_or_none()
    if data.supersedes_assessment_id != tip:
        raise HTTPException(status_code=409, detail=ASSESSMENT_STALE)
    row = ResearchClaimAssessment(
        id=uuid4(),
        collection_id=_cid(context),
        claim_version_id=version.id,
        stance=data.stance,
        link_ids=[str(i) for i in data.link_ids],
        stance_observation_ids=[str(i) for i in data.stance_observation_ids],
        rationale=data.rationale,
        assessed_by_id=actor_id,
        actor_role="adjudicator",
        supersedes_assessment_id=data.supersedes_assessment_id,
    )
    db.add(row)
    await _flush_or_conflict(db, ASSESSMENT_STALE)
    await _append(
        db,
        context,
        event_type="claim.assessed",
        claim_id=claim_id,
        actor_id=actor_id,
        actor_role="adjudicator",
        reason=data.rationale,
        payload={
            "claim_version_id": str(version.id),
            "assessment_id": str(row.id),
            "supersedes_assessment_id": _str(row.supersedes_assessment_id),
            "stance": row.stance,
            "link_ids": row.link_ids,
            "stance_observation_ids": row.stance_observation_ids,
        },
        key=key,
        fingerprint=fingerprint,
    )
    # ponytail: GOO-307 seam - invalidate_dependents(...) goes here.
    await db.commit()
    await db.refresh(row)
    return ClaimAssessmentResponse.model_validate(row), False


# --- Reads (VIEW; zero writes) ---


async def _source_changed(db: AsyncSession, row: Any) -> bool:
    """GOO-305's derive-on-read rule for one link."""
    if row.kind == "legacy_unanchored":
        return False
    document: Any = await db.get(Document, row.document_id)
    current = anchors.text_sha256(None if document is None else document.content_text)
    accepted_tip, accepted_stale = True, False
    if row.kind == "extraction":
        accepted = await db.get(ExtractionAcceptedValue, row.accepted_value_id)
        accepted_tip, accepted_stale = await _accepted_state(db, accepted)
    return claim_rules.link_source_changed(
        row.text_sha256, current, accepted_tip, accepted_stale
    )


async def _link_responses(
    db: AsyncSession, rows: Sequence[Any], observations: Sequence[Any]
) -> list[ClaimLinkResponse]:
    """``observations`` in ``created_at, id`` order; the last one per link wins."""
    latest = {o.link_id: o for o in observations}
    # ponytail: per-link staleness queries; batch them if claims lists get long.
    return [
        ClaimLinkResponse.model_validate(row).model_copy(
            update={
                "source_changed": await _source_changed(db, row),
                "latest_observation": (
                    None
                    if row.id not in latest
                    else StanceObservationResponse.model_validate(latest[row.id])
                ),
            }
        )
        for row in rows
    ]


async def _citation_review_status(db: AsyncSession, version: Any) -> str | None:
    if version.draft_review_id is None:
        return None
    review: Any = await db.get(DraftReview, version.draft_review_id)
    wanted = version.text.strip()
    for entry in ((review.review or {}).get("claims") or []) if review else []:
        if isinstance(entry, dict) and entry.get("text") == wanted:
            return cast(str | None, entry.get("support_status"))
    return None


async def _rows(db: AsyncSession, model: Any, *where: Any) -> list[Any]:
    return list(
        (
            await db.execute(
                select(model).where(*where).order_by(model.created_at, model.id)
            )
        )
        .scalars()
        .all()
    )


async def list_claims(
    db: AsyncSession, context: ProjectContext, draft_id: UUID | None
) -> ClaimListResponse:
    """Each claim's tip version, or with ``draft_id`` its latest version
    pinned to that draft (claims without one are left out). Live links only;
    the counts are computed from the same selected items."""
    versions = await _rows(
        db, ResearchClaimVersion, ResearchClaimVersion.collection_id == _cid(context)
    )
    superseded = {v.supersedes_claim_version_id for v in versions}
    selected: dict[UUID, Any] = {}
    for version in versions:  # created_at order: a later match replaces
        if draft_id is None and version.id in superseded:
            continue
        if draft_id is not None and version.draft_id != draft_id:
            continue
        current = selected.get(version.claim_id)
        if current is None or version.version_no > current.version_no:
            selected[version.claim_id] = version
    version_ids = [v.id for v in selected.values()]
    links = await _rows(
        db,
        ResearchClaimEvidenceLink,
        ResearchClaimEvidenceLink.claim_version_id.in_(version_ids),
        ResearchClaimEvidenceLink.status == "linked",
        _link_is_tip(),
    )
    observations = await _rows(
        db,
        ResearchClaimStanceObservation,
        ResearchClaimStanceObservation.link_id.in_([row.id for row in links]),
    )
    assessments = {
        a.claim_version_id: a
        for a in await _rows(
            db,
            ResearchClaimAssessment,
            ResearchClaimAssessment.claim_version_id.in_(version_ids),
            _assessment_is_tip(),
        )
    }
    link_views = await _link_responses(db, links, observations)
    items = []
    for version in sorted(selected.values(), key=lambda v: (v.created_at, str(v.id))):
        assessment = assessments.get(version.id)
        items.append(
            ClaimSummary(
                claim_id=version.claim_id,
                version=ClaimVersionResponse.model_validate(version),
                is_tip=version.id not in superseded,
                links=[v for v in link_views if v.claim_version_id == version.id],
                assessment=(
                    None
                    if assessment is None
                    else ClaimAssessmentResponse.model_validate(assessment)
                ),
                citation_review_status=await _citation_review_status(db, version),
            )
        )
    by_kind = {kind: 0 for kind in claim_rules.LINK_KINDS}
    for item in items:
        for view in item.links:
            by_kind[view.kind] += 1
    assessed = sum(item.assessment is not None for item in items)
    return ClaimListResponse(
        items=items,
        counts=ClaimCounts(
            claims=len(items),
            links_by_kind=by_kind,
            legacy_unanchored=by_kind["legacy_unanchored"],
            assessed=assessed,
            unassessed=len(items) - assessed,
        ),
    )


async def get_claim(
    db: AsyncSession, context: ProjectContext, claim_id: UUID
) -> ClaimDetailResponse:
    """Every version, link, observation and assessment of one claim."""
    claim = await _claim(db, context, claim_id)
    versions = await _rows(
        db, ResearchClaimVersion, ResearchClaimVersion.claim_id == claim.id
    )
    version_ids = [v.id for v in versions]
    links = await _rows(
        db,
        ResearchClaimEvidenceLink,
        ResearchClaimEvidenceLink.claim_version_id.in_(version_ids),
    )
    observations = await _rows(
        db,
        ResearchClaimStanceObservation,
        ResearchClaimStanceObservation.link_id.in_([row.id for row in links]),
    )
    assessments = await _rows(
        db,
        ResearchClaimAssessment,
        ResearchClaimAssessment.claim_version_id.in_(version_ids),
    )
    return ClaimDetailResponse(
        id=claim.id,
        collection_id=claim.collection_id,
        created_by_id=claim.created_by_id,
        created_at=claim.created_at,
        versions=[ClaimVersionResponse.model_validate(v) for v in versions],
        links=await _link_responses(db, links, observations),
        observations=[
            StanceObservationResponse.model_validate(o) for o in observations
        ],
        assessments=[ClaimAssessmentResponse.model_validate(a) for a in assessments],
    )
