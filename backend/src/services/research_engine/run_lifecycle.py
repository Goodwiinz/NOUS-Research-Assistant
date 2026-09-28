"""Atomic state transitions for durable research runs.

``WorkflowEngine`` remains responsible for deciding which stage runs next.
This service is the single persistence boundary for the state produced by that
orchestration: a completed step, its sources and accounting, review gates,
resume consent, and the one-use stream claim are committed together.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import RunStatus
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.schemas.research_engine import ReviewKind, RunResumeRequest
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
    no_evidence_reason,
)
from src.services.research_engine.observability import (
    ResearchObservability,
    research_observability,
    safely_observe,
)

PauseReason = Literal["user_paused", "review_required", "verification_failed"]


@dataclass(frozen=True)
class PauseDescriptor:
    pause_reason: PauseReason
    review_kind: ReviewKind | None
    step_index: int
    output_hash: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.review_kind is not None:
            value["review_kind"] = self.review_kind.value
        else:
            value.pop("review_kind", None)
        return value


@dataclass(frozen=True)
class PersistedStepTransition:
    persisted: bool
    duplicate: bool
    step: ResearchStep
    pause: PauseDescriptor | None = None
    terminal_status: str | None = None


@dataclass(frozen=True)
class ResumeTransition:
    authorized: bool
    completed: bool
    authorization_kind: str | None = None
    final_status: str | None = None
    pause: PauseDescriptor | None = None


@dataclass(frozen=True)
class StreamClaim:
    claimed: bool
    was_paused: bool
    authorization_kind: str | None = None
    started_at_before_claim: datetime | None = None
    manifest_before_claim: dict[str, Any] | None = None


class ResearchRunLifecycleError(RuntimeError):
    """Stable lifecycle conflict suitable for an API response."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        descriptor: PauseDescriptor | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.descriptor = descriptor

    def detail(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.descriptor is not None:
            value["descriptor"] = self.descriptor.to_dict()
        return value


_REVIEW_GATE_BY_STAGE: dict[str, ReviewKind] = {
    "screen": ReviewKind.SCREENING,
    "extract": ReviewKind.EXTRACTION,
    "export": ReviewKind.FINAL,
}
_PAUSE_REQUESTED_KEY = "_pause_requested"
_USER_PAUSE_SENTINEL_HASH = canonical_json_sha256(
    {"kind": "user_pause_before_first_step", "version": 1}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchRunLifecycleService:
    """Own all durable mutations that advance a research run."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now: Callable[[], datetime] = _utc_now,
        observer: ResearchObservability | None = None,
    ) -> None:
        self.session = session
        self.now = now
        self.observer = observer or research_observability

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[None]:
        """Use one database transaction even when a route already read state."""

        in_transaction = getattr(self.session, "in_transaction", None)
        active_transaction = False
        if callable(in_transaction):
            state = in_transaction()
            if inspect.isawaitable(state):
                close = getattr(state, "close", None)
                if callable(close):
                    close()
                # Async test doubles do not model SQLAlchemy's synchronous
                # in_transaction predicate. Their surrounding route has
                # already performed a read, so use the commit/rollback path.
                active_transaction = True
            else:
                active_transaction = bool(state)
        if active_transaction:
            try:
                yield
                await self.session.commit()
            except BaseException:
                await self.session.rollback()
                raise
            return
        async with self.session.begin():
            yield

    async def persist_step_completion(
        self,
        *,
        run: Any,
        event: Mapping[str, Any],
        step_definition: Mapping[str, Any],
        source_rows: Sequence[Any] = (),
    ) -> PersistedStepTransition:
        """Persist a completed step and every resulting state change atomically."""

        output_value = event.get("output")
        output = (
            copy.deepcopy(output_value)
            if isinstance(output_value, dict)
            else {"value": copy.deepcopy(output_value)}
        )
        step_index = self._nonnegative_int(event.get("step_index"), "step_index")
        stage_type = str(
            event.get("step_type") or step_definition.get("type") or "search"
        )
        output_hash = canonical_stage_output_hash(output)
        supplied_hash = event.get("outputs_hash")
        versioned_output = output.get("contract_version") == 1
        if (
            versioned_output
            and supplied_hash is not None
            and supplied_hash != output_hash
        ):
            raise ResearchRunLifecycleError(
                status_code=409,
                code="step_output_stale",
                message="Completed step hash does not match its persisted envelope",
            )

        transition: PersistedStepTransition | None = None
        async with self._atomic():
            existing = await self._load_step(cast(UUID, run.id), step_index)
            if existing is not None:
                if (
                    not isinstance(existing.output, dict)
                    or canonical_stage_output_hash(existing.output) != output_hash
                    or existing.step_type != stage_type
                ):
                    raise ResearchRunLifecycleError(
                        status_code=409,
                        code="step_output_stale",
                        message="A different output already exists for this step",
                    )
                transition = PersistedStepTransition(
                    persisted=False,
                    duplicate=True,
                    step=existing,
                    pause=self.pause_descriptor(run),
                    terminal_status=self._final_status(run),
                )
            else:
                params = self._step_parameters(step_definition)
                prompt_metadata = event.get("prompt_metadata")
                full_prompt: str | None
                if isinstance(prompt_metadata, Mapping):
                    full_prompt = json.dumps(
                        {
                            key: prompt_metadata[key]
                            for key in (
                                "prompt_context_version",
                                "prompt_context_hash",
                            )
                            if key in prompt_metadata
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                else:
                    # Compatibility for non-Daily-Brief runs. Daily Brief
                    # executors always supply content-free prompt_metadata.
                    full_prompt_value = event.get("full_prompt")
                    full_prompt = (
                        str(full_prompt_value)
                        if full_prompt_value is not None
                        else None
                    )

                model_id = step_definition.get("model_id") or params.get("model_id")
                mode = (
                    step_definition.get("mode") or params.get("mode") or "deterministic"
                )
                temperature = params.get("temperature", 0.0)
                seed = params.get("seed")
                token_count = max(0, int(event.get("token_count") or 0))
                step = ResearchStep(
                    run_id=run.id,
                    step_index=step_index,
                    step_type=stage_type,
                    mode=str(mode),
                    inputs_hash=self._optional_hash(event.get("inputs_hash")),
                    outputs_hash=(
                        output_hash
                        if versioned_output
                        else self._optional_hash(supplied_hash) or output_hash
                    ),
                    full_prompt=full_prompt,
                    model_id=str(model_id) if model_id is not None else None,
                    model_version=(
                        str(event["model_version"])
                        if event.get("model_version") is not None
                        else None
                    ),
                    temperature=float(temperature),
                    seed=int(seed) if seed is not None else None,
                    output=output,
                    quality_marks=copy.deepcopy(event.get("quality_marks") or []),
                    completed_at=self.now(),
                    token_count=token_count,
                )
                self.session.add(step)
                for source_row in source_rows:
                    self.session.add(source_row)

                manifest: dict[str, Any] = copy.deepcopy(
                    run.reproducibility_manifest or {}
                )
                stage_hashes = dict(manifest.get("stage_hashes") or {})
                stage_hashes[str(step_index)] = output_hash
                manifest["stage_hashes"] = stage_hashes
                run.total_tokens = max(0, int(run.total_tokens or 0)) + token_count

                terminal_reason = no_evidence_reason(output)
                pause: PauseDescriptor | None = None
                if terminal_reason is not None:
                    manifest.pop("pending_review", None)
                    manifest.pop("verification_failure", None)
                    manifest.pop("resume_authorization", None)
                    manifest.pop("user_pause", None)
                    manifest.pop(_PAUSE_REQUESTED_KEY, None)
                    manifest["final_status"] = "no_evidence"
                    manifest["terminal_audit"] = {
                        "outcome": "no_evidence",
                        "reason": terminal_reason,
                        "step_index": step_index,
                        "stage_type": stage_type,
                        "output_hash": output_hash,
                        "created_at": self.now().isoformat(),
                    }
                    run.status = RunStatus.COMPLETED.value
                    run.completed_at = self.now()
                else:
                    pause = self._pause_after_step(
                        run=run,
                        stage_type=stage_type,
                        step_index=step_index,
                        output=output,
                        output_hash=output_hash,
                        params=params,
                        manifest=manifest,
                    )
                    if pause is None and manifest.get(_PAUSE_REQUESTED_KEY):
                        pause = self._store_user_pause(
                            manifest,
                            step_index=step_index,
                            output_hash=output_hash,
                        )
                    elif pause is not None:
                        manifest.pop("user_pause", None)
                        manifest.pop(_PAUSE_REQUESTED_KEY, None)
                if pause is not None:
                    run.status = RunStatus.PAUSED.value
                run.reproducibility_manifest = manifest
                transition = PersistedStepTransition(
                    persisted=True,
                    duplicate=False,
                    step=step,
                    pause=pause,
                    terminal_status=self._final_status(run),
                )

        assert transition is not None
        organization_id = self._organization_id(run)
        if transition.pause is not None and not transition.duplicate:
            safely_observe(
                self.observer,
                "record_pause",
                run_id=cast(UUID, run.id),
                organization_id=organization_id,
                pause_kind=transition.pause.pause_reason,
                review_kind=(
                    transition.pause.review_kind.value
                    if transition.pause.review_kind is not None
                    else "none"
                ),
            )
        if transition.terminal_status is not None and not transition.duplicate:
            safely_observe(
                self.observer,
                "record_final_status",
                run_id=cast(UUID, run.id),
                organization_id=organization_id,
                status=transition.terminal_status,
            )
        return transition

    async def persist_user_pause(
        self,
        *,
        run: Any,
        step_index: int | None = None,
        output_hash: str | None = None,
        total_tokens: int | None = None,
    ) -> PauseDescriptor:
        """Persist a reloadable manual-pause anchor without research content."""

        async with self._atomic():
            await self._lock_run(run)
            if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
                raise ResearchRunLifecycleError(
                    status_code=409,
                    code="run_not_pauseable",
                    message="A terminal run cannot be paused",
                )
            result = await self._persist_user_pause_locked(
                run=run,
                step_index=step_index,
                output_hash=output_hash,
                total_tokens=total_tokens,
            )
        self._observe_pause(run, result)
        return result

    async def recover_stream_cancellation(
        self,
        *,
        run: Any,
        step_index: int | None = None,
        output_hash: str | None = None,
        total_tokens: int | None = None,
    ) -> PauseDescriptor | None:
        """Pause interrupted work while preserving an already-terminal commit."""

        async with self._atomic():
            await self._lock_run(run)
            # This check is deliberately inside the locked transaction. A
            # terminal commit may win after the route last inspected its ORM
            # object but before disconnect recovery takes the row lock.
            if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
                return None
            result = await self._persist_user_pause_locked(
                run=run,
                step_index=step_index,
                output_hash=output_hash,
                total_tokens=total_tokens,
            )
        self._observe_pause(run, result)
        return result

    async def _persist_user_pause_locked(
        self,
        *,
        run: Any,
        step_index: int | None,
        output_hash: str | None,
        total_tokens: int | None,
    ) -> PauseDescriptor:
        manifest: dict[str, Any] = copy.deepcopy(run.reproducibility_manifest or {})
        result: PauseDescriptor | None

        # A review or failed-verification gate owns the pause when it was
        # committed at the same boundary as the user's request.
        if isinstance(manifest.get("pending_review"), dict) or isinstance(
            manifest.get("verification_failure"), dict
        ):
            manifest.pop("user_pause", None)
            manifest.pop(_PAUSE_REQUESTED_KEY, None)
            run.status = RunStatus.PAUSED.value
            run.reproducibility_manifest = manifest
            result = self.pause_descriptor(run)
        else:
            resolved_index = step_index
            resolved_hash = output_hash
            if resolved_index is None or resolved_hash is None:
                anchor = await self.current_user_pause_descriptor(run=run)
                resolved_index = anchor.step_index
                resolved_hash = anchor.output_hash
            if type(resolved_index) is not int or resolved_index < -1:
                raise ValueError("user pause step index must be -1 or greater")
            if not self._is_hash(resolved_hash):
                raise ValueError("user pause output hash must be canonical")
            result = self._store_user_pause(
                manifest,
                step_index=resolved_index,
                output_hash=cast(str, resolved_hash),
            )
            run.status = RunStatus.PAUSED.value
            run.reproducibility_manifest = manifest

        if total_tokens is not None:
            run.total_tokens = max(0, int(total_tokens))

        if result is None:
            raise ResearchRunLifecycleError(
                status_code=409,
                code="run_not_paused",
                message="Run does not have a durable pause descriptor",
            )
        return result

    def _observe_pause(self, run: Any, result: PauseDescriptor) -> None:
        safely_observe(
            self.observer,
            "record_pause",
            run_id=cast(UUID, run.id),
            organization_id=self._organization_id(run),
            pause_kind=result.pause_reason,
            review_kind=(
                result.review_kind.value if result.review_kind is not None else "none"
            ),
        )

    async def current_user_pause_descriptor(self, *, run: Any) -> PauseDescriptor:
        """Resolve the latest safe manual-pause anchor without mutating the run."""

        latest = await self._load_latest_step(cast(UUID, run.id))
        if latest is None or not isinstance(latest.output, dict):
            return PauseDescriptor(
                pause_reason="user_paused",
                review_kind=None,
                step_index=-1,
                output_hash=_USER_PAUSE_SENTINEL_HASH,
            )
        return PauseDescriptor(
            pause_reason="user_paused",
            review_kind=None,
            step_index=int(latest.step_index),
            output_hash=canonical_stage_output_hash(latest.output),
        )

    def user_pause_manifest_value(self, descriptor: PauseDescriptor) -> dict[str, Any]:
        if descriptor.pause_reason != "user_paused":
            raise ValueError("manual pause manifest requires a user pause descriptor")
        return {
            "step_index": descriptor.step_index,
            "output_hash": descriptor.output_hash,
            "created_at": self.now().isoformat(),
        }

    async def authorize_resume(
        self,
        *,
        run: Any,
        actor_id: UUID,
        request: RunResumeRequest | Mapping[str, Any] | None = None,
    ) -> ResumeTransition:
        """Validate exact consent and mint one one-use stream authorization."""

        request = request or RunResumeRequest()
        continue_unverified = self._request_value(request, "continue_unverified", False)
        requested_hash = self._request_value(request, "output_hash", None)
        result: ResumeTransition | None = None
        created_verification_override = False

        async with self._atomic():
            await self._lock_run(run)
            if run.status != RunStatus.PAUSED.value:
                raise ResearchRunLifecycleError(
                    status_code=409,
                    code="run_not_paused",
                    message="Run is not currently paused",
                )

            manifest: dict[str, Any] = copy.deepcopy(run.reproducibility_manifest or {})
            existing_authorization = manifest.get("resume_authorization")
            if isinstance(
                existing_authorization, dict
            ) and not existing_authorization.get("consumed_at"):
                result = ResumeTransition(
                    authorized=True,
                    completed=False,
                    authorization_kind=str(
                        existing_authorization.get("kind") or "user_resume"
                    ),
                )
                run.reproducibility_manifest = manifest
            pending = manifest.get("pending_review")
            if result is not None:
                pass
            elif isinstance(pending, dict):
                descriptor = self.pause_descriptor(run)
                if descriptor is None or pending.get("status") != "approved":
                    raise ResearchRunLifecycleError(
                        status_code=409,
                        code="review_required",
                        message="The persisted stage requires an approved review",
                        descriptor=descriptor,
                    )
                review = await self._approved_review(run, pending)
                if review is None:
                    raise ResearchRunLifecycleError(
                        status_code=409,
                        code="review_required",
                        message="The exact approved review could not be verified",
                        descriptor=descriptor,
                    )
                review_step_index = int(review.step_index)
                review_kind = str(review.review_kind)
                review_output_hash = str(review.output_hash)
                history = list(manifest.get("review_history") or [])
                audit_entry = {
                    "review_id": str(review.id),
                    "reviewer_id": str(review.reviewer_id),
                    "step_index": review_step_index,
                    "review_kind": review_kind,
                    "output_hash": review_output_hash,
                    "decision": str(review.decision),
                    "created_at": (
                        review.created_at.isoformat() if review.created_at else None
                    ),
                }
                if audit_entry not in history:
                    history.append(audit_entry)
                manifest["review_history"] = history
                manifest.pop("pending_review", None)

                if review_kind in {
                    ReviewKind.SCREENING.value,
                    ReviewKind.EXTRACTION.value,
                } and self._review_has_no_evidence(review):
                    manifest.pop("resume_authorization", None)
                    manifest["final_status"] = "no_evidence"
                    run.status = RunStatus.COMPLETED.value
                    run.completed_at = self.now()
                    run.reproducibility_manifest = manifest
                    result = ResumeTransition(
                        authorized=False,
                        completed=True,
                        final_status="no_evidence",
                    )
                elif review_kind == ReviewKind.FINAL.value:
                    manifest.pop("resume_authorization", None)
                    manifest["final_status"] = "verified"
                    run.status = RunStatus.COMPLETED.value
                    run.completed_at = self.now()
                    run.reproducibility_manifest = manifest
                    result = ResumeTransition(
                        authorized=False,
                        completed=True,
                        final_status="verified",
                    )
                else:
                    authorization = self._authorization(
                        kind="approved_review",
                        actor_id=actor_id,
                        output_hash=review_output_hash,
                        step_index=review_step_index,
                        review_id=str(review.id),
                    )
                    manifest["resume_authorization"] = authorization
                    run.reproducibility_manifest = manifest
                    result = ResumeTransition(
                        authorized=True,
                        completed=False,
                        authorization_kind="approved_review",
                    )
            else:
                verification = await self._verification_failure(run, manifest)
                if isinstance(verification, dict):
                    descriptor = self._verification_descriptor(verification)
                    expected_hash = verification.get("output_hash")
                    if not continue_unverified or not requested_hash:
                        raise ResearchRunLifecycleError(
                            status_code=409,
                            code="verification_override_required",
                            message=(
                                "Continuing requires explicit unverified consent "
                                "for the persisted verification output"
                            ),
                            descriptor=descriptor,
                        )
                    if requested_hash != expected_hash:
                        raise ResearchRunLifecycleError(
                            status_code=409,
                            code="verification_output_stale",
                            message="Verification output is stale",
                            descriptor=descriptor,
                        )
                    manifest["verification_override"] = {
                        "actor_id": str(actor_id),
                        "output_hash": expected_hash,
                        "step_index": verification.get("step_index"),
                        "created_at": self.now().isoformat(),
                    }
                    manifest["resume_authorization"] = self._authorization(
                        kind="continue_unverified",
                        actor_id=actor_id,
                        output_hash=cast(str, expected_hash),
                        step_index=cast(int, verification.get("step_index")),
                    )
                    manifest.pop("verification_failure", None)
                    run.reproducibility_manifest = manifest
                    created_verification_override = True
                    result = ResumeTransition(
                        authorized=True,
                        completed=False,
                        authorization_kind="continue_unverified",
                    )
                else:
                    # A plain user pause has no content gate, but still uses a
                    # POST-created one-use authorization before GET /stream.
                    user_pause = manifest.get("user_pause")
                    pause_index = -1
                    pause_hash = _USER_PAUSE_SENTINEL_HASH
                    if isinstance(user_pause, dict):
                        candidate_index = user_pause.get("step_index")
                        candidate_hash = user_pause.get("output_hash")
                        if type(candidate_index) is int and self._is_hash(
                            candidate_hash
                        ):
                            pause_index = candidate_index
                            pause_hash = cast(str, candidate_hash)
                    manifest["resume_authorization"] = self._authorization(
                        kind="user_resume",
                        actor_id=actor_id,
                        output_hash=pause_hash,
                        step_index=pause_index,
                    )
                    manifest["continuation_requested"] = True
                    run.reproducibility_manifest = manifest
                    result = ResumeTransition(
                        authorized=True,
                        completed=False,
                        authorization_kind="user_resume",
                    )

        assert result is not None
        organization_id = self._organization_id(run)
        if result.final_status is not None:
            safely_observe(
                self.observer,
                "record_final_status",
                run_id=cast(UUID, run.id),
                organization_id=organization_id,
                status=result.final_status,
            )
        if created_verification_override:
            safely_observe(
                self.observer,
                "record_override",
                run_id=cast(UUID, run.id),
                organization_id=organization_id,
                override_kind="verification",
                outcome="continued_unverified",
            )
            safely_observe(
                self.observer,
                "record_verification",
                run_id=cast(UUID, run.id),
                organization_id=organization_id,
                outcome="overridden",
            )
        return result

    @staticmethod
    def _organization_id(run: Any) -> UUID | None:
        value = getattr(run, "organization_id", None)
        return value if isinstance(value, UUID) else None

    async def claim_stream(self, *, run: Any) -> StreamClaim:
        """Claim a pending run or consume one paused-run authorization once."""

        result: StreamClaim | None = None
        async with self._atomic():
            await self._lock_run(run)
            if run.status == RunStatus.PENDING.value:
                started_at_before_claim = getattr(run, "started_at", None)
                manifest_before_claim = copy.deepcopy(run.reproducibility_manifest)
                run.status = RunStatus.RUNNING.value
                run.started_at = started_at_before_claim or self.now()
                result = StreamClaim(
                    claimed=True,
                    was_paused=False,
                    started_at_before_claim=started_at_before_claim,
                    manifest_before_claim=manifest_before_claim,
                )
            elif run.status == RunStatus.PAUSED.value:
                manifest: dict[str, Any] = copy.deepcopy(
                    run.reproducibility_manifest or {}
                )
                manifest_before_claim = copy.deepcopy(manifest)
                started_at_before_claim = getattr(run, "started_at", None)
                authorization = manifest.get("resume_authorization")
                if not isinstance(authorization, dict) or authorization.get(
                    "consumed_at"
                ):
                    verification = await self._verification_failure(run, manifest)
                    descriptor = (
                        self._verification_descriptor(verification)
                        if isinstance(verification, dict)
                        else self.pause_descriptor(run)
                    )
                    code = (
                        "verification_override_required"
                        if descriptor
                        and descriptor.pause_reason == "verification_failed"
                        else (
                            "review_required"
                            if descriptor
                            and descriptor.pause_reason == "review_required"
                            else "resume_authorization_required"
                        )
                    )
                    raise ResearchRunLifecycleError(
                        status_code=409,
                        code=code,
                        message="A one-use resume authorization is required",
                        descriptor=descriptor,
                    )
                kind = str(authorization.get("kind") or "")
                authorization["consumed_at"] = self.now().isoformat()
                manifest["resume_authorization"] = authorization
                if kind == "continue_unverified":
                    manifest["continued_after_failure"] = True
                    manifest["continued_after_failure_step_index"] = authorization.get(
                        "step_index"
                    )
                manifest.pop("user_pause", None)
                manifest.pop("continuation_requested", None)
                run.reproducibility_manifest = manifest
                run.status = RunStatus.RUNNING.value
                run.started_at = getattr(run, "started_at", None) or self.now()
                result = StreamClaim(
                    claimed=True,
                    was_paused=True,
                    authorization_kind=kind,
                    started_at_before_claim=started_at_before_claim,
                    manifest_before_claim=manifest_before_claim,
                )
            else:
                raise ResearchRunLifecycleError(
                    status_code=409,
                    code="run_already_claimed",
                    message="Run was just claimed by another stream",
                )

        assert result is not None
        return result

    async def release_stream_claim(
        self,
        *,
        run: Any,
        claim: StreamClaim,
    ) -> None:
        """Undo a claim when admission rejects work before execution starts.

        A paused claim consumes its resume authorization in the same transaction
        that marks the run running.  Admission happens after that claim, so a
        denial must restore both pieces of state or the exact consent becomes
        unusable on retry.
        """

        async with self._atomic():
            await self._lock_run(run)
            if run.status != RunStatus.RUNNING.value:
                raise ResearchRunLifecycleError(
                    status_code=409,
                    code="run_already_claimed",
                    message="Run is no longer held by this stream claim",
                )

            if claim.was_paused:
                manifest = copy.deepcopy(claim.manifest_before_claim or {})
                authorization = manifest.get("resume_authorization")
                if not isinstance(authorization, dict):
                    raise ResearchRunLifecycleError(
                        status_code=409,
                        code="resume_authorization_required",
                        message="Paused run claim is missing its resume authorization",
                    )
                run.reproducibility_manifest = manifest
                run.status = RunStatus.PAUSED.value
                run.started_at = claim.started_at_before_claim
            else:
                run.status = RunStatus.PENDING.value
                run.started_at = claim.started_at_before_claim
                run.reproducibility_manifest = copy.deepcopy(
                    claim.manifest_before_claim
                )

    @staticmethod
    def pause_descriptor(run: Any) -> PauseDescriptor | None:
        if getattr(run, "status", None) != RunStatus.PAUSED.value:
            return None
        manifest = getattr(run, "reproducibility_manifest", None)
        if not isinstance(manifest, dict):
            return None
        pending = manifest.get("pending_review")
        if isinstance(pending, dict):
            if pending.get("run_id") != str(run.id):
                return None
            try:
                kind = ReviewKind(str(pending.get("review_kind")))
            except ValueError:
                return None
            index = pending.get("step_index")
            output_hash = pending.get("output_hash")
            if (
                type(index) is int
                and index >= 0
                and ResearchRunLifecycleService._is_hash(output_hash)
            ):
                return PauseDescriptor(
                    pause_reason="review_required",
                    review_kind=kind,
                    step_index=index,
                    output_hash=cast(str, output_hash),
                )
        failure = manifest.get("verification_failure")
        if isinstance(failure, dict):
            if failure.get("run_id") != str(run.id):
                return None
            index = failure.get("step_index")
            output_hash = failure.get("output_hash")
            if (
                type(index) is int
                and index >= 0
                and ResearchRunLifecycleService._is_hash(output_hash)
            ):
                return PauseDescriptor(
                    pause_reason="verification_failed",
                    review_kind=None,
                    step_index=index,
                    output_hash=cast(str, output_hash),
                )
        user_pause = manifest.get("user_pause")
        if isinstance(user_pause, dict):
            index = user_pause.get("step_index")
            output_hash = user_pause.get("output_hash")
            if type(index) is int and ResearchRunLifecycleService._is_hash(output_hash):
                return PauseDescriptor(
                    pause_reason="user_paused",
                    review_kind=None,
                    step_index=index,
                    output_hash=cast(str, output_hash),
                )
        return None

    async def _load_step(self, run_id: UUID, step_index: int) -> ResearchStep | None:
        result = await self.session.execute(
            select(ResearchStep).where(
                cast(Any, ResearchStep.run_id) == run_id,
                cast(Any, ResearchStep.step_index) == step_index,
            )
        )
        candidate = result.scalars().first()
        return candidate if isinstance(candidate, ResearchStep) else None

    async def _load_latest_step(self, run_id: UUID) -> ResearchStep | None:
        result = await self.session.execute(
            select(ResearchStep)
            .where(cast(Any, ResearchStep.run_id) == run_id)
            .order_by(cast(Any, ResearchStep.step_index).desc())
            .limit(1)
        )
        candidate = result.scalars().first()
        return candidate if isinstance(candidate, ResearchStep) else None

    def _store_user_pause(
        self,
        manifest: dict[str, Any],
        *,
        step_index: int,
        output_hash: str,
    ) -> PauseDescriptor:
        manifest.pop(_PAUSE_REQUESTED_KEY, None)
        manifest["user_pause"] = {
            "step_index": step_index,
            "output_hash": output_hash,
            "created_at": self.now().isoformat(),
        }
        return PauseDescriptor(
            pause_reason="user_paused",
            review_kind=None,
            step_index=step_index,
            output_hash=output_hash,
        )

    async def _lock_run(self, run: Any) -> None:
        if isinstance(self.session, AsyncSession):
            await self.session.refresh(run, with_for_update=True)
            return
        # Explicit hook for focused transaction doubles. Avoid calling an
        # arbitrary ``refresh`` mock here because route tests use refresh to
        # simulate an external pause at later event boundaries.
        hook = None
        if "lifecycle_lock_run" in vars(self.session):
            hook = self.session.lifecycle_lock_run
        elif callable(getattr(type(self.session), "lifecycle_lock_run", None)):
            hook = self.session.lifecycle_lock_run
        if hook is not None:
            await hook(run)

    async def _verification_failure(
        self,
        run: Any,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        persisted = manifest.get("verification_failure")
        if isinstance(persisted, dict):
            return dict(persisted)

        # Runs paused before lifecycle descriptors were introduced remain
        # resumable, but they must pass through the same exact-hash consent
        # gate. Only the latest persisted step can explain the current pause.
        result = await self.session.execute(
            select(ResearchStep)
            .where(cast(Any, ResearchStep.run_id) == cast(UUID, run.id))
            .order_by(cast(Any, ResearchStep.step_index).desc())
            .limit(1)
        )
        step = result.scalars().first()
        if (
            not isinstance(step, ResearchStep)
            or step.step_type != "verify"
            or not isinstance(step.output, dict)
            or not self._verification_failed(step.output)
        ):
            return None
        return {
            "run_id": str(run.id),
            "step_index": step.step_index,
            "stage_type": "verify",
            "output_hash": canonical_stage_output_hash(step.output),
            "status": "pending",
        }

    @classmethod
    def _verification_descriptor(
        cls, failure: Mapping[str, Any]
    ) -> PauseDescriptor | None:
        index = failure.get("step_index")
        output_hash = failure.get("output_hash")
        if type(index) is not int or index < 0 or not cls._is_hash(output_hash):
            return None
        return PauseDescriptor(
            pause_reason="verification_failed",
            review_kind=None,
            step_index=index,
            output_hash=cast(str, output_hash),
        )

    async def _approved_review(
        self, run: Any, pending: Mapping[str, Any]
    ) -> ResearchStageReview | None:
        review_id = pending.get("review_id")
        if not review_id:
            return None
        try:
            review_uuid = UUID(str(review_id))
        except (TypeError, ValueError):
            return None
        step_index = pending.get("step_index")
        output_hash = pending.get("output_hash")
        if type(step_index) is not int or not self._is_hash(output_hash):
            return None
        step = await self._load_step(cast(UUID, run.id), step_index)
        if (
            step is None
            or not isinstance(step.output, dict)
            or canonical_stage_output_hash(step.output) != output_hash
        ):
            return None
        result = await self.session.execute(
            select(ResearchStageReview).where(
                ResearchStageReview.id == review_uuid,
                ResearchStageReview.run_id == cast(UUID, run.id),
                ResearchStageReview.step_index == step_index,
                ResearchStageReview.review_kind == pending.get("review_kind"),
                ResearchStageReview.output_hash == output_hash,
                ResearchStageReview.decision == "approve",
            )
        )
        return result.scalars().first()

    def _pause_after_step(
        self,
        *,
        run: Any,
        stage_type: str,
        step_index: int,
        output: Mapping[str, Any],
        output_hash: str,
        params: Mapping[str, Any],
        manifest: dict[str, Any],
    ) -> PauseDescriptor | None:
        if stage_type == "verify" and self._verification_failed(output):
            manifest["verification_failure"] = {
                "run_id": str(run.id),
                "step_index": step_index,
                "stage_type": stage_type,
                "output_hash": output_hash,
                "status": "pending",
                "created_at": self.now().isoformat(),
            }
            manifest.pop("resume_authorization", None)
            return PauseDescriptor(
                pause_reason="verification_failed",
                review_kind=None,
                step_index=step_index,
                output_hash=output_hash,
            )

        requested_gate = params.get("review_gate")
        expected_kind = _REVIEW_GATE_BY_STAGE.get(stage_type)
        if expected_kind is None or requested_gate != expected_kind.value:
            return None
        if stage_type == "export" and isinstance(
            manifest.get("verification_override"), dict
        ):
            manifest["final_status"] = "unverified"
            return None

        contract_version = output.get("contract_version")
        if type(contract_version) is not int or contract_version < 1:
            raise ResearchRunLifecycleError(
                status_code=409,
                code="review_required",
                message="Review gate output is missing a contract version",
            )
        manifest["pending_review"] = {
            "run_id": str(run.id),
            "step_index": step_index,
            "stage_type": stage_type,
            "review_kind": expected_kind.value,
            "contract_version": contract_version,
            "output_hash": output_hash,
            "status": "pending",
            "created_at": self.now().isoformat(),
        }
        manifest.pop("resume_authorization", None)
        return PauseDescriptor(
            pause_reason="review_required",
            review_kind=expected_kind,
            step_index=step_index,
            output_hash=output_hash,
        )

    def _authorization(
        self,
        *,
        kind: str,
        actor_id: UUID,
        output_hash: str,
        step_index: int,
        review_id: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": kind,
            "actor_id": str(actor_id),
            "step_index": step_index,
            "output_hash": output_hash,
            "created_at": self.now().isoformat(),
            "consumed_at": None,
        }
        if review_id is not None:
            value["review_id"] = review_id
        return value

    @staticmethod
    def _review_has_no_evidence(review: ResearchStageReview) -> bool:
        items = review.decision_payload.get("items")
        if not isinstance(items, list):
            return False
        accepted = "include" if review.review_kind == "screening" else "accept"
        return not any(
            isinstance(item, dict) and item.get("decision") == accepted
            for item in items
        )

    @staticmethod
    def _verification_failed(output: Mapping[str, Any]) -> bool:
        verification = output.get("verification")
        return (
            isinstance(verification, Mapping) and verification.get("passed") is not True
        )

    @staticmethod
    def _step_parameters(step_definition: Mapping[str, Any]) -> dict[str, Any]:
        value = step_definition.get("params") or step_definition.get("parameters") or {}
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _request_value(request: Any, key: str, default: Any) -> Any:
        if isinstance(request, Mapping):
            return request.get(key, default)
        return getattr(request, key, default)

    @staticmethod
    def _nonnegative_int(value: Any, name: str) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _is_hash(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @classmethod
    def _optional_hash(cls, value: Any) -> str | None:
        return value if cls._is_hash(value) else None

    @staticmethod
    def _final_status(run: Any) -> str | None:
        manifest = getattr(run, "reproducibility_manifest", None)
        if isinstance(manifest, dict) and isinstance(manifest.get("final_status"), str):
            return cast(str, manifest["final_status"])
        return None


__all__ = [
    "PauseDescriptor",
    "PersistedStepTransition",
    "ResearchRunLifecycleError",
    "ResearchRunLifecycleService",
    "ResumeTransition",
    "StreamClaim",
]
