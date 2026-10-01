"""External peer-review rounds, anchored comments, responses and decisions
(GOO-314).

Lock order for every writer: the route's ``resolve_project`` (Workspace SHARE
-> Collection UPDATE, roles reloaded), then this Collection's
``research_peer_review`` stream ``FOR UPDATE``. The idempotency replay check
runs under that lock before any validation; then targets are loaded
Collection-scoped (a foreign id looks missing), the expected tip is checked,
one row is inserted, one decision event appended and the session committed
exactly once. ``UNIQUE(supersedes_*)`` and the partial unique initial indexes
are the backstop: a lost race is the same stable 409, never an overwrite.

External reviewers are labels inside a round: nothing here maps one to a
user or a role. ``draft_reviews`` (machine citation review) is never read or
written. Status and anchor state are derived on every read.
"""

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.generated_draft import GeneratedDraft
from src.models.peer_review import (
    PeerReviewComment,
    PeerReviewDecision,
    PeerReviewResponse,
    PeerReviewReviewer,
    PeerReviewRound,
)
from src.models.research_claim import ResearchClaimAssessment, ResearchClaimVersion
from src.models.research_project_role import ResearchProjectRole
from src.services.research import peer_review_rules as rules
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    _workspace_role,
)
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.peer_review_schemas import (
    CommentCreate,
    CommentDetail,
    CommentResponse,
    CommentVersionResponse,
    DecisionCreate,
    DecisionResponse,
    DiffHunk,
    DraftRef,
    EvidenceClaim,
    ResponseCreate,
    ResponseVersionResponse,
    ResponseView,
    ReviewerResponse,
    RoundCreate,
    RoundDetail,
    RoundListResponse,
    RoundResponse,
)

AGGREGATE_TYPE = "research_peer_review"
SUBJECT_TYPE = "peer_review_comment"
EXPORT_SCHEMA = "nous.peer-review-response.v1"

ROUND_NOT_FOUND = "Round not found"
REVIEWER_NOT_FOUND = "Reviewer not found"
COMMENT_NOT_FOUND = "Comment not found"
DRAFT_NOT_FOUND = "Draft not found"
CLAIM_VERSION_NOT_FOUND = "Claim version not found"
COMMENT_STALE = "Comment changed; reload"
RESPONSE_STALE = "Response changed; reload"
DECISION_STALE = "Decision changed; reload"
HASH_MISMATCH = "Draft content hash does not match"
NUMBER_TAKEN = "Comment number already used in this round"
LATER_VERSION = "Revision must be a later saved version"
ANCHOR_LOST = "Comment anchor not found in the base version"
ADJUDICATOR_REQUIRED = "adjudicator role required"


def decision_action(kind: str) -> ResearchAction:
    """Assignment is EDIT; resolution and reopening are ADJUDICATE (an
    explicit adjudicator assignment; ownership and edit rights never do)."""
    return ResearchAction.EDIT if kind == "assigned" else ResearchAction.ADJUDICATE


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _unprocessable(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def _stale(detail: str, tip: Any) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"message": detail, "current_tip_id": _str(tip)}
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
    fingerprint = decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor_id),
            **{name: str(value) for name, value in ids.items()},
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    return key, fingerprint, None if replay is None else dict(replay.payload)


async def _append(
    db: AsyncSession,
    context: ProjectContext,
    *,
    event_type: str,
    subject_id: UUID,
    actor_id: UUID,
    actor_role: str,
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
    reason: str | None = None,
) -> None:
    payload = {"collection_id": str(_cid(context)), **payload}
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
            subject_id=subject_id,
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=reason,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


async def _flush_or_stale(db: AsyncSession, detail: str, current_tip: Any) -> None:
    """A unique-index hit (a race past the tip check) is the tip-check 409."""
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_unique_violation(error):
            raise
        raise _stale(detail, await current_tip()) from error


# --- Loads, all scoped to the Collection; a foreign id looks missing. ---


async def _draft(db: AsyncSession, collection_id: UUID, draft_id: UUID) -> Any:
    draft = (
        await db.execute(
            select(GeneratedDraft).where(
                GeneratedDraft.id == draft_id,
                GeneratedDraft.project_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail=DRAFT_NOT_FOUND)
    return draft


async def _current_draft(db: AsyncSession, collection_id: UUID) -> Any:
    draft = (
        await db.execute(
            select(GeneratedDraft)
            .where(GeneratedDraft.project_id == collection_id)
            .order_by(GeneratedDraft.is_current.desc(), GeneratedDraft.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail=DRAFT_NOT_FOUND)
    return draft


async def _round(db: AsyncSession, collection_id: UUID, round_id: UUID) -> Any:
    row = (
        await db.execute(
            select(PeerReviewRound).where(
                PeerReviewRound.id == round_id,
                PeerReviewRound.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=ROUND_NOT_FOUND)
    return row


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


def _chains(rows: Iterable[Any], supersedes: str) -> dict[UUID, list[Any]]:
    """Group versioned rows by root: ``{root_id: [root, ..., tip]}``."""
    rows = list(rows)
    successor = {getattr(r, supersedes): r for r in rows if getattr(r, supersedes)}
    chains: dict[UUID, list[Any]] = {}
    for row in rows:
        if getattr(row, supersedes) is None:
            chain = [row]
            while chain[-1].id in successor:
                chain.append(successor[chain[-1].id])
            chains[row.id] = chain
    return chains


async def _comment_root(
    db: AsyncSession, collection_id: UUID, comment_root_id: UUID
) -> tuple[Any, list[Any]]:
    """(round, the comment's version chain) for a root id in this Collection."""
    root: Any = (
        await db.execute(
            select(PeerReviewComment)
            .join(PeerReviewRound, PeerReviewRound.id == PeerReviewComment.round_id)
            .where(
                PeerReviewComment.id == comment_root_id,
                PeerReviewComment.supersedes_comment_id.is_(None),
                PeerReviewRound.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if root is None:
        raise HTTPException(status_code=404, detail=COMMENT_NOT_FOUND)
    round_row = await db.get(PeerReviewRound, root.round_id)
    comments = await _rows(
        db, PeerReviewComment, PeerReviewComment.round_id == root.round_id
    )
    return round_row, _chains(comments, "supersedes_comment_id")[root.id]


async def _response_tip(db: AsyncSession, comment_root_id: UUID) -> Any:
    chain = _chains(
        await _rows(
            db,
            PeerReviewResponse,
            PeerReviewResponse.comment_root_id == comment_root_id,
        ),
        "supersedes_response_id",
    )
    # The partial unique initial index allows one chain per comment.
    return next((c[-1] for c in chain.values()), None)


def _decision_tips(decisions: Sequence[Any]) -> tuple[Any, Any]:
    """(assignment tip, resolution tip) of one comment's decisions."""
    assignment = [d for d in decisions if d.kind == "assigned"]
    resolution = [d for d in decisions if d.kind in rules.RESOLUTION_KINDS]
    tips = []
    for rows in (assignment, resolution):
        chain = next(iter(_chains(rows, "supersedes_decision_id").values()), None)
        tips.append(None if chain is None else chain[-1])
    return tips[0], tips[1]


# --- Responses ---


def _round_response(row: Any, version: int, reviewers: Sequence[Any]) -> RoundResponse:
    return RoundResponse(
        id=row.id,
        collection_id=row.collection_id,
        draft_id=row.draft_id,
        draft_version=version,
        draft_content_hash=row.draft_content_hash,
        label=row.label,
        received_at=row.received_at,
        created_by_id=row.created_by_id,
        created_at=row.created_at,
        reviewers=[ReviewerResponse.model_validate(r) for r in reviewers],
    )


async def _round_out(db: AsyncSession, row: Any) -> RoundResponse:
    draft: Any = await db.get(GeneratedDraft, row.draft_id)
    reviewers = await _rows(
        db, PeerReviewReviewer, PeerReviewReviewer.round_id == row.id
    )
    reviewers.sort(key=lambda r: r.label)
    return _round_response(row, draft.version, reviewers)


def _comment_out(row: Any, root_id: UUID) -> CommentResponse:
    return CommentResponse(
        **CommentVersionResponse.model_validate(row).model_dump(),
        comment_root_id=root_id,
    )


# --- Writers ---


async def create_round(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: RoundCreate
) -> tuple[RoundResponse, bool]:
    """Record a round of one exact saved version with its reviewers (EDIT)."""
    key, fingerprint, replay = await _begin(db, context, "round", data, actor_id)
    if replay is not None:
        row: Any = await db.get(PeerReviewRound, UUID(replay["round_id"]))
        return await _round_out(db, row), True
    draft = await _draft(db, _cid(context), data.draft_id)
    if rules.sha256(draft.content) != data.draft_content_hash:
        raise HTTPException(status_code=409, detail=HASH_MISMATCH)
    labels = [r.label for r in data.reviewers]
    if len(set(labels)) != len(labels):
        raise HTTPException(status_code=422, detail="Reviewer labels must be unique")
    row = PeerReviewRound(
        id=uuid4(),
        collection_id=_cid(context),
        draft_id=draft.id,
        draft_content_hash=data.draft_content_hash,
        label=data.label,
        received_at=data.received_at,
        created_by_id=actor_id,
    )
    db.add(row)
    await db.flush()
    reviewers = [
        PeerReviewReviewer(
            id=uuid4(),
            round_id=row.id,
            label=r.label,
            display_name=r.display_name,
            created_by_id=actor_id,
        )
        for r in data.reviewers
    ]
    db.add_all(reviewers)
    await db.flush()
    await _append(
        db,
        context,
        event_type="review_round.recorded",
        subject_id=row.id,
        actor_id=actor_id,
        actor_role="editor",
        payload={
            "round_id": str(row.id),
            "draft_id": str(draft.id),
            "draft_content_hash": row.draft_content_hash,
            "reviewer_ids": [str(r.id) for r in reviewers],
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _round_out(db, row), False


async def add_comment(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    round_id: UUID,
    data: CommentCreate,
) -> tuple[CommentResponse, bool]:
    """A new comment, or a successor version of a comment's tip (EDIT)."""
    key, fingerprint, replay = await _begin(
        db, context, "comment", data, actor_id, round_id=round_id
    )
    if replay is not None:
        row: Any = await db.get(PeerReviewComment, UUID(replay["comment_id"]))
        return _comment_out(row, UUID(replay["comment_root_id"])), True
    round_row = await _round(db, _cid(context), round_id)
    reviewer = (
        await db.execute(
            select(PeerReviewReviewer).where(
                PeerReviewReviewer.id == data.reviewer_id,
                PeerReviewReviewer.round_id == round_row.id,
            )
        )
    ).scalar_one_or_none()
    if reviewer is None:
        raise HTTPException(status_code=404, detail=REVIEWER_NOT_FOUND)
    chains = _chains(
        await _rows(db, PeerReviewComment, PeerReviewComment.round_id == round_row.id),
        "supersedes_comment_id",
    )
    root_id: UUID
    if data.supersedes_comment_id is None:
        if any(chain[0].number == data.number for chain in chains.values()):
            raise HTTPException(status_code=409, detail=NUMBER_TAKEN)
        root_id = uuid4()
        current_tip = None
    else:
        chain = next(
            (
                c
                for c in chains.values()
                if data.supersedes_comment_id in {r.id for r in c}
            ),
            None,
        )
        if chain is None:
            raise HTTPException(status_code=404, detail=COMMENT_NOT_FOUND)
        current_tip = chain[-1].id
        if current_tip != data.supersedes_comment_id:
            raise _stale(COMMENT_STALE, current_tip)
        if (chain[0].reviewer_id, chain[0].number) != (data.reviewer_id, data.number):
            raise HTTPException(
                status_code=422,
                detail="A comment version keeps its reviewer and number",
            )
        root_id = chain[0].id
    draft: Any = await db.get(GeneratedDraft, round_row.draft_id)
    anchor: dict[str, Any] = {
        "start_char": None,
        "end_char": None,
        "quote": None,
        "quote_sha256": None,
    }
    if data.anchor is not None:
        try:
            digest = rules.check_anchor(
                draft.content, data.anchor.start, data.anchor.end, data.anchor.quote
            )
        except ValueError as error:
            raise _unprocessable(error) from error
        anchor = {
            "start_char": data.anchor.start,
            "end_char": data.anchor.end,
            "quote": data.anchor.quote,
            "quote_sha256": digest,
        }
    row = PeerReviewComment(
        id=root_id if data.supersedes_comment_id is None else uuid4(),
        round_id=round_row.id,
        reviewer_id=reviewer.id,
        number=data.number,
        body=data.body,
        draft_id=round_row.draft_id,
        draft_content_hash=round_row.draft_content_hash,
        supersedes_comment_id=data.supersedes_comment_id,
        author_id=actor_id,
        **anchor,
    )
    db.add(row)

    async def tip() -> Any:
        return current_tip

    await _flush_or_stale(db, COMMENT_STALE, tip)
    await _append(
        db,
        context,
        event_type="review_comment.versioned",
        subject_id=root_id,
        actor_id=actor_id,
        actor_role="editor",
        payload={
            "round_id": str(round_row.id),
            "comment_id": str(row.id),
            "comment_root_id": str(root_id),
            "supersedes_comment_id": _str(row.supersedes_comment_id),
            "reviewer_id": str(reviewer.id),
            "draft_content_hash": row.draft_content_hash,
            "anchored": row.quote is not None,
            "quote_sha256": row.quote_sha256,
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    await db.refresh(row)
    return _comment_out(row, root_id), False


async def _evidence_ids(
    db: AsyncSession, collection_id: UUID, ids: Sequence[UUID]
) -> list[str]:
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="Evidence ids must be unique")
    found = set(
        (
            await db.execute(
                select(ResearchClaimVersion.id).where(
                    ResearchClaimVersion.id.in_(ids),
                    ResearchClaimVersion.collection_id == collection_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(found) != len(ids):
        raise HTTPException(status_code=404, detail=CLAIM_VERSION_NOT_FOUND)
    return [str(i) for i in ids]


def _base_span(comment: Any, base: Any) -> tuple[int, int] | None:
    """The comment's anchor span inside the base version, if anchored."""
    if comment.quote is None:
        return None
    state, start, end = rules.anchor_state(
        comment.quote,
        comment.quote_sha256,
        comment.draft_content_hash,
        comment.start_char,
        comment.end_char,
        base.content,
        rules.sha256(base.content),
    )
    if state == "unresolved_anchor" or start is None or end is None:
        raise HTTPException(status_code=422, detail=ANCHOR_LOST)
    return start, end


async def respond(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    comment_root_id: UUID,
    data: ResponseCreate,
) -> tuple[ResponseVersionResponse, bool]:
    """A new response version superseding the comment's response tip (EDIT)."""
    key, fingerprint, replay = await _begin(
        db, context, "respond", data, actor_id, comment_root_id=comment_root_id
    )
    if replay is not None:
        row: Any = await db.get(PeerReviewResponse, UUID(replay["response_id"]))
        return ResponseVersionResponse.model_validate(row), True
    cid = _cid(context)
    round_row, chain = await _comment_root(db, cid, comment_root_id)
    rationale = data.rationale
    try:
        rules.check_response_shape(data.kind, data.revised_draft_id, rationale)
    except ValueError as error:
        raise _unprocessable(error) from error
    if data.kind == "no_change" and data.base_draft_id is not None:
        raise HTTPException(
            status_code=422, detail="A no-change response must not name a base"
        )
    evidence = await _evidence_ids(db, cid, data.evidence_claim_version_ids)
    revised = base = None
    digest = None
    if data.kind == "change":
        assert data.revised_draft_id is not None
        revised = await _draft(db, cid, data.revised_draft_id)
        reviewed: Any = await db.get(GeneratedDraft, round_row.draft_id)
        base = await _draft(db, cid, data.base_draft_id or round_row.draft_id)
        if revised.version <= reviewed.version or revised.version <= base.version:
            raise HTTPException(status_code=422, detail=LATER_VERSION)
        hunks = rules.anchored_diff(base.content, revised.content)
        if not hunks:
            raise HTTPException(status_code=422, detail=rules.NO_DIFF)
        span = _base_span(chain[-1], base)
        if span is not None and not rules.touches(hunks, *span):
            raise HTTPException(status_code=422, detail=rules.UNTOUCHED)
        digest = rules.diff_sha256(hunks)
    tip = await _response_tip(db, comment_root_id)
    tip_id = None if tip is None else tip.id
    if data.supersedes_response_id != tip_id:
        raise _stale(RESPONSE_STALE, tip_id)
    row = PeerReviewResponse(
        id=uuid4(),
        comment_root_id=comment_root_id,
        kind=data.kind,
        body=data.body,
        revised_draft_id=None if revised is None else revised.id,
        revised_content_hash=None if revised is None else rules.sha256(revised.content),
        base_draft_id=None if base is None else base.id,
        diff_sha256=digest,
        rationale=rationale,
        evidence_claim_version_ids=evidence,
        supersedes_response_id=data.supersedes_response_id,
        author_id=actor_id,
    )
    db.add(row)

    async def current_tip() -> Any:
        latest = await _response_tip(db, comment_root_id)
        return None if latest is None else latest.id

    await _flush_or_stale(db, RESPONSE_STALE, current_tip)
    await _append(
        db,
        context,
        event_type="review_response.versioned",
        subject_id=comment_root_id,
        actor_id=actor_id,
        actor_role="editor",
        payload={
            "comment_root_id": str(comment_root_id),
            "response_id": str(row.id),
            "supersedes_response_id": _str(row.supersedes_response_id),
            "kind": row.kind,
            "revised_draft_id": _str(row.revised_draft_id),
            "diff_sha256": row.diff_sha256,
            "evidence_claim_version_ids": evidence,
        },
        key=key,
        fingerprint=fingerprint,
        reason=rationale,
    )
    await db.commit()
    await db.refresh(row)
    return ResponseVersionResponse.model_validate(row), False


async def decide(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    comment_root_id: UUID,
    data: DecisionCreate,
) -> tuple[DecisionResponse, bool]:
    """Assign (EDIT) or resolve/reopen (ADJUDICATE) a comment."""
    resolving = data.kind in rules.RESOLUTION_KINDS
    if resolving and ResearchProjectRole.ADJUDICATOR not in context.effective_roles:
        raise HTTPException(status_code=403, detail=ADJUDICATOR_REQUIRED)
    key, fingerprint, replay = await _begin(
        db, context, "decide", data, actor_id, comment_root_id=comment_root_id
    )
    if replay is not None:
        row: Any = await db.get(PeerReviewDecision, UUID(replay["decision_id"]))
        return DecisionResponse.model_validate(row), True
    await _comment_root(db, _cid(context), comment_root_id)
    if data.kind == "assigned":
        if data.assignee_id is None or data.response_id is not None:
            raise HTTPException(
                status_code=422, detail="An assignment names only an assignee"
            )
        if _workspace_role(context.workspace, data.assignee_id) is None:
            raise HTTPException(
                status_code=422, detail="Assignee is not a project member"
            )
    elif data.assignee_id is not None:
        raise HTTPException(
            status_code=422, detail="Only an assignment names an assignee"
        )
    decisions = await _rows(
        db,
        PeerReviewDecision,
        PeerReviewDecision.comment_root_id == comment_root_id,
    )
    assignment, resolution = _decision_tips(decisions)
    chain_tip = resolution if resolving else assignment
    chain_tip_id = None if chain_tip is None else chain_tip.id
    if data.supersedes_decision_id != chain_tip_id:
        raise _stale(DECISION_STALE, chain_tip_id)
    if data.kind == "resolved":
        response_tip = await _response_tip(db, comment_root_id)
        response_tip_id = None if response_tip is None else response_tip.id
        if data.response_id is None or data.response_id != response_tip_id:
            raise _stale(RESPONSE_STALE, response_tip_id)
    elif data.kind == "reopened":
        if resolution is None or resolution.kind != "resolved":
            raise HTTPException(status_code=409, detail="Comment is not resolved")
        if data.response_id is not None:
            raise HTTPException(status_code=422, detail="A reopening names no response")
    actor_role = "adjudicator" if resolving else "editor"
    row = PeerReviewDecision(
        id=uuid4(),
        comment_root_id=comment_root_id,
        kind=data.kind,
        assignee_id=data.assignee_id,
        response_id=data.response_id,
        rationale=data.rationale,
        actor_id=actor_id,
        actor_role=actor_role,
        supersedes_decision_id=data.supersedes_decision_id,
    )
    db.add(row)

    async def current_tip() -> Any:
        tips = _decision_tips(
            await _rows(
                db,
                PeerReviewDecision,
                PeerReviewDecision.comment_root_id == comment_root_id,
            )
        )
        latest = tips[1] if resolving else tips[0]
        return None if latest is None else latest.id

    await _flush_or_stale(db, DECISION_STALE, current_tip)
    await _append(
        db,
        context,
        event_type="review_decision.recorded",
        subject_id=comment_root_id,
        actor_id=actor_id,
        actor_role=actor_role,
        payload={
            "comment_root_id": str(comment_root_id),
            "decision_id": str(row.id),
            "kind": row.kind,
            "assignee_id": _str(row.assignee_id),
            "response_id": _str(row.response_id),
            "supersedes_decision_id": _str(row.supersedes_decision_id),
        },
        key=key,
        fingerprint=fingerprint,
        reason=data.rationale,
    )
    await db.commit()
    await db.refresh(row)
    return DecisionResponse.model_validate(row), False


# --- Reads (VIEW; zero writes) ---


async def list_rounds(db: AsyncSession, context: ProjectContext) -> RoundListResponse:
    rounds = await _rows(
        db, PeerReviewRound, PeerReviewRound.collection_id == _cid(context)
    )
    return RoundListResponse(rounds=[await _round_out(db, r) for r in rounds])


async def _evidence(
    db: AsyncSession, collection_id: UUID, ids: set[str]
) -> dict[str, EvidenceClaim]:
    if not ids:
        return {}
    versions = await _rows(
        db,
        ResearchClaimVersion,
        ResearchClaimVersion.id.in_([UUID(i) for i in ids]),
        ResearchClaimVersion.collection_id == collection_id,
    )
    assessments = _chains(
        await _rows(
            db,
            ResearchClaimAssessment,
            ResearchClaimAssessment.claim_version_id.in_([v.id for v in versions]),
        ),
        "supersedes_assessment_id",
    )
    stance = {
        chain[-1].claim_version_id: chain[-1].stance for chain in assessments.values()
    }
    return {
        str(v.id): EvidenceClaim(
            claim_version_id=v.id,
            claim_id=v.claim_id,
            text=v.text,
            assessment_stance=stance.get(v.id),
        )
        for v in versions
    }


async def _detail(
    db: AsyncSession, collection_id: UUID, round_row: Any, target: Any
) -> RoundDetail:
    reviewers = {
        r.id: r
        for r in await _rows(
            db, PeerReviewReviewer, PeerReviewReviewer.round_id == round_row.id
        )
    }
    chains = _chains(
        await _rows(db, PeerReviewComment, PeerReviewComment.round_id == round_row.id),
        "supersedes_comment_id",
    )
    roots = list(chains)
    responses = await _rows(
        db, PeerReviewResponse, PeerReviewResponse.comment_root_id.in_(roots)
    )
    decisions = await _rows(
        db, PeerReviewDecision, PeerReviewDecision.comment_root_id.in_(roots)
    )
    draft_ids = {r.revised_draft_id for r in responses if r.revised_draft_id}
    draft_ids |= {r.base_draft_id for r in responses if r.base_draft_id}
    drafts = {
        d.id: d
        for d in await _rows(db, GeneratedDraft, GeneratedDraft.id.in_(draft_ids))
    }
    evidence = await _evidence(
        db,
        collection_id,
        {str(i) for r in responses for i in r.evidence_claim_version_ids or []},
    )
    target_hash = rules.sha256(target.content)
    details = []
    for root_id, versions in chains.items():
        tip = versions[-1]
        state, start, end = rules.anchor_state(
            tip.quote,
            tip.quote_sha256,
            tip.draft_content_hash,
            tip.start_char,
            tip.end_char,
            target.content,
            target_hash,
        )
        own = [r for r in responses if r.comment_root_id == root_id]
        response_chain = next(
            iter(_chains(own, "supersedes_response_id").values()), None
        )
        response_tip = None if response_chain is None else response_chain[-1]
        own_decisions = [d for d in decisions if d.comment_root_id == root_id]
        assignment, resolution = _decision_tips(own_decisions)
        view = None
        if response_tip is not None:
            view = ResponseView(
                **ResponseVersionResponse.model_validate(response_tip).model_dump(),
                evidence=[
                    evidence[str(i)]
                    for i in response_tip.evidence_claim_version_ids or []
                    if str(i) in evidence
                ],
            )
            if response_tip.kind == "change":
                hunks = rules.anchored_diff(
                    drafts[response_tip.base_draft_id].content,
                    drafts[response_tip.revised_draft_id].content,
                )
                view.hunks = [DiffHunk(**h) for h in hunks]
                view.diff_verified = (
                    rules.diff_sha256(hunks) == response_tip.diff_sha256
                )
        details.append(
            CommentDetail(
                comment_root_id=root_id,
                reviewer_id=tip.reviewer_id,
                reviewer_label=reviewers[tip.reviewer_id].label,
                number=tip.number,
                status=rules.comment_status(
                    None if response_tip is None else response_tip.id,
                    (
                        None
                        if resolution is None
                        else (resolution.kind, resolution.response_id)
                    ),
                ),
                anchor_state=state,
                anchor_start=start,
                anchor_end=end,
                current=CommentVersionResponse.model_validate(tip),
                versions=[CommentVersionResponse.model_validate(v) for v in versions],
                response=view,
                responses=[ResponseVersionResponse.model_validate(r) for r in own],
                assignment=(
                    None
                    if assignment is None
                    else DecisionResponse.model_validate(assignment)
                ),
                resolution=(
                    None
                    if resolution is None
                    else DecisionResponse.model_validate(resolution)
                ),
                decisions=[DecisionResponse.model_validate(d) for d in own_decisions],
            )
        )
    details.sort(key=lambda d: (d.reviewer_label, d.number))
    return RoundDetail(
        round=await _round_out(db, round_row),
        target=DraftRef(id=target.id, version=target.version, content_hash=target_hash),
        comments=details,
    )


async def get_round(
    db: AsyncSession,
    context: ProjectContext,
    round_id: UUID,
    target_draft_id: UUID | None = None,
) -> RoundDetail:
    """Comments with derived status and anchor state against the target
    version (default: the project's current draft)."""
    cid = _cid(context)
    round_row = await _round(db, cid, round_id)
    target = (
        await _current_draft(db, cid)
        if target_draft_id is None
        else await _draft(db, cid, target_draft_id)
    )
    return await _detail(db, cid, round_row, target)


def _markdown(detail: RoundDetail) -> str:
    r = detail.round
    lines = [
        f"# Response to reviewers: {r.label}",
        "",
        f"Reviewed version: v{r.draft_version} (`{r.draft_content_hash}`)",
        f"Anchors checked against: v{detail.target.version}"
        f" (`{detail.target.content_hash}`)",
        "",
    ]
    reviewer = None
    for c in detail.comments:
        if c.reviewer_label != reviewer:
            reviewer = c.reviewer_label
            lines += [f"## {reviewer}", ""]
        quote = c.current.quote
        lines += [
            f"### Comment {c.number} ({c.status}; anchor: {c.anchor_state})",
            "",
            c.current.body,
            "",
        ]
        if quote is not None:
            lines += [
                f"> {quote}",
                "",
                f"Original anchor: v{r.draft_version}"
                f" [{c.current.start_char}, {c.current.end_char}]"
                + (
                    f"; now [{c.anchor_start}, {c.anchor_end}]"
                    if c.anchor_start is not None
                    else ""
                ),
                "",
            ]
        response = c.response
        if response is None:
            lines += ["**Response:** none", ""]
        else:
            lines += [
                f"**Response ({response.kind})** by `{response.author_id}`"
                f" at {response.created_at.isoformat()}:",
                "",
                response.body,
                "",
            ]
            if response.kind == "change":
                lines += [
                    f"Revised version: `{response.revised_draft_id}`"
                    f" (diff verified: {response.diff_verified})",
                    "",
                ]
                for h in response.hunks:
                    lines += [
                        f"- {h.op} old {h.old} -> new {h.new}",
                        f"  - old: {h.old_text}",
                        f"  - new: {h.new_text}",
                    ]
                lines.append("")
            else:
                lines += [f"Rationale: {response.rationale}", ""]
            for e in response.evidence:
                lines.append(
                    f"- Evidence claim `{e.claim_version_id}`: {e.text}"
                    f" (assessment: {e.assessment_stance or 'none'})"
                )
            if response.evidence:
                lines.append("")
        if c.resolution is not None:
            lines += [
                f"**Resolution:** {c.resolution.kind} by `{c.resolution.actor_id}`"
                f" ({c.resolution.actor_role}) at {c.resolution.created_at.isoformat()}",
                "",
            ]
    lines += ["## Unresolved items", ""]
    unresolved = [c for c in detail.comments if c.status != "resolved"]
    for c in unresolved:
        lines.append(
            f"- {c.reviewer_label} comment {c.number}: {c.status};"
            f" anchor {c.anchor_state}"
            + (f' (original quote: "{c.current.quote}")' if c.current.quote else "")
        )
    if not unresolved:
        lines.append("- none")
    return "\n".join(lines) + "\n"


async def export_round(
    db: AsyncSession,
    context: ProjectContext,
    round_id: UUID,
    fmt: str,
    target_draft_id: UUID | None = None,
) -> tuple[bytes, str, str]:
    """The response export: (bytes, filename, media type). The trailing
    ``Unresolved items`` lists every comment not ``resolved`` with its anchor
    state (an ``unresolved_anchor`` keeps its original quote)."""
    detail = await get_round(db, context, round_id, target_draft_id)
    stem = f"peer-review-{round_id}"
    if fmt == "markdown":
        return _markdown(detail).encode("utf-8"), f"{stem}.md", "text/markdown"
    body = detail.model_dump(mode="json")
    body["unresolved"] = [
        {
            "comment_root_id": str(c.comment_root_id),
            "status": c.status,
            "anchor_state": c.anchor_state,
        }
        for c in detail.comments
        if c.status != "resolved"
    ]
    package = {
        "schema": EXPORT_SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "body": body,
        "body_sha256": canonical_json_sha256(body),
    }
    content = json.dumps(package, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return content, f"{stem}.json", "application/json"


async def open_obligations(
    db: AsyncSession, collection_id: UUID, draft_id: UUID
) -> list[dict[str, Any]]:
    """GOO-315's peer-review input: every comment in this Collection's rounds
    on a reviewed version ``<=`` the draft's version whose status is not
    ``resolved``, with its anchor state against the draft. Empty means the
    obligation passes (GOO-315 decides ``not_applicable`` when no round)."""
    draft = await _draft(db, collection_id, draft_id)
    rounds = list(
        (
            await db.execute(
                select(PeerReviewRound)
                .join(GeneratedDraft, GeneratedDraft.id == PeerReviewRound.draft_id)
                .where(
                    PeerReviewRound.collection_id == collection_id,
                    GeneratedDraft.version <= draft.version,
                )
                .order_by(PeerReviewRound.created_at, PeerReviewRound.id)
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for round_row in rounds:
        detail = await _detail(db, collection_id, round_row, draft)
        out += [
            {
                "comment_root_id": str(c.comment_root_id),
                "state": c.status,
                "anchor_state": c.anchor_state,
            }
            for c in detail.comments
            if c.status != "resolved"
        ]
    return out


async def export_body(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``peer_review.json`` body: every round with its
    comments, responses and decisions, anchors checked against the current
    draft."""
    cid = _cid(context)
    rounds = await _rows(db, PeerReviewRound, PeerReviewRound.collection_id == cid)
    if not rounds:
        return {"rounds": []}
    target = await _current_draft(db, cid)
    return {
        "rounds": [
            (await _detail(db, cid, r, target)).model_dump(mode="json") for r in rounds
        ]
    }
