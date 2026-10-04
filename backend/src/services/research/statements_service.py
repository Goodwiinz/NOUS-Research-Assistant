"""Statement sets, author approvals, ORCID receipts and venue checks (GOO-316).

A statement set is a project-level, versioned record that exists before a
candidate: GOO-315's ``build_snapshot`` captures the current set's id, hash
and approvals, and verified promotion applies the ``statements`` and
``venue`` obligations to that exact candidate. Approvals bind the set hash;
an attestation is never shown as a self-approval.

Authorship is display-linking only: ``user_id`` on an author grants no role,
and nothing in ``project_access`` reads this module. ORCID ``authenticated``
needs an ``orcid_authentications`` receipt for the author's linked user;
``record_orcid`` keeps only ``orcid``, ``name`` and ``scope`` from the token
response (the ID token as a sha256). The ORCID token exchange is this
module's only outbound call: nothing here deposits or submits anything.

Lock order: the route's ``resolve_project``, then (candidate creation only)
``research_manuscript``, then this Collection's ``research_statements``.
"""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.manuscript_statements import (
    ManuscriptStatementApproval,
    ManuscriptStatementSet,
    OrcidAuthentication,
    VenueCheck,
)
from src.models.peer_review import PeerReviewReviewer, PeerReviewRound
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.user import User
from src.services.research import venue_rules as rules
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.statements_schemas import (
    ApprovalCreate,
    ApprovalResponse,
    AuthorIdentity,
    OrcidAuthenticationListResponse,
    OrcidAuthenticationResponse,
    OrcidReceipt,
    OrcidStartResponse,
    StatementSetCreate,
    StatementSetResponse,
    StatementsListResponse,
    VenueCheckListResponse,
    VenueCheckResponse,
    VenueItem,
)

AGGREGATE_TYPE = "research_statements"
SUBJECT_TYPE = "statement_set"
STATE_TTL_SECONDS = 600

SET_NOT_FOUND = "Statement set not found"
SET_CHANGED = "Statement set changed; reload"
UNKNOWN_AUTHOR = "Unknown author"
NOT_THE_AUTHOR = "Only the author's own account can self-approve"
NOTE_REQUIRED = "A recorded attestation needs a note"
ALREADY_APPROVED = "This author already approved this statement set"
ORCID_NOT_CONFIGURED = "ORCID not configured"
ORCID_DENIED = "orcid_denied"
ORCID_STATE_INVALID = "orcid_state_invalid"
ORCID_EXCHANGE_FAILED = "orcid_exchange_failed"


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


# --- Reads ---


async def _sets(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(ManuscriptStatementSet)
        .where(ManuscriptStatementSet.collection_id == collection_id)
        .order_by(ManuscriptStatementSet.created_at, ManuscriptStatementSet.id),
    )


def _tip(sets: Sequence[Any]) -> Any:
    superseded = {s.supersedes_set_id for s in sets if s.supersedes_set_id}
    tips = [s for s in sets if s.id not in superseded]
    return tips[-1] if tips else None


async def _approvals(db: AsyncSession, set_ids: Sequence[Any]) -> list[Any]:
    if not set_ids:
        return []
    return await _all(
        db,
        select(ManuscriptStatementApproval)
        .where(ManuscriptStatementApproval.statement_set_id.in_(list(set_ids)))
        .order_by(
            ManuscriptStatementApproval.created_at, ManuscriptStatementApproval.id
        ),
    )


async def current_set(
    db: AsyncSession, collection_id: UUID
) -> tuple[Any, list[Any]] | None:
    """(tip set, its approvals) or None."""
    tip = _tip(await _sets(db, collection_id))
    if tip is None:
        return None
    return tip, await _approvals(db, [tip.id])


async def snapshot_binding(
    db: AsyncSession, collection_id: UUID
) -> dict[str, Any] | None:
    """What GOO-315's snapshot captures: the current set's id and hash and
    its approvals (never checked here; the obligations check them)."""
    current = await current_set(db, collection_id)
    if current is None:
        return None
    tip, approvals = current
    return {
        "statement_set_id": str(tip.id),
        "set_hash": tip.set_hash,
        "approvals": sorted(
            (
                {"author_key": a.author_key, "approval_id": str(a.id)}
                for a in approvals
                if a.set_hash == tip.set_hash
            ),
            key=lambda a: a["author_key"],
        ),
    }


async def package_body(db: AsyncSession, binding: Mapping[str, Any]) -> dict[str, Any]:
    """The ``statements.json`` body for a bound set (identified variant)."""
    row = cast(
        Any, await db.get(ManuscriptStatementSet, UUID(binding["statement_set_id"]))
    )
    approvals = await _approvals(db, [row.id])
    return {
        "statement_set_id": str(row.id),
        "set_hash": row.set_hash,
        "schema": row.schema,
        "credit_vocabulary": rules.CREDIT_VOCABULARY,
        "statements": dict(row.body),
        "approvals": [
            {"author_key": a.author_key, "method": a.method, "approval_id": str(a.id)}
            for a in approvals
            if a.set_hash == row.set_hash
        ],
    }


async def unapproved_authors(
    db: AsyncSession, binding: Mapping[str, Any]
) -> tuple[str, ...]:
    """Authors of the bound set without an approval of that exact set hash."""
    row = cast(
        Any, await db.get(ManuscriptStatementSet, UUID(binding["statement_set_id"]))
    )
    every_set = [s.id for s in await _sets(db, cast(UUID, row.collection_id))]
    approved = {
        a.author_key
        for a in await _approvals(db, every_set)
        # The binding: an approval of another set (or hash) never carries over.
        if a.statement_set_id == row.id and a.set_hash == binding["set_hash"]
    }
    return tuple(
        str(a["author_key"])
        for a in row.body.get("authors") or []
        if a["author_key"] not in approved
    )


async def venue_checks_for(db: AsyncSession, release_id: Any) -> list[Any]:
    return await _all(
        db,
        select(VenueCheck)
        .where(VenueCheck.release_id == release_id)
        .order_by(VenueCheck.created_at, VenueCheck.id),
    )


async def _user_identities(db: AsyncSession, user_ids: set[Any]) -> list[str]:
    users = await _all(db, select(User).where(User.id.in_(user_ids or {uuid4()})))
    out: list[str] = []
    for user in users:
        name = " ".join(p for p in (user.first_name, user.last_name) if p)
        out += [str(user.id), str(user.email), name]
    return out


async def identities(db: AsyncSession, context: ProjectContext) -> list[str]:
    """Every designated identity string for the anonymized variant: the
    current set's author fields and award ids, every project user's (owner,
    member, role holder, decision actor, linked author) id, full name and
    email, ORCID name claims of linked authors, and every peer reviewer's
    display name."""
    collection_id = _cid(context)
    workspace = context.workspace
    user_ids: set[Any] = {workspace.owner_id}
    user_ids |= {m.user_id for m in workspace.members}
    for row in await _all(
        db,
        select(ResearchProjectRoleAssignment).where(
            ResearchProjectRoleAssignment.collection_id == collection_id
        ),
    ):
        user_ids |= {row.user_id, row.assigned_by_id}
    actors = await db.execute(
        select(ResearchDecisionEvent.actor_user_id)
        .where(ResearchDecisionEvent.collection_id == collection_id)
        .distinct()
    )
    user_ids |= {a for (a,) in actors.all() if a is not None}
    strings: list[str] = []
    current = await current_set(db, collection_id)
    if current is not None:
        body = current[0].body
        for author in body.get("authors") or []:
            strings += [author.get(k) or "" for k in ("display_name", "email", "orcid")]
            strings += list(author.get("affiliations") or [])
            if author.get("user_id"):
                user_ids.add(UUID(author["user_id"]))
        for grant in (body.get("funding") or {}).get("grants") or []:
            strings.append(grant.get("award_id") or "")
    strings += await _user_identities(db, user_ids)
    claims = await db.execute(
        select(OrcidAuthentication.name_claim).where(
            OrcidAuthentication.user_id.in_(user_ids or {uuid4()})
        )
    )
    strings += [c for (c,) in claims.all() if c]
    reviewers = await db.execute(
        select(PeerReviewReviewer.display_name)
        .join(PeerReviewRound, PeerReviewRound.id == PeerReviewReviewer.round_id)
        .where(PeerReviewRound.collection_id == collection_id)
    )
    strings += [r for (r,) in reviewers.all() if r]
    return sorted({s for s in strings if s})


async def _orcid_rows(db: AsyncSession, user_ids: set[Any]) -> list[dict[str, Any]]:
    rows = await _all(
        db,
        select(OrcidAuthentication).where(
            OrcidAuthentication.user_id.in_(user_ids or {uuid4()})
        ),
    )
    return [
        {
            "id": r.id,
            "user_id": str(r.user_id),
            "orcid": r.orcid,
            "environment": r.environment,
            "token_received_at": r.token_received_at,
        }
        for r in rows
    ]


def _approval(row: Any) -> ApprovalResponse:
    return cast(ApprovalResponse, ApprovalResponse.model_validate(row))


async def author_identity_view(
    db: AsyncSession, context: ProjectContext, set_id: UUID
) -> list[AuthorIdentity]:
    """Per author: ORCID status with its receipt, and the approval of this
    exact set hash (if any)."""
    row = await _set(db, _cid(context), set_id)
    return (await _views(db, [row], row))[0].authors


def _authors(
    row: Any, receipts: Sequence[Mapping[str, Any]], approvals: Sequence[Any]
) -> list[AuthorIdentity]:
    by_author = {
        a.author_key: a
        for a in approvals
        if a.statement_set_id == row.id and a.set_hash == row.set_hash
    }
    out = []
    for author in row.body.get("authors") or []:
        status, receipt = rules.orcid_status(author, receipts)
        approval = by_author.get(author["author_key"])
        out.append(
            AuthorIdentity(
                author_key=author["author_key"],
                order=author["order"],
                display_name=author.get("display_name"),
                user_id=author.get("user_id"),
                orcid=author.get("orcid"),
                orcid_status=cast(Any, status),
                orcid_receipt=(
                    None
                    if receipt is None
                    else OrcidReceipt.model_validate(receipt, from_attributes=False)
                ),
                approval=None if approval is None else _approval(approval),
            )
        )
    return out


async def _views(
    db: AsyncSession, rows: Sequence[Any], tip: Any
) -> list[StatementSetResponse]:
    approvals = await _approvals(db, [r.id for r in rows])
    linked = {
        UUID(a["user_id"])
        for r in rows
        for a in r.body.get("authors") or []
        if a.get("user_id")
    }
    receipts = await _orcid_rows(db, linked)
    out = []
    for row in rows:
        authors = _authors(row, receipts, approvals)
        items = rules.required_items(row.body)
        own = [a for a in approvals if a.statement_set_id == row.id]
        out.append(
            StatementSetResponse(
                id=row.id,
                collection_id=row.collection_id,
                body=dict(row.body),
                set_hash=row.set_hash,
                schema_id=row.schema,
                credit_vocabulary=rules.CREDIT_VOCABULARY,
                supersedes_set_id=row.supersedes_set_id,
                created_by_id=row.created_by_id,
                created_at=row.created_at,
                is_tip=tip is not None and row.id == tip.id,
                missing_fields=[i["field"] for i in items],
                missing_items=[VenueItem(**i) for i in items],
                authors=authors,
                approvals=[_approval(a) for a in own],
                all_approved=bool(authors) and all(a.approval for a in authors),
            )
        )
    return out


async def list_sets(
    db: AsyncSession, context: ProjectContext
) -> StatementsListResponse:
    """VIEW: the tip and the history (oldest first)."""
    rows = await _sets(db, _cid(context))
    tip = _tip(rows)
    views = await _views(db, rows, tip)
    return StatementsListResponse(
        tip=next((v for v in views if v.is_tip), None),
        history=views,
        credit_roles=list(rules.CREDIT_ROLES_V1),
        credit_vocabulary=rules.CREDIT_VOCABULARY,
    )


async def _set(db: AsyncSession, collection_id: UUID, set_id: UUID) -> Any:
    row = (
        await db.execute(
            select(ManuscriptStatementSet).where(
                ManuscriptStatementSet.id == set_id,
                ManuscriptStatementSet.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=SET_NOT_FOUND)
    return row


async def _one(db: AsyncSession, collection_id: UUID, set_id: UUID) -> Any:
    rows = await _sets(db, collection_id)
    row = next(r for r in rows if r.id == set_id)
    return (await _views(db, [row], _tip(rows)))[0]


# --- Writers ---


async def _begin(
    db: AsyncSession,
    context: ProjectContext,
    operation: str,
    data: Any,
    actor_id: UUID,
    **ids: Any,
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
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
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
            actor_role="editor",
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


async def _flush(db: AsyncSession, detail: str) -> None:
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if not _is_unique_violation(error):
            raise
        raise HTTPException(status_code=409, detail=detail) from error


async def version_set(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: StatementSetCreate
) -> tuple[StatementSetResponse, bool]:
    """EDIT: record a new statement set version; commits once. 409 unless
    ``supersedes_set_id`` is the current tip (None for the first set)."""
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, context, "version", data, actor_id)
    if replay is not None:
        return await _one(db, collection_id, UUID(replay["statement_set_id"])), True
    try:
        body = rules.validate_statement_body(data.body.model_dump(mode="json"))
    except rules.StatementError as error:
        raise HTTPException(
            status_code=422, detail=f"Invalid statement field: {error.field}"
        ) from error
    tip = _tip(await _sets(db, collection_id))
    if (None if tip is None else tip.id) != data.supersedes_set_id:
        raise HTTPException(status_code=409, detail=SET_CHANGED)
    digest = rules.set_hash(body)
    row = ManuscriptStatementSet(
        id=uuid4(),
        collection_id=collection_id,
        body=body,
        set_hash=digest,
        schema=rules.STATEMENTS_SCHEMA,
        supersedes_set_id=data.supersedes_set_id,
        created_by_id=actor_id,
        actor_role="editor",
    )
    db.add(row)
    await _flush(db, SET_CHANGED)
    await _append(
        db,
        context,
        event_type="statements.versioned",
        subject_id=cast(UUID, row.id),
        actor_id=actor_id,
        payload={
            "statement_set_id": str(row.id),
            "supersedes_set_id": (
                None if data.supersedes_set_id is None else str(data.supersedes_set_id)
            ),
            "set_hash": digest,
            "author_keys": [a["author_key"] for a in body["authors"] or []],
            "missing_fields": rules.missing_fields(body),
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _one(db, collection_id, cast(UUID, row.id)), False


async def approve(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    set_id: UUID,
    data: ApprovalCreate,
) -> tuple[ApprovalResponse, bool]:
    """EDIT: one author's approval of the tip set's exact hash; commits once.
    ``in_app_self`` needs the actor to be the author's linked user (403);
    ``recorded_attestation`` needs a note."""
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(
        db, context, "approve", data, actor_id, set_id=set_id
    )
    if replay is not None:
        row = await db.get(ManuscriptStatementApproval, UUID(replay["approval_id"]))
        return _approval(row), True
    statement_set = await _set(db, collection_id, set_id)
    tip = _tip(await _sets(db, collection_id))
    if tip is None or tip.id != statement_set.id:
        raise HTTPException(status_code=409, detail=SET_CHANGED)
    if data.set_hash != statement_set.set_hash:
        raise HTTPException(status_code=409, detail=SET_CHANGED)
    author = next(
        (
            a
            for a in statement_set.body.get("authors") or []
            if a["author_key"] == data.author_key
        ),
        None,
    )
    if author is None:
        raise HTTPException(status_code=422, detail=UNKNOWN_AUTHOR)
    note = (data.attestation_note or "").strip() or None
    if data.method == "in_app_self" and author.get("user_id") != str(actor_id):
        raise HTTPException(status_code=403, detail=NOT_THE_AUTHOR)
    if data.method == "recorded_attestation" and note is None:
        raise HTTPException(status_code=422, detail=NOTE_REQUIRED)
    row = ManuscriptStatementApproval(
        id=uuid4(),
        statement_set_id=statement_set.id,
        author_key=data.author_key,
        set_hash=statement_set.set_hash,
        method=data.method,
        approved_by_id=actor_id,
        attestation_note=note,
    )
    db.add(row)
    await _flush(db, ALREADY_APPROVED)
    await _append(
        db,
        context,
        event_type="statements.approved",
        subject_id=cast(UUID, statement_set.id),
        actor_id=actor_id,
        payload={
            "statement_set_id": str(statement_set.id),
            "author_key": data.author_key,
            "set_hash": statement_set.set_hash,
            "method": data.method,
            "approval_id": str(row.id),
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    await db.refresh(row)
    return _approval(row), False


def venue_response(row: Any) -> VenueCheckResponse:
    response = cast(VenueCheckResponse, VenueCheckResponse.model_validate(row))
    result = dict(row.result or {})
    return cast(
        VenueCheckResponse,
        response.model_copy(
            update={
                "rules": dict(result.get("rules") or {}),
                "items": [VenueItem(**i) for i in result.get("items") or []],
            }
        ),
    )


async def record_venue_check(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    release: Any,
    members: Mapping[str, bytes],
    anonymized: tuple[Mapping[str, bytes], str] | None,
    *,
    key: str,
    fingerprint: str | None = None,
) -> Any:
    """Run ``generic-icmje-credit/1`` on one release's exact package bytes
    and insert the bound row plus ``venue.checked``; the caller commits.
    Locks this Collection's statements stream."""
    await lock_aggregate_stream(
        db,
        collection_id=_cid(context),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=_cid(context),
    )
    result = rules.check_profile(
        release.snapshot,
        members,
        None if anonymized is None else anonymized[0],
        await identities(db, context),
    )
    row = VenueCheck(
        id=uuid4(),
        collection_id=_cid(context),
        release_id=release.id,
        profile_id=result["profile_id"],
        profile_version=result["profile_version"],
        package_sha256=release.package_sha256,
        anonymized_sha256=None if anonymized is None else anonymized[1],
        result=result,
        status=result["status"],
        checked_by_id=actor_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    payload = {
        "venue_check_id": str(row.id),
        "release_id": str(release.id),
        "profile_id": row.profile_id,
        "profile_version": row.profile_version,
        "package_sha256": row.package_sha256,
        "anonymized_sha256": row.anonymized_sha256,
        "status": row.status,
        "failing_rules": sorted(
            name for name, state in result["rules"].items() if state != "pass"
        ),
    }
    await _append(
        db,
        context,
        event_type="venue.checked",
        subject_id=cast(UUID, row.id),
        actor_id=actor_id,
        payload=payload,
        key=key,
        fingerprint=fingerprint or decision_request_fingerprint(payload),
    )
    return row


async def list_venue_checks(
    db: AsyncSession, context: ProjectContext, release_id: UUID
) -> VenueCheckListResponse:
    rows = await _all(
        db,
        select(VenueCheck)
        .where(
            VenueCheck.collection_id == _cid(context),
            VenueCheck.release_id == release_id,
        )
        .order_by(VenueCheck.created_at, VenueCheck.id),
    )
    return VenueCheckListResponse(checks=[venue_response(r) for r in rows])


# --- ORCID (a user identity receipt, never a project decision) ---


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _signature(payload: str) -> str:
    key = str(settings.SECRET_KEY).encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def sign_state(user_id: UUID, now: float | None = None) -> str:
    """``base64url(user_id.nonce.exp) + "." + HMAC-SHA256(SECRET_KEY, ...)``."""
    expires = int((time.time() if now is None else now) + STATE_TTL_SECONDS)
    payload = _b64(f"{user_id}.{secrets.token_hex(16)}.{expires}".encode())
    return f"{payload}.{_signature(payload)}"


def verify_state(state: str, user_id: UUID, now: float | None = None) -> bool:
    """Signature, expiry and the session user (constant-time compare)."""
    payload, _, signature = state.partition(".")
    if not payload or not hmac.compare_digest(signature, _signature(payload)):
        return False
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        owner, _nonce, expires = raw.split(".")
        live = int(expires) > (time.time() if now is None else now)
    except ValueError:
        return False
    return live and owner == str(user_id)


def _environment() -> str:
    return "sandbox" if "sandbox." in settings.ORCID_BASE_URL else "production"


def _redirect_uri() -> str:
    base = settings.FRONTEND_BASE_URL or next(iter(settings.cors_origins_list), "")
    return f"{base.rstrip('/')}/auth/orcid/callback"


def _require_configured() -> None:
    if not settings.ORCID_CLIENT_ID:
        raise HTTPException(status_code=503, detail=ORCID_NOT_CONFIGURED)


def orcid_start(user_id: UUID) -> OrcidStartResponse:
    """The ORCID ``/authenticate`` authorize URL with a signed state."""
    _require_configured()
    query = urlencode(
        {
            "client_id": settings.ORCID_CLIENT_ID,
            "response_type": "code",
            "scope": "/authenticate",
            "redirect_uri": _redirect_uri(),
            "state": sign_state(user_id),
        }
    )
    base = settings.ORCID_BASE_URL.rstrip("/")
    return OrcidStartResponse(authorize_url=f"{base}/oauth/authorize?{query}")


async def _exchange(code: str) -> Mapping[str, Any]:
    """The only outbound call in GOO-316: ORCID's token endpoint."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{settings.ORCID_BASE_URL.rstrip('/')}/oauth/token",
            data={
                "client_id": settings.ORCID_CLIENT_ID,
                "client_secret": settings.ORCID_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
            headers={"Accept": "application/json"},
        )
    response.raise_for_status()
    return cast(Mapping[str, Any], response.json())


async def record_orcid(
    db: AsyncSession,
    user_id: UUID,
    token_response: Mapping[str, Any],
    *,
    environment: str,
    client_id: str,
    state_hash: str,
) -> OrcidAuthentication:
    """Insert one receipt from ``orcid``, ``name`` and ``scope`` only (the
    ID token as its sha256); every other field, tokens included, is dropped
    before insert. Commits. ``ValueError`` for a malformed iD."""
    orcid = str(token_response.get("orcid") or "")
    if not rules.ORCID_PATTERN.match(orcid):
        raise ValueError("malformed ORCID iD")
    name = token_response.get("name")
    id_token = token_response.get("id_token")
    row = OrcidAuthentication(
        id=uuid4(),
        user_id=user_id,
        orcid=orcid,
        environment=environment,
        scope=str(token_response.get("scope") or "")[:64],
        name_claim=name[:255] if isinstance(name, str) and name else None,
        client_id=client_id[:64],
        token_received_at=datetime.now(timezone.utc),
        id_token_sha256=(
            hashlib.sha256(id_token.encode()).hexdigest()
            if isinstance(id_token, str) and id_token
            else None
        ),
        flow_state_sha256=state_hash,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def orcid_callback(
    db: AsyncSession,
    user_id: UUID,
    code: str | None,
    state: str | None,
    error: str | None,
) -> OrcidAuthenticationResponse:
    """Verify the state for this session user, exchange the code, keep the
    receipt. Every failure is one safe code, never ORCID's raw text."""
    _require_configured()
    if error:
        raise HTTPException(status_code=400, detail=ORCID_DENIED)
    if not code or not state or not verify_state(state, user_id):
        raise HTTPException(status_code=400, detail=ORCID_STATE_INVALID)
    try:
        token_response = await _exchange(code)
        row = await record_orcid(
            db,
            user_id,
            token_response,
            environment=_environment(),
            client_id=settings.ORCID_CLIENT_ID,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
        )
    except (httpx.HTTPError, ValueError) as failure:
        raise HTTPException(status_code=502, detail=ORCID_EXCHANGE_FAILED) from failure
    return cast(
        OrcidAuthenticationResponse, OrcidAuthenticationResponse.model_validate(row)
    )


async def list_orcid(
    db: AsyncSession, user_id: UUID
) -> OrcidAuthenticationListResponse:
    """The current user's receipts only."""
    rows = await _all(
        db,
        select(OrcidAuthentication)
        .where(OrcidAuthentication.user_id == user_id)
        .order_by(OrcidAuthentication.token_received_at),
    )
    return OrcidAuthenticationListResponse(
        authentications=[
            cast(
                OrcidAuthenticationResponse,
                OrcidAuthenticationResponse.model_validate(r),
            )
            for r in rows
        ]
    )
