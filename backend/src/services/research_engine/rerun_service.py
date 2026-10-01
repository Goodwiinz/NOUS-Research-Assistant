"""Fresh reruns of one manifested workflow (GOO-313).

The only writer of ``experiment_reruns`` and ``experiment_rerun_attempts``
(both insert-only). A rerun restores a completed run's archived inputs, code
and environment lock (never live documents) into a new isolated sandbox
(``SandboxManager.run_isolated`` with a ``RestoreSpec``; never the
conversation cache), runs the recorded command and compares every output
under a rule declared and hashed at admission.

Publication is the terminal attempt row: ``UNIQUE(rerun_id, attempt)`` gives a
cancel, the interrupt sweep and a late worker exactly one winner, and the
loser discards its outputs (compensating blob delete). Attempt claims live in
the ``research_reproduction`` ledger (``rerun.attempt_started`` carries the
lease), so no row is ever updated.

Lock order: admission, cancel and retry take the route's ``resolve_project``
(Workspace SHARE -> Collection UPDATE), then this Collection's
``research_reproduction`` stream. The worker takes ``lock_active_project``
(Collection UPDATE) then the stream for its claim, and only the stream for
its terminal insert; the sweep takes only the stream.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_experiment import ResearchRunArtifact, ResearchRunManifest
from src.models.research_rerun import ExperimentRerun, ExperimentRerunAttempt
from src.models.research_run import ResearchRun
from src.schemas.research_engine import (
    RerunAttemptResponse,
    RerunCreate,
    RerunEligibilityResponse,
    RerunListResponse,
    RerunOutputResponse,
    RerunResponse,
)
from src.services.artifacts.storage import get_artifact_storage
from src.services.research_decisions import (
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import manifest_rules
from src.services.research_engine import rerun_rules as rules
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    lock_active_project,
    resolve_engine_project_context,
)
from src.services.research_engine.run_conformance import require_run_conformance
from src.services.research_engine.screening_service import _is_unique_violation
from src.services.sandbox.e2b_sandbox_manager import (
    MAX_EXECUTION_TIMEOUT,
    IsolatedSpec,
    RestoreSpec,
    get_sandbox_manager,
)

AGGREGATE_TYPE = "research_reproduction"
SUBJECT_TYPE = "experiment_rerun"
EXPORT_SCHEMA = "nous.academic.reproduction.v1"
# The restore install and the command each get MAX_EXECUTION_TIMEOUT, plus
# four 60 s probes (freeze, python, sha256sum, mkdir) and slack. An expired
# lease is swept to ``interrupted``; a slower worker then loses the insert.
LEASE_SECONDS = 2 * MAX_EXECUTION_TIMEOUT + 360

NOT_ELIGIBLE = "Run is not eligible for rerun"
RERUN_NOT_FOUND = "Rerun not found"
ATTEMPT_NOT_FOUND = "Rerun attempt not found"
OUTPUT_NOT_FOUND = "Rerun output not found"
OUTPUT_CORRUPT = "Rerun output bytes do not match their hash"
NOT_RETRYABLE = "Rerun can be retried only after a terminal, unexecuted attempt"

SessionFactory = Callable[[], Any]


class RerunIneligible(Exception):
    """Admission refused; the route answers 409 with ``reasons``."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(NOT_ELIGIBLE)
        self.reasons = reasons


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _started_key(rerun_id: Any, attempt: int) -> str:
    return f"rerun:{rerun_id}:{attempt}:started"


def _output_key(organization_id: Any, rerun_id: Any, attempt: int, sha: str) -> str:
    """Private, content-addressed per attempt; never in a DTO."""
    return f"artifacts/{organization_id}/research-reruns/{rerun_id}/{attempt}/{sha}"


# --- Loading ---------------------------------------------------------------------


async def run_context(
    db: AsyncSession, run: Any, user_id: UUID, action: ResearchAction
) -> tuple[ProjectContext, ResearchBlueprint]:
    blueprint = await db.get(ResearchBlueprint, run.blueprint_id)
    if blueprint is None or blueprint.is_deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    context = await resolve_engine_project_context(
        db, cast(UUID, blueprint.project_id), user_id, action
    )
    return context, blueprint


async def _manifest(db: AsyncSession, run_id: Any) -> Any:
    return (
        await db.execute(
            select(ResearchRunManifest).where(ResearchRunManifest.run_id == run_id)
        )
    ).scalar_one_or_none()


async def _artifacts(db: AsyncSession, run_id: Any) -> list[Any]:
    return list(
        (
            await db.execute(
                select(ResearchRunArtifact)
                .where(ResearchRunArtifact.run_id == run_id)
                .order_by(ResearchRunArtifact.role, ResearchRunArtifact.name)
            )
        )
        .scalars()
        .all()
    )


def _required(manifest: Mapping[str, Any] | None) -> list[str]:
    """Every archived file a rerun restores or compares against."""
    if manifest is None:
        return []
    return [
        manifest_rules.CODE_NAME,
        manifest_rules.LOCK_NAME,
        *(str(i.get("name")) for i in manifest.get("inputs") or []),
        *(str(o.get("name")) for o in manifest.get("outputs") or []),
    ]


async def _archived(
    rows: Sequence[Any], required: Sequence[str]
) -> tuple[dict[tuple[str, str], bytes], dict]:
    """Archived bytes by (role, name), re-hashed on read, and the
    eligibility view ``name -> (exists, hash_ok)``; a required file with no
    archived row is missing."""
    storage = get_artifact_storage()
    data: dict[tuple[str, str], bytes] = {}
    checks: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        blob = await storage.get(cast(str, row.storage_key))
        exists = blob is not None
        ok = exists and manifest_rules.sha256_hex(blob or b"") == row.sha256
        prior = checks.get(row.name, (True, True))
        checks[row.name] = (prior[0] and exists, prior[1] and ok)
        if ok:
            data[(str(row.role), str(row.name))] = cast(bytes, blob)
    for name in required:
        checks.setdefault(name, (False, False))
    return data, checks


def _analyze_steps(steps: Any) -> int:
    return sum(
        1
        for step in steps or []
        if isinstance(step, Mapping) and step.get("type") == manifest_rules.STEP_TYPE
    )


async def _eligibility(
    db: AsyncSession, run: Any, blueprint: Any, manifest_row: Any
) -> list[str]:
    rows = await _artifacts(db, run.id)
    manifest = None if manifest_row is None else manifest_row.manifest
    _, checks = await _archived(rows, _required(manifest))
    return rules.eligibility(
        run_status=str(run.status),
        conformance=str(run.conformance_status),
        manifest=None if manifest_row is None else manifest_row.manifest,
        analyze_steps=_analyze_steps(blueprint.steps),
        artifacts=checks,
    )


async def eligibility(
    db: AsyncSession, run: Any, user_id: UUID
) -> RerunEligibilityResponse:
    """VIEW, zero writes: the structured reasons and the default rule."""
    _, blueprint = await run_context(db, run, user_id, ResearchAction.VIEW)
    manifest_row = await _manifest(db, run.id)
    reasons = await _eligibility(db, run, blueprint, manifest_row)
    return RerunEligibilityResponse(
        eligible=not reasons,
        reasons=reasons,
        default_rule=(
            None if manifest_row is None else rules.default_rule(manifest_row.manifest)
        ),
    )


# --- Responses -------------------------------------------------------------------


async def _attempt_rows(db: AsyncSession, rerun_id: Any) -> list[Any]:
    return list(
        (
            await db.execute(
                select(ExperimentRerunAttempt)
                .where(ExperimentRerunAttempt.rerun_id == rerun_id)
                .order_by(ExperimentRerunAttempt.attempt)
            )
        )
        .scalars()
        .all()
    )


async def _started(db: AsyncSession, rerun: Any) -> dict[int, Any]:
    """attempt -> its ``rerun.attempt_started`` event."""
    events = (
        (
            await db.execute(
                select(ResearchDecisionEvent).where(
                    ResearchDecisionEvent.collection_id == rerun.collection_id,
                    ResearchDecisionEvent.event_type == "rerun.attempt_started",
                    ResearchDecisionEvent.idempotency_key.like(f"rerun:{rerun.id}:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    return {int(cast(dict, e.payload)["attempt"]): e for e in events}


def _lease(event: Any) -> datetime:
    return datetime.fromisoformat(cast(dict, event.payload)["lease_expires_at"])


def _row_response(row: Any) -> RerunAttemptResponse:
    return RerunAttemptResponse(
        attempt=row.attempt,
        status=row.status,
        reproduction=row.reproduction,
        reasons=list(row.reasons or []),
        environment_validation=dict(row.environment_validation or {}),
        input_validation=dict(row.input_validation or {}),
        comparison=row.comparison,
        comparison_hash=row.comparison_hash,
        outputs=[
            RerunOutputResponse(
                name=o["name"], sha256=o["sha256"], byte_size=o["byte_size"]
            )
            for o in row.outputs or []
        ],
        template_id=row.template_id,
        sandbox_id=row.sandbox_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def _response(db: AsyncSession, rerun: Any) -> RerunResponse:
    rows = await _attempt_rows(db, rerun.id)
    started = await _started(db, rerun)
    attempts = [_row_response(row) for row in rows]
    current = len(rows) + 1
    executed = any(row.status == "executed" for row in rows)
    if not executed and current in started:
        lease = _lease(started[current])
        attempts.append(
            RerunAttemptResponse(
                attempt=current,
                status="running" if lease > _now() else "interrupted",
                started_at=started[current].occurred_at,
                lease_expires_at=lease,
            )
        )
    elif not rows:
        attempts.append(RerunAttemptResponse(attempt=1, status="queued"))
    return RerunResponse(
        id=rerun.id,
        collection_id=rerun.collection_id,
        run_id=rerun.run_id,
        manifest_id=rerun.manifest_id,
        manifest_hash=rerun.manifest_hash,
        rule=rerun.rule,
        rule_hash=rerun.rule_hash,
        requested_by_id=rerun.requested_by_id,
        created_at=rerun.created_at,
        attempts=attempts,
    )


async def _rerun_for(
    db: AsyncSession, rerun_id: UUID, user_id: UUID, action: ResearchAction
) -> tuple[Any, Any, ProjectContext]:
    """(rerun, run, context); 404 for a missing or foreign rerun."""
    rerun = await db.get(ExperimentRerun, rerun_id)
    run = None if rerun is None else await db.get(ResearchRun, rerun.run_id)
    if rerun is None or run is None or run.is_deleted:
        raise HTTPException(status_code=404, detail=RERUN_NOT_FOUND)
    try:
        context, _ = await run_context(db, run, user_id, ResearchAction.VIEW)
        if action != ResearchAction.VIEW:
            context, _ = await run_context(db, run, user_id, action)
    except HTTPException as error:
        if error.status_code == 404:
            raise HTTPException(status_code=404, detail=RERUN_NOT_FOUND) from None
        raise
    if context.collection.id != rerun.collection_id:
        raise HTTPException(status_code=404, detail=RERUN_NOT_FOUND)
    return rerun, run, context


async def get(db: AsyncSession, rerun_id: UUID, user_id: UUID) -> RerunResponse:
    """VIEW: the rule, its hash and every attempt with derived status."""
    rerun, _, _ = await _rerun_for(db, rerun_id, user_id, ResearchAction.VIEW)
    return await _response(db, rerun)


async def list_for_run(db: AsyncSession, run: Any, user_id: UUID) -> RerunListResponse:
    """VIEW: every rerun of one run, oldest first."""
    await run_context(db, run, user_id, ResearchAction.VIEW)
    reruns = (
        (
            await db.execute(
                select(ExperimentRerun)
                .where(ExperimentRerun.run_id == run.id)
                .order_by(ExperimentRerun.created_at, ExperimentRerun.id)
            )
        )
        .scalars()
        .all()
    )
    return RerunListResponse(reruns=[await _response(db, r) for r in reruns])


async def latest_reproduction(db: AsyncSession, run_id: UUID) -> str:
    """GOO-315's read: the newest executed attempt's verdict, else
    ``not_attempted``. Never a release gate."""
    verdict = (
        await db.execute(
            select(ExperimentRerunAttempt.reproduction)
            .join(
                ExperimentRerun, ExperimentRerun.id == ExperimentRerunAttempt.rerun_id
            )
            .where(
                ExperimentRerun.run_id == run_id,
                ExperimentRerunAttempt.status == "executed",
            )
            .order_by(ExperimentRerunAttempt.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return cast(str, verdict) if verdict else "not_attempted"


# --- Ledger helpers --------------------------------------------------------------


async def _append(
    db: AsyncSession,
    rerun: Any,
    *,
    event_type: str,
    actor_id: Any,
    actor_role: str,
    payload: dict[str, Any],
    key: str,
    fingerprint: str | None = None,
) -> None:
    await append_decision(
        db,
        collection_id=cast(UUID, rerun.collection_id),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=cast(UUID, rerun.collection_id),
        event_type=event_type,
        event_schema_version=1,
        actor_user_id=cast(UUID, actor_id),
        actor_role=actor_role,
        subject_type=SUBJECT_TYPE,
        subject_id=cast(UUID, rerun.id),
        subject_version_id=None,
        subject_hash=decision_request_fingerprint(payload),
        reason=None,
        payload=payload,
        idempotency_key=key,
        request_fingerprint=fingerprint or decision_request_fingerprint(payload),
    )


async def _lock(db: AsyncSession, collection_id: Any) -> Any:
    return await lock_aggregate_stream(
        db,
        collection_id=cast(UUID, collection_id),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=cast(UUID, collection_id),
    )


async def _publish(
    db: AsyncSession,
    rerun: Any,
    attempt: int,
    *,
    status: str,
    actor_id: Any,
    actor_role: str,
    reasons: Sequence[str] = (),
    started_at: datetime | None = None,
    **fields: Any,
) -> ExperimentRerunAttempt:
    """Insert the terminal row and append ``rerun.attempt_finished`` in the
    caller's transaction (stream already locked). ``IntegrityError`` on
    ``(rerun_id, attempt)`` means another terminal row won."""
    reproduction = fields.pop("reproduction", None)
    outputs = list(fields.pop("outputs", []))
    row = ExperimentRerunAttempt(
        id=uuid4(),
        rerun_id=rerun.id,
        attempt=attempt,
        status=status,
        reproduction=reproduction,
        environment_validation=fields.pop("environment_validation", {}),
        input_validation=fields.pop("input_validation", {}),
        reasons=list(reasons),
        outputs=outputs,
        started_at=started_at,
        **fields,
    )
    db.add(row)
    await db.flush()
    await _append(
        db,
        rerun,
        event_type="rerun.attempt_finished",
        actor_id=actor_id,
        actor_role=actor_role,
        payload={
            "collection_id": str(rerun.collection_id),
            "rerun_id": str(rerun.id),
            "attempt": attempt,
            "status": status,
            "reproduction": reproduction,
            "comparison_hash": row.comparison_hash,
            "output_sha256s": [o["sha256"] for o in outputs],
            "reasons": list(reasons),
        },
        key=f"rerun:{rerun.id}:{attempt}:finished",
    )
    return row


async def _publish_or_lose(
    db: AsyncSession, rerun: Any, attempt: int, **kwargs: Any
) -> bool:
    """Commit one terminal row; False (rolled back) when another won."""
    try:
        await _publish(db, rerun, attempt, **kwargs)
        await db.commit()
        return True
    except IntegrityError as error:
        await db.rollback()
        if _is_unique_violation(error):
            return False
        raise


# --- Admission ---------------------------------------------------------------------


async def admit(
    db: AsyncSession, actor_id: UUID, run: Any, data: RerunCreate
) -> tuple[RerunResponse, bool]:
    """REVIEW: bind the run's retained plan (``require_run_conformance``),
    check eligibility, validate and hash the rule, insert the rerun, append
    ``rerun.admitted`` and enqueue attempt 1 after the one commit."""
    context, blueprint = await run_context(db, run, actor_id, ResearchAction.REVIEW)
    await require_run_conformance(db, run, blueprint, context)
    collection_id = cast(UUID, context.collection.id)
    stream = await _lock(db, collection_id)
    key = f"rerun:{run.id}:{data.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": "admit_rerun",
            "actor_user_id": str(actor_id),
            "run_id": str(run.id),
            "rule": data.rule,
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        rerun = await db.get(
            ExperimentRerun, UUID(cast(dict, replay.payload)["rerun_id"])
        )
        return await _response(db, rerun), True
    manifest_row = await _manifest(db, run.id)
    reasons = await _eligibility(db, run, blueprint, manifest_row)
    if reasons:
        raise RerunIneligible(reasons)
    manifest = cast(Mapping[str, Any], manifest_row.manifest)
    try:
        rule = rules.validate_rule(
            rules.default_rule(manifest) if data.rule is None else data.rule, manifest
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    rerun = ExperimentRerun(
        id=uuid4(),
        collection_id=collection_id,
        run_id=run.id,
        manifest_id=manifest_row.id,
        manifest_hash=manifest_row.manifest_hash,
        rule=rule,
        rule_hash=rules.rule_hash(rule),
        requested_by_id=actor_id,
        actor_role="reviewer",
        idempotency_key=data.idempotency_key,
    )
    db.add(rerun)
    await db.flush()
    await _append(
        db,
        rerun,
        event_type="rerun.admitted",
        actor_id=actor_id,
        actor_role="reviewer",
        payload={
            "collection_id": str(collection_id),
            "rerun_id": str(rerun.id),
            "run_id": str(run.id),
            "manifest_id": str(manifest_row.id),
            "manifest_hash": manifest_row.manifest_hash,
            "rule_hash": rerun.rule_hash,
        },
        key=key,
        fingerprint=fingerprint,
    )
    _enqueue(db, rerun.id, 1)
    await db.commit()
    await db.refresh(rerun)
    return await _response(db, rerun), False


def _enqueue(db: AsyncSession, rerun_id: Any, attempt: int) -> None:
    from src.tasks.enqueue import enqueue_after_commit
    from src.tasks.research_run_tasks import execute_experiment_rerun

    enqueue_after_commit(db, execute_experiment_rerun, str(rerun_id), attempt)


# --- Cancel and retry ------------------------------------------------------------


async def cancel(db: AsyncSession, rerun_id: UUID, actor_id: UUID) -> RerunResponse:
    """REVIEW, idempotent: a ``cancelled`` terminal row for the in-flight
    attempt (attempt 1 before it starts, or a started attempt). A worker that
    finishes later loses the insert and discards its outputs.

    ponytail: no sandbox kill; the sandbox id is only known inside the
    worker, and ``run_isolated`` kills its sandbox when the command ends
    (bounded by the timeout). Store the id in the claim event if minutes
    matter."""
    rerun, _, _ = await _rerun_for(db, rerun_id, actor_id, ResearchAction.REVIEW)
    await _lock(db, rerun.collection_id)
    rows = await _attempt_rows(db, rerun.id)
    started = await _started(db, rerun)
    current = len(rows) + 1
    if not any(r.status == "executed" for r in rows) and (
        current == 1 or current in started
    ):
        event = started.get(current)
        await _publish(
            db,
            rerun,
            current,
            status="cancelled",
            actor_id=actor_id,
            actor_role="reviewer",
            reasons=["cancelled_by_reviewer"],
            started_at=None if event is None else event.occurred_at,
        )
    await db.commit()
    return await _response(db, rerun)


async def retry(db: AsyncSession, rerun_id: UUID, actor_id: UUID) -> RerunResponse:
    """REVIEW: attempt n+1 under the same stored rule and ``rule_hash``,
    only when attempt n is terminal and did not execute. The claim (and its
    lease) is the reviewer's ``rerun.attempt_started``."""
    rerun, _, _ = await _rerun_for(db, rerun_id, actor_id, ResearchAction.REVIEW)
    await _lock(db, rerun.collection_id)
    rows = await _attempt_rows(db, rerun.id)
    started = await _started(db, rerun)
    nxt = len(rows) + 1
    if not rows or rows[-1].status == "executed" or nxt in started:
        raise HTTPException(status_code=409, detail=NOT_RETRYABLE)
    await _claim(db, rerun, nxt, actor_id=actor_id, actor_role="reviewer")
    _enqueue(db, rerun.id, nxt)
    await db.commit()
    return await _response(db, rerun)


async def _claim(
    db: AsyncSession, rerun: Any, attempt: int, *, actor_id: Any, actor_role: str
) -> None:
    await _append(
        db,
        rerun,
        event_type="rerun.attempt_started",
        actor_id=actor_id,
        actor_role=actor_role,
        payload={
            "collection_id": str(rerun.collection_id),
            "rerun_id": str(rerun.id),
            "attempt": attempt,
            "lease_expires_at": (_now() + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        },
        key=_started_key(rerun.id, attempt),
    )


# --- Sweep -------------------------------------------------------------------------


async def sweep_expired(db: AsyncSession) -> int:
    """Insert ``interrupted`` for every started attempt whose lease expired
    without a terminal row. ponytail: scans every claim event; filter on the
    lease in SQL when reruns number in the thousands."""
    claims = (
        await db.execute(
            select(
                ResearchDecisionEvent.payload, ResearchDecisionEvent.occurred_at
            ).where(ResearchDecisionEvent.event_type == "rerun.attempt_started")
        )
    ).all()
    now, swept = _now(), 0
    for payload, occurred_at in claims:
        if datetime.fromisoformat(payload["lease_expires_at"]) > now:
            continue
        row = await db.get(ExperimentRerun, UUID(payload["rerun_id"]))
        if row is None:  # FK RESTRICT: a claim always names a rerun
            continue
        rerun = SimpleNamespace(
            id=row.id,
            collection_id=row.collection_id,
            requested_by_id=row.requested_by_id,
        )
        attempt = int(payload["attempt"])
        await _lock(db, rerun.collection_id)
        rows = await _attempt_rows(db, rerun.id)
        if any(r.attempt == attempt for r in rows):
            await db.rollback()
            continue
        if await _publish_or_lose(
            db,
            rerun,
            attempt,
            status="interrupted",
            actor_id=rerun.requested_by_id,
            actor_role="machine",
            reasons=["lease_expired"],
            started_at=occurred_at,
        ):
            swept += 1
    return swept


# --- Worker ------------------------------------------------------------------------


def _validation(
    result: Any, manifest: Mapping[str, Any], expected: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Environment and input validation as far as the restore got: ``True``
    verified in the sandbox, ``False`` refused, ``None`` never reached."""
    reasons = set(result.reasons)
    environment = dict(manifest.get("environment") or {})
    mismatched = {
        r for r in reasons if r == "code_mismatch" or r.startswith("input_mismatch:")
    }
    # The command runs only after every restoration check passed.
    command_ran = result.status == "completed" or str(result.error or "").startswith(
        ("exit_code:", "missing_output:")
    )
    hashed = command_ran or bool(mismatched)
    lock_refused = bool(
        reasons & {"environment_install_failed", "environment_lock_mismatch"}
    )
    files: dict[str, Any] = {}
    for name, digest in expected.items():
        reason = (
            "code_mismatch"
            if name == manifest_rules.CODE_NAME
            else f"input_mismatch:{name.removeprefix('in/')}"
        )
        files[name] = {
            "expected_sha256": digest,
            "verified_in_sandbox": (reason not in reasons) if hashed else None,
        }
    return (
        {
            "expected_template_id": environment.get("template_id"),
            "actual_template_id": result.template_id,
            "template_verified": (
                None
                if result.template_id is None
                else result.template_id == environment.get("template_id")
            ),
            "lock_sha256": environment.get("lock_sha256"),
            "lock_verified": True if hashed else (False if lock_refused else None),
        },
        {"files": files},
    )


async def execute_attempt(
    db_factory: SessionFactory, rerun_id: UUID, attempt: int
) -> None:
    """The worker: claim, restore and run outside any transaction, store
    the outputs, compare, then publish the terminal row (or lose to a
    cancel/interrupt row and delete this attempt's blobs)."""
    async with db_factory() as db:
        row = await db.get(ExperimentRerun, rerun_id)
        if row is None:
            return
        # Plain values: a lost insert rolls back and expires ORM instances.
        rerun = SimpleNamespace(
            id=row.id,
            collection_id=row.collection_id,
            run_id=row.run_id,
            manifest_id=row.manifest_id,
            rule=row.rule,
            requested_by_id=row.requested_by_id,
        )
        try:
            await lock_active_project(db, cast(UUID, rerun.collection_id))
        except HTTPException:
            await _lock(db, rerun.collection_id)
            if len(await _attempt_rows(db, rerun.id)) + 1 != attempt:
                return
            await _publish_or_lose(
                db,
                rerun,
                attempt,
                status="cancelled",
                actor_id=rerun.requested_by_id,
                actor_role="machine",
                reasons=["project_lifecycle"],
            )
            return
        await _lock(db, rerun.collection_id)
        rows = await _attempt_rows(db, rerun.id)
        started = await _started(db, rerun)
        claim = started.get(attempt)
        if len(rows) + 1 != attempt or (
            claim is not None and claim.actor_role == "machine"
        ):
            # Not the in-flight attempt, or another worker claimed it.
            return
        if claim is None:
            await _claim(
                db, rerun, attempt, actor_id=rerun.requested_by_id, actor_role="machine"
            )
            claim = (await _started(db, rerun))[attempt]
        started_at = claim.occurred_at
        manifest_row = await db.get(ResearchRunManifest, rerun.manifest_id)
        manifest = cast(Mapping[str, Any], manifest_row.manifest)
        artifacts = await _artifacts(db, rerun.run_id)
        organization_id = artifacts[0].organization_id
        await db.commit()
    data, checks = await _archived(artifacts, _required(manifest))
    corrupt = [
        f"artifact_corrupt:{name}"
        for name, (exists, ok) in sorted(checks.items())
        if not (exists and ok)
    ]
    finish: dict[str, Any] = {
        "actor_id": rerun.requested_by_id,
        "actor_role": "machine",
        "started_at": started_at,
    }
    if corrupt:
        async with db_factory() as db:
            await _lock(db, rerun.collection_id)
            await _publish_or_lose(
                db,
                rerun,
                attempt,
                status="restoration_failed",
                reasons=corrupt,
                **finish,
            )
        return
    environment = cast(Mapping[str, Any], manifest["environment"])
    files = {manifest_rules.CODE_NAME: data[("code", manifest_rules.CODE_NAME)]}
    expected = {manifest_rules.CODE_NAME: str(manifest["code"]["sha256"])}
    for item in manifest["inputs"]:
        files[f"in/{item['name']}"] = data[("input", item["name"])]
        expected[f"in/{item['name']}"] = str(item["sha256"])
    outputs = [str(o["name"]) for o in manifest["outputs"]]
    template = str(environment["template_id"])
    result = await get_sandbox_manager().run_isolated(
        IsolatedSpec(
            template=template,
            requirements=(),
            files=files,
            command=str(manifest["command"]),
            output_names=tuple(outputs),
        ),
        restore=RestoreSpec(
            expected_template_id=template,
            lock_bytes=data[("environment", manifest_rules.LOCK_NAME)],
            expected_sha256=expected,
        ),
    )
    env_check, input_check = _validation(result, manifest, expected)
    finish |= {
        "environment_validation": env_check,
        "input_validation": input_check,
        "template_id": result.template_id,
        "sandbox_id": result.sandbox_id,
    }
    status, reasons = {
        "unavailable": ("environment_unavailable", [result.error or "unavailable"]),
        "restoration_failed": ("restoration_failed", list(result.reasons)),
        "failed": ("execution_failed", [result.error or "failed"]),
        "timeout": ("execution_failed", ["timeout"]),
        "completed": ("executed", []),
    }[result.status]
    written: list[str] = []
    if status == "executed":
        storage = get_artifact_storage()
        stored = []
        try:
            for name in outputs:
                blob = result.outputs[name]
                sha = manifest_rules.sha256_hex(blob)
                key = _output_key(organization_id, rerun.id, attempt, sha)
                await storage.put(key, blob, "application/octet-stream")
                written.append(key)
                stored.append(
                    {
                        "name": name,
                        "sha256": sha,
                        "byte_size": len(blob),
                        "storage_key": key,
                    }
                )
        except Exception:
            await _forget(written)
            written, status, reasons = [], "execution_failed", ["output_storage_failed"]
        if status == "executed":
            expected_outputs = {
                name: blob for (role, name), blob in data.items() if role == "output"
            }
            comparison, reproduced = rules.compare(
                rerun.rule, manifest, result.outputs, expected_outputs
            )
            finish |= {
                "outputs": stored,
                "comparison": comparison,
                "comparison_hash": canonical_json_sha256(comparison),
                "reproduction": "reproduced" if reproduced else "not_reproduced",
            }
    async with db_factory() as db:
        await _lock(db, rerun.collection_id)
        won = await _publish_or_lose(
            db, rerun, attempt, status=status, reasons=reasons, **finish
        )
        if not won and written:
            winner = (
                await db.execute(
                    select(ExperimentRerunAttempt.outputs).where(
                        ExperimentRerunAttempt.rerun_id == rerun.id,
                        ExperimentRerunAttempt.attempt == attempt,
                    )
                )
            ).scalar_one()
            kept = {o["storage_key"] for o in winner or []}
            await _forget([key for key in written if key not in kept])


async def _forget(keys: Sequence[str]) -> None:
    """Compensating delete for blobs no committed row cites."""
    storage = get_artifact_storage()
    for key in keys:
        await storage.delete(key)


# --- Downloads and export ------------------------------------------------------


async def read_output(
    db: AsyncSession, rerun_id: UUID, attempt: int, name: str, user_id: UUID
) -> tuple[bytes, str]:
    """VIEW: one retained output, re-hashed before it leaves."""
    rerun, _, _ = await _rerun_for(db, rerun_id, user_id, ResearchAction.VIEW)
    row = next(
        (r for r in await _attempt_rows(db, rerun.id) if r.attempt == attempt), None
    )
    if row is None:
        raise HTTPException(status_code=404, detail=ATTEMPT_NOT_FOUND)
    output = next((o for o in row.outputs or [] if o["name"] == name), None)
    if output is None:
        raise HTTPException(status_code=404, detail=OUTPUT_NOT_FOUND)
    data = await get_artifact_storage().get(output["storage_key"])
    if data is None:
        raise HTTPException(status_code=404, detail=OUTPUT_NOT_FOUND)
    if manifest_rules.sha256_hex(data) != output["sha256"]:
        raise HTTPException(status_code=500, detail=OUTPUT_CORRUPT)
    return data, str(output["sha256"])


def _comparison_body(rerun: RerunResponse) -> dict[str, Any]:
    return {
        "schema": rules.COMPARISON_SCHEMA,
        "rerun_id": str(rerun.id),
        "run_id": str(rerun.run_id),
        "manifest_id": str(rerun.manifest_id),
        "manifest_hash": rerun.manifest_hash,
        "rule": rerun.rule,
        "rule_hash": rerun.rule_hash,
        "attempts": [
            a.model_dump(
                mode="json",
                include={
                    "attempt",
                    "status",
                    "reproduction",
                    "reasons",
                    "comparison",
                    "comparison_hash",
                    "outputs",
                },
            )
            for a in rerun.attempts
        ],
    }


async def comparison_export(
    db: AsyncSession, rerun_id: UUID, user_id: UUID
) -> dict[str, Any]:
    """VIEW: ``nous.rerun-comparison/1`` for every attempt (no storage keys)."""
    return _comparison_body(await get(db, rerun_id, user_id))


async def export_body(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``reproduction.json`` body: every rerun of the
    project with its rule, hash and attempts (no storage keys)."""
    reruns = (
        (
            await db.execute(
                select(ExperimentRerun)
                .where(ExperimentRerun.collection_id == context.collection.id)
                .order_by(ExperimentRerun.created_at, ExperimentRerun.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "project_id": str(context.collection.id),
        "reruns": [_comparison_body(await _response(db, r)) for r in reruns],
    }
