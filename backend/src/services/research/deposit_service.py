"""Resumable archive deposits of verified manuscript releases (GOO-318).

API writers (approve, revoke, request, requeue) follow the release writers'
lock order: the route's ``resolve_project`` (RELEASE), then this Collection's
``research_deposit`` stream; one commit each. An approval binds the exact
release, package hash, repository, account label and action ``publish``;
holding a project role never authorizes by itself, and the requester may
never be the approver.

The worker (``drain``) never talks to Zenodo without a durable intent:

1. claim the outbox row with a conditional UPDATE and commit;
2. recheck the requester's access and the approval in force (no remote call
   when either fails), then end that transaction;
3. reconcile by the known remote id (or the operation marker) before any
   create, and call Zenodo for exactly one phase;
4. insert the attempt with every remote id the response carried, append
   ``deposit.phase_recorded`` and commit.

A crash or failed commit after step 3 leaves the claim ``processing``; after
``STALE_CLAIM`` it is reclaimed and step 3's reconcile recovers the remote
ids, so a retry can never create a second deposition. A timeout or 5xx is
recorded as outcome ``unknown`` (the remote side may have acted), never
``failed``. Protocol registration (``protocol_service``) is a separate
receipt table; this module copies its idempotency rule, not its code.

# ponytail: out of scope, each added at its seam when needed: production
# Zenodo (``ZENODO_SANDBOX_ONLY``), new-version deposits for a superseding
# release (``POST /deposit/depositions/{id}/actions/newversion``, GOO-320),
# editing published metadata, embargo/restricted access, per-user Zenodo
# OAuth (one service-account label today), and deleting a remote draft on
# revocation (revocation only blocks further remote calls).
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.manuscript_release import ManuscriptRelease
from src.models.research_deposit import (
    ArchiveDepositApproval,
    ArchiveDepositAttempt,
    ArchiveDepositOutbox,
)
from src.models.research_project_role import ResearchProjectRole
from src.services.research import deposit_rules as rules
from src.services.research import manuscript_release_service as releases
from src.services.research.archives import zenodo
from src.services.research.archives.zenodo import ZenodoAdapter, ZenodoError
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.deposit_schemas import (
    DepositApprovalCreate,
    DepositApprovalResponse,
    DepositApprovalRevoke,
    DepositAttemptResponse,
    DepositCreate,
    DepositFile,
    DepositListResponse,
    DepositResponse,
)

logger = logging.getLogger(__name__)

AGGREGATE_TYPE = "research_deposit"
SUBJECT_TYPE = "archive_deposit"
EXPORT_SCHEMA = "nous.academic.deposits.v1"
MAX_ATTEMPTS = 5
STALE_CLAIM = timedelta(minutes=5)
PACKAGE_NAME = "package.zip"
REFERENCES_NAME = "references.csl.json"

NOT_CONFIGURED = "Archive deposits are not configured"
NOT_VERIFIED = "Release is not verified"
RELEASE_CHANGED = "Release package changed; reload"
NO_APPROVAL = "No valid deposit approval"
SELF_APPROVAL = "The requester cannot approve their own deposit"
APPROVAL_NOT_FOUND = "Approval not found"
NOT_IN_FORCE = "Approval is not in force"
DEPOSIT_NOT_FOUND = "Deposit not found"
METADATA_INCOMPLETE = "metadata_incomplete"

APPROVAL_INVALID = "approval_invalid"
PROJECT_UNAVAILABLE = "project_unavailable"
REQUESTER_UNAUTHORIZED = "requester_unauthorized"
RECONCILED = "reconciled"
READBACK_MISMATCH = "readback_mismatch"


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _role(context: ProjectContext) -> str:
    if ResearchProjectRole.ADJUDICATOR in context.effective_roles:
        return "adjudicator"
    return "supervisor"


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


# --- Reads ---


async def _approvals(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(ArchiveDepositApproval)
        .where(ArchiveDepositApproval.collection_id == collection_id)
        .order_by(ArchiveDepositApproval.created_at, ArchiveDepositApproval.id),
    )


def _approval_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "package_sha256": row.package_sha256,
        "account_ref": row.account_ref,
        "action": row.action,
        "repository": row.repository,
        "approved_by_id": row.approved_by_id,
    }


def _in_force(
    approvals: Sequence[Any], release_id: Any, package_sha256: str, account: str
) -> Mapping[str, Any] | None:
    scoped = [_approval_dict(a) for a in approvals if a.release_id == release_id]
    return rules.valid_approval(scoped, package_sha256, account)


def _chains(rows: Sequence[Any]) -> list[list[Any]]:
    """Each operation's attempts in chain order (``previous_id`` links)."""
    by_previous = {r.previous_id: r for r in rows if r.previous_id is not None}
    chains = []
    for root in (r for r in rows if r.phase == "prepared"):
        chain = [root]
        while chain[-1].id in by_previous:
            chain.append(by_previous[chain[-1].id])
        chains.append(chain)
    return chains


def _attempt(row: Any) -> rules.Attempt:
    response = row.response if isinstance(row.response, Mapping) else {}
    return rules.Attempt(
        id=str(row.id),
        phase=row.phase,
        outcome=row.outcome,
        retryable=bool(row.retryable),
        remote_deposition_id=row.remote_deposition_id,
        remote_record_id=row.remote_record_id,
        doi=row.doi,
        submitted=response.get("submitted") is True,
        reason=row.reason,
        files=list(row.files or []),
    )


async def _chain(db: AsyncSession, operation_id: UUID) -> list[Any]:
    rows = await _all(
        db,
        select(ArchiveDepositAttempt).where(
            ArchiveDepositAttempt.operation_id == operation_id
        ),
    )
    chains = _chains(rows)
    return chains[0] if chains else []


def _package_sha(root: Any) -> str:
    """The exact release package hash the operation prepared."""
    return next(str(f["sha256"]) for f in root.files if f["name"] == PACKAGE_NAME)


def _confirmed(chain: Sequence[Any], phase: str) -> Any:
    return next(
        (r for r in reversed(chain) if r.phase == phase and r.outcome == "succeeded"),
        None,
    )


def _response(
    chain: Sequence[Any], approvals: Sequence[Any], queue: str | None
) -> DepositResponse:
    root, attempts = chain[0], [_attempt(r) for r in chain]
    status = rules.operation_status(attempts)
    in_force = _in_force(
        approvals, root.release_id, _package_sha(root), root.account_ref
    )
    remote = rules.latest_remote(attempts)
    published = (
        _confirmed(chain, "published") if status in ("published", "verified") else None
    )
    verified = _confirmed(chain, "verified") if status == "verified" else None
    doi = None if verified is None else verified.doi
    return DepositResponse(
        operation_id=root.id,
        release_id=root.release_id,
        package_sha256=_package_sha(root),
        repository=root.repository,
        account_ref=root.account_ref,
        requested_by_id=root.requested_by_id,
        approval_id=None if in_force is None else in_force["id"],
        approval_in_force=in_force is not None,
        status=cast(Any, status),
        last_reason=chain[-1].reason if len(chain) > 1 else None,
        files=[DepositFile.model_validate(f) for f in root.files],
        remote_deposition_id=None if remote is None else remote.remote_deposition_id,
        remote_record_id=None if published is None else published.remote_record_id,
        doi=doi,
        doi_url=None if doi is None else f"https://doi.org/{doi}",
        queue_status=queue,
        attempts=[
            DepositAttemptResponse(
                id=r.id,
                phase=r.phase,
                outcome=r.outcome,
                retryable=r.retryable,
                remote_deposition_id=r.remote_deposition_id,
                remote_record_id=r.remote_record_id,
                reason=r.reason,
                previous_id=r.previous_id,
                created_at=r.created_at,
                mismatch=list((r.response or {}).get("mismatch") or []),
            )
            for r in chain
        ],
        created_at=root.created_at,
    )


def _approval_response(
    row: Any, approvals: Sequence[Any], releases_by_id: Mapping[Any, Any]
) -> DepositApprovalResponse:
    release = releases_by_id.get(row.release_id)
    in_force = None
    if release is not None:
        in_force = _in_force(
            approvals, row.release_id, release.package_sha256, row.account_ref
        )
    response = cast(
        DepositApprovalResponse, DepositApprovalResponse.model_validate(row)
    )
    return cast(
        DepositApprovalResponse,
        response.model_copy(
            update={"in_force": in_force is not None and in_force["id"] == row.id}
        ),
    )


async def list_deposits(
    db: AsyncSession, context: ProjectContext
) -> DepositListResponse:
    """VIEW: every operation with its chain and derived status, and every
    approval with whether it is in force. DOIs only once verified."""
    collection_id = _cid(context)
    approvals = await _approvals(db, collection_id)
    rows = await _all(
        db,
        select(ArchiveDepositAttempt)
        .where(ArchiveDepositAttempt.collection_id == collection_id)
        .order_by(ArchiveDepositAttempt.created_at, ArchiveDepositAttempt.id),
    )
    chains = _chains(rows)
    queue: dict[Any, str] = {}
    if chains:
        outbox = await db.execute(
            select(
                ArchiveDepositOutbox.operation_id, ArchiveDepositOutbox.status
            ).where(ArchiveDepositOutbox.operation_id.in_([c[0].id for c in chains]))
        )
        queue = {operation: state for operation, state in outbox.all()}
    release_ids = {a.release_id for a in approvals}
    releases_by_id = {
        r.id: r
        for r in (
            await _all(
                db,
                select(ManuscriptRelease).where(ManuscriptRelease.id.in_(release_ids)),
            )
            if release_ids
            else []
        )
    }
    label = settings.ZENODO_ACCOUNT_LABEL
    return DepositListResponse(
        configured=zenodo.from_settings() is not None,
        account_ref=zenodo.account_ref() if label else None,
        approvals=[_approval_response(a, approvals, releases_by_id) for a in approvals],
        deposits=[_response(c, approvals, queue.get(c[0].id)) for c in chains],
    )


async def export_part(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``deposits.json`` body (redacted rows only)."""
    listing = await list_deposits(db, context)
    return {"project_id": str(_cid(context)), **listing.model_dump(mode="json")}


# --- API writers ---


async def _begin(
    db: AsyncSession,
    context: ProjectContext,
    operation: str,
    data: Any,
    actor_id: UUID,
    **ids: Any,
) -> tuple[str, str, dict[str, Any] | None]:
    """Lock the stream, then (key, fingerprint, replayed payload)."""
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
    collection_id: UUID,
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


async def _verified_release(
    db: AsyncSession, context: ProjectContext, release_id: UUID, package_sha256: str
) -> Any:
    """The release, which must derive ``verified`` (not stale) and still
    carry the package hash the caller saw."""
    row = await releases._release(db, _cid(context), release_id)
    view = (await releases._responses(db, _cid(context), [row]))[0]
    if row.stage != "verified" or view.status != "verified":
        raise HTTPException(status_code=409, detail=NOT_VERIFIED)
    if row.package_sha256 != package_sha256:
        raise HTTPException(status_code=409, detail=RELEASE_CHANGED)
    return row


def _require_configured() -> None:
    if zenodo.from_settings() is None:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)


async def _live_operation(db: AsyncSession, release_id: UUID) -> Any:
    return (
        await db.execute(
            select(ArchiveDepositAttempt).where(
                ArchiveDepositAttempt.release_id == release_id,
                ArchiveDepositAttempt.repository == rules.REPOSITORY,
                ArchiveDepositAttempt.phase == "prepared",
            )
        )
    ).scalar_one_or_none()


async def _one(db: AsyncSession, collection_id: UUID, operation_id: UUID) -> Any:
    chain = await _chain(db, operation_id)
    if not chain or chain[0].collection_id != collection_id:
        raise HTTPException(status_code=404, detail=DEPOSIT_NOT_FOUND)
    approvals = await _approvals(db, collection_id)
    queue = (
        await db.execute(
            select(ArchiveDepositOutbox.status).where(
                ArchiveDepositOutbox.operation_id == operation_id
            )
        )
    ).scalar_one_or_none()
    return _response(chain, approvals, queue)


async def _approval_view(db: AsyncSession, collection_id: UUID, row: Any) -> Any:
    approvals = await _approvals(db, collection_id)
    release = await db.get(ManuscriptRelease, row.release_id)
    return _approval_response(row, approvals, {row.release_id: release})


async def approve(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: DepositApprovalCreate,
) -> tuple[DepositApprovalResponse, bool]:
    """RELEASE: approve depositing this exact release package to the
    configured sandbox account; commits once."""
    _require_configured()
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, context, "approve", data, actor_id)
    if replay is not None:
        row = await db.get(ArchiveDepositApproval, UUID(replay["approval_id"]))
        return await _approval_view(db, collection_id, row), True
    release = await _verified_release(db, context, data.release_id, data.package_sha256)
    live = await _live_operation(db, cast(UUID, release.id))
    if live is not None and live.requested_by_id == actor_id:
        raise HTTPException(status_code=422, detail=SELF_APPROVAL)
    row = ArchiveDepositApproval(
        id=uuid4(),
        collection_id=collection_id,
        release_id=release.id,
        package_sha256=release.package_sha256,
        repository=rules.REPOSITORY,
        account_ref=zenodo.account_ref(),
        action=rules.ACTION,
        kind="approved",
        approved_by_id=actor_id,
        actor_role=_role(context),
        rationale=data.rationale,
    )
    await _insert_approval(db, row, key, fingerprint)
    await db.commit()
    return await _approval_view(db, collection_id, row), False


async def _insert_approval(
    db: AsyncSession, row: ArchiveDepositApproval, key: str, fingerprint: str
) -> None:
    db.add(row)
    await db.flush()
    await _append(
        db,
        cast(UUID, row.collection_id),
        event_type=f"deposit.{row.kind}",
        subject_id=cast(UUID, row.id),
        actor_id=cast(UUID, row.approved_by_id),
        actor_role=cast(str, row.actor_role),
        payload={
            "approval_id": str(row.id),
            "release_id": str(row.release_id),
            "package_sha256": row.package_sha256,
            "repository": row.repository,
            "account_ref": row.account_ref,
            "action": row.action,
        },
        key=key,
        fingerprint=fingerprint,
        reason=cast(str, row.rationale),
    )


async def revoke(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    approval_id: UUID,
    data: DepositApprovalRevoke,
) -> tuple[DepositApprovalResponse, bool]:
    """RELEASE: an insert-only revocation of the approval in force. A
    published deposition cannot be revoked remotely; this blocks every
    further remote call for the release."""
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(
        db, context, "revoke", data, actor_id, approval_id=approval_id
    )
    if replay is not None:
        row = await db.get(ArchiveDepositApproval, UUID(replay["approval_id"]))
        return await _approval_view(db, collection_id, row), True
    approvals = await _approvals(db, collection_id)
    target = next((a for a in approvals if a.id == approval_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=APPROVAL_NOT_FOUND)
    in_force = _in_force(
        approvals, target.release_id, target.package_sha256, target.account_ref
    )
    if in_force is None or in_force["id"] != target.id:
        raise HTTPException(status_code=409, detail=NOT_IN_FORCE)
    row = ArchiveDepositApproval(
        id=uuid4(),
        collection_id=collection_id,
        release_id=target.release_id,
        package_sha256=target.package_sha256,
        repository=target.repository,
        account_ref=target.account_ref,
        action=target.action,
        kind="revoked",
        approved_by_id=actor_id,
        actor_role=_role(context),
        rationale=data.rationale,
    )
    await _insert_approval(db, row, key, fingerprint)
    await db.commit()
    return await _approval_view(db, collection_id, row), False


def _file(name: str, data: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "bytes": len(data),
    }


async def _payload(release: Any) -> tuple[dict[str, bytes], dict[str, Any] | None]:
    """The files to deposit: the exact package bytes (re-hashed) and the
    CSL references (GOO-317), plus the package's sealed statements body."""
    package = await releases._stored_bytes(release)
    if hashlib.sha256(package).hexdigest() != release.package_sha256:
        raise HTTPException(status_code=409, detail=releases.PACKAGE_CHANGED)
    members = releases._members(package)
    csl = releases.reference_file(release.snapshot, "csl-json")[0]
    sealed = members.get("references.json")
    if sealed is not None and json.loads(sealed)["body"] != json.loads(csl):
        raise HTTPException(status_code=409, detail=releases.PACKAGE_CHANGED)
    statements = members.get("statements.json")
    body = None if statements is None else json.loads(statements)["body"]
    return {PACKAGE_NAME: package, REFERENCES_NAME: csl.encode("utf-8")}, body


async def request_deposit(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: DepositCreate
) -> tuple[DepositResponse, bool]:
    """RELEASE: record the ``prepared`` attempt and its outbox row; commits
    once. One live operation per (release, repository): a second request
    for the same release returns the existing operation."""
    _require_configured()
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, context, "request", data, actor_id)
    if replay is not None:
        return await _one(db, collection_id, UUID(replay["operation_id"])), True
    release = await _verified_release(db, context, data.release_id, data.package_sha256)
    account = zenodo.account_ref()
    approval = _in_force(
        await _approvals(db, collection_id),
        release.id,
        cast(str, release.package_sha256),
        account,
    )
    if approval is None:
        raise HTTPException(status_code=409, detail=NO_APPROVAL)
    if approval["approved_by_id"] == actor_id:
        raise HTTPException(status_code=422, detail=SELF_APPROVAL)
    if await _access_failure(db, collection_id, cast(UUID, approval["approved_by_id"])):
        raise HTTPException(status_code=409, detail=NO_APPROVAL)
    payload, statements = await _payload(release)
    title = (release.snapshot.get("draft") or {}).get("title")
    metadata, missing = rules.zenodo_metadata(
        title, cast(str, release.package_sha256), statements
    )
    if missing:
        raise HTTPException(
            status_code=409, detail=f"{METADATA_INCOMPLETE}:{','.join(missing)}"
        )
    operation_id = uuid4()
    files = [_file(name, data) for name, data in payload.items()]
    row = ArchiveDepositAttempt(
        id=operation_id,
        collection_id=collection_id,
        operation_id=operation_id,
        release_id=release.id,
        repository=rules.REPOSITORY,
        account_ref=account,
        phase="prepared",
        outcome="succeeded",
        retryable=False,
        idempotency_key=data.idempotency_key,
        request_fingerprint=fingerprint,
        requested_by_id=actor_id,
        approval_id=approval["id"],
        files=files,
        request=rules.redact(
            {
                "metadata": metadata,
                "marker": rules.operation_marker(operation_id),
                "package_sha256": release.package_sha256,
            }
        ),
        response={},
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        # uq_deposit_live: one operation per release; the other request wins.
        await db.rollback()
        if not _is_unique_violation(error):
            raise
        winner = await _live_operation(db, data.release_id)
        if winner is None:
            raise
        return await _one(db, collection_id, cast(UUID, winner.id)), True
    db.add(ArchiveDepositOutbox(operation_id=operation_id))
    await _append(
        db,
        collection_id,
        event_type="deposit.requested",
        subject_id=operation_id,
        actor_id=actor_id,
        actor_role=_role(context),
        payload={
            "operation_id": str(operation_id),
            "release_id": str(release.id),
            "package_sha256": release.package_sha256,
            "repository": rules.REPOSITORY,
            "account_ref": account,
            "approval_id": str(approval["id"]),
            "file_count": len(files),
        },
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return await _one(db, collection_id, operation_id), False


async def requeue(
    db: AsyncSession, context: ProjectContext, operation_id: UUID
) -> DepositResponse:
    """RELEASE: rebuild a missing, done or skipped outbox row from the chain.
    The chain is untouched; the worker rechecks everything."""
    collection_id = _cid(context)
    await _one(db, collection_id, operation_id)
    reset = await db.execute(
        update(ArchiveDepositOutbox)
        .where(
            ArchiveDepositOutbox.operation_id == operation_id,
            ArchiveDepositOutbox.status != "processing",
        )
        .values(status="pending", attempts=0, last_error=None, updated_at=_now())
    )
    if cast(CursorResult[Any], reset).rowcount == 0:
        exists = await db.scalar(
            select(ArchiveDepositOutbox.id).where(
                ArchiveDepositOutbox.operation_id == operation_id
            )
        )
        if exists is None:
            db.add(ArchiveDepositOutbox(operation_id=operation_id))
    await db.commit()
    return cast(DepositResponse, await _one(db, collection_id, operation_id))


# --- Worker ---


async def _access_failure(
    db: AsyncSession, collection_id: UUID, user_id: UUID
) -> str | None:
    """None while ``user_id`` may still RELEASE in this project."""
    try:
        await resolve_project(db, collection_id, user_id, ResearchAction.RELEASE)
    except HTTPException as error:
        return "forbidden" if error.status_code == 403 else PROJECT_UNAVAILABLE
    return None


async def _precheck(db: AsyncSession, root: Any) -> tuple[str | None, Any]:
    """(failure reason, approval in force). The requester must still hold
    RELEASE on a live project, and the approval in force must still bind
    this exact release, package, account and action, with its approver
    still holding RELEASE. Checked before every remote call."""
    collection_id = cast(UUID, root.collection_id)
    failure = await _access_failure(db, collection_id, root.requested_by_id)
    if failure is not None:
        if failure == "forbidden":
            return REQUESTER_UNAUTHORIZED, None
        return PROJECT_UNAVAILABLE, None
    approval = _in_force(
        await _approvals(db, collection_id),
        root.release_id,
        _package_sha(root),
        root.account_ref,
    )
    if (
        approval is None
        or approval["approved_by_id"] == root.requested_by_id
        or await _access_failure(db, collection_id, approval["approved_by_id"])
    ):
        return APPROVAL_INVALID, None
    return None, approval


class _Recorder:
    """Appends attempts to one operation's chain (the caller commits)."""

    def __init__(self, db: AsyncSession, root: Any, tip: Any, approval_id: Any):
        self.db, self.root, self.tip = db, root, tip
        self.approval_id = approval_id

    async def record(
        self,
        phase: str,
        outcome: str,
        *,
        retryable: bool = False,
        remote: Mapping[str, Any] | None = None,
        doi: str | None = None,
        files: Sequence[Mapping[str, Any]] = (),
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> Any:
        root = self.root
        await lock_aggregate_stream(
            self.db,
            collection_id=root.collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=root.collection_id,
        )
        remote = remote or {}
        row = ArchiveDepositAttempt(
            id=uuid4(),
            collection_id=root.collection_id,
            operation_id=root.id,
            release_id=root.release_id,
            repository=root.repository,
            account_ref=root.account_ref,
            phase=phase,
            outcome=outcome,
            retryable=retryable,
            requested_by_id=root.requested_by_id,
            approval_id=self.approval_id,
            files=list(files),
            remote_deposition_id=remote.get("deposition_id"),
            remote_record_id=remote.get("record_id"),
            doi=doi,
            request=rules.redact(dict(request or {})),
            response=rules.redact(dict(response or {})),
            reason=None if reason is None else reason[:200],
            previous_id=self.tip.id,
        )
        self.db.add(row)
        await self.db.flush()
        payload = {
            "operation_id": str(root.id),
            "attempt_id": str(row.id),
            "previous_id": str(self.tip.id),
            "approval_id": None if self.approval_id is None else str(self.approval_id),
            "phase": phase,
            "outcome": outcome,
            "remote_deposition_id": row.remote_deposition_id,
            "remote_record_id": row.remote_record_id,
            "doi": doi,
        }
        await _append(
            self.db,
            cast(UUID, root.collection_id),
            event_type="deposit.phase_recorded",
            subject_id=cast(UUID, row.id),
            actor_id=cast(UUID, root.requested_by_id),
            actor_role="machine",
            payload=payload,
            key=f"attempt:{row.id}",
            fingerprint=decision_request_fingerprint(payload),
        )
        self.tip = row
        return row

    async def remote_error(
        self, phase: str, error: ZenodoError, known: rules.Attempt | None
    ) -> tuple[str, Any]:
        """A timeout or 5xx may have acted remotely: ``unknown``, never
        ``failed``, so the next step reconciles before re-sending."""
        outcome = "unknown" if error.retryable else "failed"
        return outcome, await self.record(
            phase,
            outcome,
            retryable=error.retryable,
            remote={
                "deposition_id": None if known is None else known.remote_deposition_id
            },
            request={"call": phase},
            response={"error": str(error), "status": error.status},
            reason=str(error),
        )


def _shown(remote: Mapping[str, Any], files: Sequence[Mapping[str, Any]]) -> str:
    """The highest phase a remote deposition proves."""
    if remote["submitted"] and remote["record_id"]:
        return "published"
    present = {f["name"]: f["md5"] for f in remote["files"]}
    if all(present.get(f["name"]) == f["md5"] for f in files):
        return "files_uploaded"
    return "draft_created"


def _remote_files(
    files: Sequence[Mapping[str, Any]], remote: Mapping[str, Any]
) -> list[dict[str, Any]]:
    present = {f["name"]: f["md5"] for f in remote["files"]}
    return [{**f, "remote_md5": present.get(f["name"])} for f in files]


async def _step(
    db: AsyncSession, adapter: ZenodoAdapter, outbox_id: UUID
) -> tuple[str, Any]:
    """One worker step for a claimed outbox row: (kind, recorded attempt or
    None). Raises on a local failure (the claim then goes stale)."""
    outbox = await db.get(ArchiveDepositOutbox, outbox_id, populate_existing=True)
    if outbox is None:
        return "gone", None
    chain = await _chain(db, cast(UUID, outbox.operation_id))
    root, attempts = chain[0], [_attempt(r) for r in chain]
    step = rules.next_phase(attempts)
    if step is None:
        return "done", None
    failure, approval = await _precheck(db, root)
    await db.commit()  # release the access locks before any remote call
    recorder = _Recorder(
        db, root, chain[-1], None if approval is None else approval["id"]
    )
    target = rules.phase_after(attempts) if step == rules.RECONCILE else step
    if failure is not None:
        # No remote call: stale authorization fails before it reaches Zenodo.
        return "blocked", await recorder.record(
            target, "failed", retryable=True, reason=failure
        )
    known = rules.latest_remote(attempts)
    files = list(root.files)
    if step == rules.RECONCILE and known is not None:
        try:
            remote = await adapter.get_deposition(cast(str, known.remote_deposition_id))
        except ZenodoError as error:
            return await recorder.remote_error(target, error, known)
        if remote is not None:
            shown = _shown(remote, files)
            if rules.PHASES.index(shown) >= rules.PHASES.index(target):
                return "progress", await recorder.record(
                    shown,
                    "succeeded",
                    remote=remote,
                    doi=(
                        remote["doi"]
                        if shown == "published"
                        else remote["prereserve_doi"]
                    ),
                    files=_remote_files(files, remote),
                    request={"call": "get_deposition"},
                    response=remote["raw"],
                    reason=RECONCILED,
                )
    # Without a saved remote id, the draft phase's lookup is the reconcile.
    return await _run_phase(adapter, recorder, target, chain, files, known)


async def _run_phase(
    adapter: ZenodoAdapter,
    recorder: _Recorder,
    phase: str,
    chain: Sequence[Any],
    files: Sequence[Mapping[str, Any]],
    known: rules.Attempt | None,
) -> tuple[str, Any]:
    try:
        if phase == "draft_created":
            return await _draft(adapter, recorder)
        deposition_id = cast(rules.Attempt, known).remote_deposition_id
        if phase == "files_uploaded":
            return await _upload(adapter, recorder, cast(str, deposition_id), files)
        if phase == "published":
            remote = await adapter.get_deposition(cast(str, deposition_id))
            if remote is not None and remote["submitted"] and remote["record_id"]:
                published, call = remote, "get_deposition"
            else:
                published, call = (
                    await adapter.publish(cast(str, deposition_id)),
                    "publish",
                )
            done = bool(published["submitted"] and published["record_id"])
            row = await recorder.record(
                "published",
                "succeeded" if done else "unknown",
                retryable=not done,
                remote=published,
                doi=published["doi"],
                request={"call": call},
                response=published["raw"],
                reason=None if done else "not_submitted",
            )
            if not done:
                return "unknown", row
            await recorder.db.commit()
            chain = [*chain, row]
    except ZenodoError as error:
        return await recorder.remote_error(phase, error, known)
    try:
        return await _verify(adapter, recorder, chain, files)
    except ZenodoError as error:
        return await recorder.remote_error("verified", error, known)


async def _draft(adapter: ZenodoAdapter, recorder: _Recorder) -> tuple[str, Any]:
    """The only remote create, always preceded by a lookup for it."""
    root = recorder.root
    marker = rules.operation_marker(root.id)
    found = await adapter.find_by_operation(marker)
    if found is not None:
        return "progress", await recorder.record(
            "draft_created",
            "succeeded",
            remote=found,
            doi=found["prereserve_doi"],
            request={"call": "find_by_operation"},
            response=found["raw"],
            reason=RECONCILED,
        )
    metadata = dict(root.request["metadata"])
    draft = await adapter.create_draft(metadata, marker)
    return "progress", await recorder.record(
        "draft_created",
        "succeeded",
        remote=draft,
        doi=draft["prereserve_doi"],
        request={"call": "create_draft", "metadata": metadata},
        response=draft["raw"],
    )


async def _upload(
    adapter: ZenodoAdapter,
    recorder: _Recorder,
    deposition_id: str,
    files: Sequence[Mapping[str, Any]],
) -> tuple[str, Any]:
    """Upload only the files Zenodo does not already hold with our md5."""
    remote = await adapter.get_deposition(deposition_id)
    if remote is None:
        raise ZenodoError("deposition_not_found", retryable=True, status=404)
    present = {f["name"]: f["md5"] for f in remote["files"]}
    release = await recorder.db.get(ManuscriptRelease, recorder.root.release_id)
    payload, _statements = await _payload(release)
    await recorder.db.commit()
    uploaded = []
    for f in files:
        if present.get(f["name"]) == f["md5"]:
            continue  # already there with the right bytes
        result = await adapter.upload_file(
            cast(str, remote["bucket"]), f["name"], payload[f["name"]]
        )
        uploaded.append(f["name"])
        present[f["name"]] = result["md5"]
    remote = {**remote, "files": [{"name": k, "md5": v} for k, v in present.items()]}
    bad = [f["name"] for f in files if present.get(f["name"]) != f["md5"]]
    return ("progress" if not bad else "failed"), await recorder.record(
        "files_uploaded",
        "succeeded" if not bad else "failed",
        remote=remote,
        files=_remote_files(files, remote),
        request={"call": "upload_file", "uploaded": uploaded},
        response={"files": remote["files"]},
        reason=None if not bad else f"checksum:{bad[0]}",
    )


async def _verify(
    adapter: ZenodoAdapter,
    recorder: _Recorder,
    chain: Sequence[Any],
    files: Sequence[Mapping[str, Any]],
) -> tuple[str, Any]:
    """``GET /records/{id}`` read-back against what we prepared: the file set,
    every md5, the pre-reserved DOI and the published record id."""
    published = _confirmed(chain, "published")
    draft = _confirmed(chain, "draft_created")
    record_id = cast(str, published.remote_record_id)
    record = await adapter.get_record(record_id)
    if record is None:
        raise ZenodoError("record_not_found", retryable=True, status=404)
    mismatch = rules.check_readback(
        files,
        record,
        expected_doi=None if draft is None else draft.doi,
        expected_record_id=record_id,
    )
    if published.doi != record["doi"] and "doi" not in mismatch:
        mismatch.append("doi")
    return ("progress" if not mismatch else "failed"), await recorder.record(
        "verified",
        "succeeded" if not mismatch else "failed",
        remote={
            "deposition_id": published.remote_deposition_id,
            "record_id": record_id,
        },
        doi=record["doi"] if not mismatch else None,
        files=_remote_files(files, record),
        request={"call": "get_record", "record_id": record_id},
        response={"record": record["raw"], "mismatch": mismatch},
        reason=None if not mismatch else READBACK_MISMATCH,
    )


def _eligible(now: datetime) -> Any:
    return or_(
        ArchiveDepositOutbox.status == "pending",
        and_(
            ArchiveDepositOutbox.status == "processing",
            ArchiveDepositOutbox.updated_at < now - STALE_CLAIM,
        ),
    )


async def drain(
    db: AsyncSession,
    adapter: ZenodoAdapter,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    """Advance up to ``limit`` claimed operations by one step each; returns
    how many steps recorded an attempt. The ``drain_artifact_outbox`` claim:
    a conditional UPDATE committed before any work; a stale ``processing``
    claim (worker died) is eligible again."""
    now = now or _now()
    candidates = (
        await db.scalars(
            select(ArchiveDepositOutbox.id)
            .where(_eligible(now))
            .order_by(ArchiveDepositOutbox.created_at)
            .limit(limit)
        )
    ).all()
    steps = 0
    for outbox_id in candidates:
        claimed = await db.execute(
            update(ArchiveDepositOutbox)
            .where(ArchiveDepositOutbox.id == outbox_id, _eligible(now))
            .values(
                status="processing",
                attempts=ArchiveDepositOutbox.attempts + 1,
                updated_at=now,
            )
        )
        await db.commit()
        if cast(CursorResult[Any], claimed).rowcount != 1:
            continue  # another worker claimed it
        try:
            kind, row = await _step(db, adapter, outbox_id)
        except Exception as error:  # noqa: BLE001 - keep draining other rows
            # A local failure (crash, failed commit) after a possible remote
            # success: the claim stays ``processing`` and is reclaimed after
            # STALE_CLAIM; the next step reconciles by remote id.
            logger.warning("archive deposit step failed", exc_info=error)
            await db.rollback()
            await db.execute(
                update(ArchiveDepositOutbox)
                .where(ArchiveDepositOutbox.id == outbox_id)
                .values(last_error=type(error).__name__[:200])
            )
            await db.commit()
            continue
        claims = await db.scalar(
            select(ArchiveDepositOutbox.attempts).where(
                ArchiveDepositOutbox.id == outbox_id
            )
        )
        if row is not None:
            steps += 1
        values: dict[str, Any] = {
            "last_error": None if row is None else row.reason,
            "updated_at": _now(),
        }
        if kind in ("done", "blocked", "failed", "gone") or (
            row is not None and row.phase == "verified"
        ):
            values["status"] = "done"
        elif kind == "progress":
            values.update(status="pending", attempts=0)
        else:  # unknown: bounded retries, then a person decides (requeue)
            values["status"] = "skipped" if (claims or 0) >= MAX_ATTEMPTS else "pending"
        await db.execute(
            update(ArchiveDepositOutbox)
            .where(ArchiveDepositOutbox.id == outbox_id)
            .values(**values)
        )
        await db.commit()
    return steps
