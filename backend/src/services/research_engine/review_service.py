"""Exact-hash, append-only reviews for persisted research stage outputs."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_stage_review import ResearchStageReview
from src.models.research_step import ResearchStep
from src.schemas.research_engine import (
    MAX_NESTED_PAYLOAD_BYTES,
    MAX_PENDING_REVIEW_OUTPUT_BYTES,
    MAX_REVIEW_ITEMS,
    MAX_REVIEW_SAFE_FIELD_CHARS,
    ExtractionReviewDecisionPayload,
    PendingReviewResponse,
    ReviewDecision,
    ReviewDescriptor,
    ReviewKind,
    ReviewValidationVocabulary,
    ScreeningReviewDecisionPayload,
    StageReviewRequest,
    StageReviewResponse,
    serialized_payload_size,
)
from src.services.research_engine.contracts import (
    canonical_json_sha256,
    canonical_stage_output_hash,
)
from src.services.research_engine.observability import (
    ResearchObservability,
    research_observability,
    safely_observe,
)


class ResearchReviewError(ValueError):
    """Safe service error for the public review API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        descriptor: ReviewDescriptor | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.descriptor = (
            ReviewDescriptor.model_validate(descriptor)
            if descriptor is not None
            else None
        )


@dataclass(frozen=True)
class _CanonicalDecision:
    decision: str
    payload: dict[str, Any]
    note: str | None


_STAGE_FOR_KIND: dict[str, str] = {
    ReviewKind.SCREENING.value: "screen",
    ReviewKind.EXTRACTION.value: "extract",
    ReviewKind.FINAL.value: "export",
}


class ResearchReviewService:
    """Validate and append decisions bound to complete persisted stage envelopes."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        observer: ResearchObservability | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session = session
        self.observer = observer or research_observability
        self.now = now

    async def get_pending_review(
        self, *, run_id: UUID, owner_id: UUID
    ) -> PendingReviewResponse:
        run = await self._load_owned_run(run_id, owner_id, lock=False)
        pending = self._pending_mapping(run)
        if pending is None:
            return PendingReviewResponse(pending=False)

        step_index = self._integer_field(pending, "step_index")
        persisted_run_id = cast(UUID, run.id)
        step = await self._load_step(persisted_run_id, step_index, lock=False)
        if step is None or not isinstance(step.output, dict):
            raise self._review_required()

        output = copy.deepcopy(step.output)
        output_hash = canonical_stage_output_hash(output)
        descriptor = self._validate_gate(run, step, pending, output_hash)
        accepted = await self._load_review(
            run_id=persisted_run_id,
            step_index=cast(int, step.step_index),
            output_hash=output_hash,
            review_kind=descriptor.review_kind.value,
            lock=False,
        )
        validation = self._validation_vocabulary(descriptor.review_kind)
        return PendingReviewResponse(
            pending=True,
            descriptor=descriptor,
            stage_output=self._pending_stage_output(output),
            accepted_review=(
                self._response(accepted, replay=True) if accepted is not None else None
            ),
            validation=validation,
        )

    @classmethod
    def _pending_stage_output(cls, output: Mapping[str, Any]) -> dict[str, Any]:
        """Project oversized output to the complete ordered review identity set."""

        copied = copy.deepcopy(dict(output))
        if serialized_payload_size(copied) <= MAX_NESTED_PAYLOAD_BYTES:
            return copied

        stage_type = output.get("stage_type")
        collection_key = (
            {
                "screen": "screening",
                "extract": "extractions",
            }.get(stage_type)
            if isinstance(stage_type, str)
            else None
        )
        projected: dict[str, Any] = {
            "contract_version": output.get("contract_version"),
            "stage_type": stage_type,
            "review_projection": {
                "projected": True,
                "truncated": True,
                "identity_complete": True,
            },
        }
        if collection_key is not None:
            records = output.get(collection_key)
            if not isinstance(records, list) or len(records) > MAX_REVIEW_ITEMS:
                raise cls._review_required()
            projected_items: list[dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    raise cls._review_required()
                source_id = record.get("source_id")
                part_id = record.get("part_id")
                if not isinstance(source_id, str) or not isinstance(part_id, str):
                    raise cls._review_required()
                item: dict[str, Any] = {
                    "source_id": source_id,
                    "part_id": part_id,
                }
                evidence_level = record.get("evidence_level")
                if (
                    isinstance(evidence_level, str)
                    and len(evidence_level) <= MAX_REVIEW_SAFE_FIELD_CHARS
                ):
                    item["evidence_level"] = evidence_level
                included = record.get("included")
                if isinstance(included, bool):
                    item["included"] = included
                projected_items.append(item)
            projected[collection_key] = projected_items
        elif stage_type == "export":
            for field in (
                "format",
                "verification_output_hash",
                "report_hash",
            ):
                field_value = output.get(field)
                if isinstance(field_value, str):
                    projected[field] = field_value
        else:
            raise cls._review_required()

        if serialized_payload_size(projected) > MAX_PENDING_REVIEW_OUTPUT_BYTES:
            raise cls._review_required()
        return projected

    async def submit_review(
        self,
        *,
        run_id: UUID,
        step_index: int,
        owner_id: UUID,
        organization_id: UUID | None,
        reviewer_id: UUID,
        request: StageReviewRequest,
    ) -> StageReviewResponse:
        # Authentication and other read dependencies can leave SQLAlchemy's
        # implicit transaction open on the request-scoped session. End that
        # read transaction before entering the review's owned write boundary.
        if self.session.in_transaction():
            await self.session.rollback()
        canonical = self._canonical_decision(request)
        try:
            response, wait_seconds = await self._submit_transaction(
                run_id=run_id,
                step_index=step_index,
                owner_id=owner_id,
                organization_id=organization_id,
                reviewer_id=reviewer_id,
                request=request,
                canonical=canonical,
            )
            if not response.replay:
                self._record_review_observability(
                    run_id=run_id,
                    organization_id=organization_id,
                    request=request,
                    wait_seconds=wait_seconds,
                )
            return response
        except ResearchReviewError as error:
            safely_observe(
                self.observer,
                "record_review",
                run_id=run_id,
                organization_id=organization_id,
                review_kind=request.review_kind.value,
                outcome=(
                    "stale"
                    if error.code
                    in {"review_output_stale", "verification_output_stale"}
                    else "failed"
                ),
                wait_seconds=0.0,
            )
            raise
        except IntegrityError:
            # A concurrent winner may have committed the unique gate while this
            # transaction waited. Reload its immutable row after clearing the
            # failed transaction and compare the canonical decision.
            await self.session.rollback()
            existing = await self._load_review(
                run_id=run_id,
                step_index=step_index,
                output_hash=request.output_hash,
                review_kind=request.review_kind.value,
                lock=False,
            )
            if existing is None:
                raise
            response = self._resolve_existing(existing, canonical)
            if not response.replay:
                self._record_review_observability(
                    run_id=run_id,
                    organization_id=organization_id,
                    request=request,
                    wait_seconds=0.0,
                )
            return response

    async def _submit_transaction(
        self,
        *,
        run_id: UUID,
        step_index: int,
        owner_id: UUID,
        organization_id: UUID | None,
        reviewer_id: UUID,
        request: StageReviewRequest,
        canonical: _CanonicalDecision,
    ) -> tuple[StageReviewResponse, float]:
        async with self.session.begin():
            shared_lock = request.decision == ReviewDecision.DECLINE
            run = await self._load_owned_run(
                run_id,
                owner_id,
                lock=True,
                shared=shared_lock,
            )
            pending = self._pending_mapping(run)
            if pending is None or run.status != "paused":
                raise self._review_required()

            descriptor_index = self._integer_field(pending, "step_index")
            if descriptor_index != step_index:
                raise self._review_required()
            persisted_run_id = cast(UUID, run.id)
            step = await self._load_step(
                persisted_run_id,
                step_index,
                lock=True,
                shared=shared_lock,
            )
            if step is None or not isinstance(step.output, dict):
                raise self._review_required()

            output = copy.deepcopy(step.output)
            output_hash = canonical_stage_output_hash(output)
            descriptor = self._validate_gate(run, step, pending, output_hash)

            if request.output_hash != output_hash:
                raise self._stale(descriptor)
            if request.review_kind.value != descriptor.review_kind.value:
                raise self._review_required(descriptor)

            self._validate_exact_payload(
                request=request,
                output=output,
                decision=canonical,
            )
            if (
                request.review_kind == ReviewKind.FINAL
                and request.decision == ReviewDecision.APPROVE
            ):
                await self._validate_final_approval(run, step, output)

            existing = await self._load_review(
                run_id=persisted_run_id,
                step_index=cast(int, step.step_index),
                output_hash=output_hash,
                review_kind=request.review_kind.value,
                lock=True,
                shared=shared_lock,
            )
            if existing is not None:
                return self._resolve_existing(existing, canonical), self._review_wait(
                    descriptor
                )

            review = ResearchStageReview(
                owner_id=owner_id,
                organization_id=organization_id,
                run_id=persisted_run_id,
                step_index=cast(int, step.step_index),
                stage_type=step.step_type,
                review_kind=request.review_kind.value,
                reviewer_id=reviewer_id,
                output_hash=output_hash,
                decision=canonical.decision,
                decision_payload=canonical.payload,
                note=canonical.note,
            )
            self.session.add(review)
            await self.session.flush()

            if request.decision == ReviewDecision.APPROVE:
                manifest = copy.deepcopy(run.reproducibility_manifest or {})
                approved = dict(pending)
                approved["status"] = "approved"
                approved["review_id"] = str(review.id)
                manifest["pending_review"] = approved
                run.reproducibility_manifest = manifest

            return self._response(review, replay=False), self._review_wait(descriptor)

    def _review_wait(self, descriptor: ReviewDescriptor) -> float:
        created_at = descriptor.created_at
        if created_at is None:
            return 0.0
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            return 0.0
        return max(0.0, (self.now() - created_at).total_seconds())

    @staticmethod
    def _iso_timestamp(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).isoformat()
            except ValueError:
                return None
        return None

    def _record_review_observability(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        request: StageReviewRequest,
        wait_seconds: float,
    ) -> None:
        outcome = (
            "approved" if request.decision == ReviewDecision.APPROVE else "declined"
        )
        safely_observe(
            self.observer,
            "record_review",
            run_id=run_id,
            organization_id=organization_id,
            review_kind=request.review_kind.value,
            outcome=outcome,
            wait_seconds=wait_seconds,
        )
        if (
            request.review_kind == ReviewKind.EXTRACTION
            and request.decision == ReviewDecision.APPROVE
        ):
            payload = request.decision_payload.model_dump(
                mode="json", exclude_none=True
            )
            items = payload.get("items") if isinstance(payload, dict) else None
            for item_outcome in ("accept", "reject"):
                count = sum(
                    1
                    for item in items or []
                    if isinstance(item, dict) and item.get("decision") == item_outcome
                )
                if count:
                    safely_observe(
                        self.observer,
                        "record_extraction_decision",
                        run_id=run_id,
                        organization_id=organization_id,
                        outcome=item_outcome,
                        count=count,
                    )

    async def apply_approved_overlays(
        self,
        *,
        run_id: UUID,
        owner_id: UUID,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return deterministic downstream projections without rewriting outputs."""

        run = await self._load_owned_run(run_id, owner_id, lock=False)
        reviews_result = await self.session.execute(
            select(ResearchStageReview)
            .where(
                ResearchStageReview.run_id == cast(UUID, run.id),
                ResearchStageReview.decision == ReviewDecision.APPROVE.value,
            )
            .order_by(ResearchStageReview.step_index, ResearchStageReview.created_at)
        )
        reviews = list(reviews_result.scalars().all())
        steps_result = await self.session.execute(
            select(ResearchStep).where(
                cast(Any, ResearchStep.run_id) == cast(UUID, run.id)
            )
        )
        steps = {step.step_index: step for step in steps_result.scalars().all()}
        projected = copy.deepcopy(dict(context))
        audit: list[dict[str, Any]] = []

        for review in reviews:
            step = steps.get(review.step_index)
            if step is None or not isinstance(step.output, dict):
                continue
            if canonical_stage_output_hash(step.output) != review.output_hash:
                continue
            items = review.decision_payload.get("items", [])
            if not isinstance(items, list):
                continue
            decisions = {
                (item.get("source_id"), item.get("part_id")): item.get("decision")
                for item in items
                if isinstance(item, dict)
            }
            if review.review_kind == ReviewKind.SCREENING.value:
                included = sorted(
                    {
                        source_id
                        for (source_id, _part_id), decision in decisions.items()
                        if decision == "include" and isinstance(source_id, str)
                    }
                )
                projected["included_source_ids"] = included
            elif review.review_kind == ReviewKind.EXTRACTION.value:
                source_records = projected.get("extractions")
                if not isinstance(source_records, list):
                    source_records = step.output.get("extractions", [])
                projected["extractions"] = [
                    copy.deepcopy(record)
                    for record in source_records
                    if isinstance(record, dict)
                    and decisions.get((record.get("source_id"), record.get("part_id")))
                    == "accept"
                ]
            audit.append(
                {
                    "review_id": str(review.id),
                    "step_index": review.step_index,
                    "review_kind": review.review_kind,
                    "output_hash": review.output_hash,
                    "decision": review.decision,
                    "decision_payload": copy.deepcopy(review.decision_payload),
                    "created_at": self._iso_timestamp(review.created_at),
                }
            )

        if audit:
            projected["approved_review_overlays"] = audit
        return projected

    async def _load_owned_run(
        self,
        run_id: UUID,
        owner_id: UUID,
        *,
        lock: bool,
        shared: bool = False,
    ) -> ResearchRun:
        statement = (
            select(ResearchRun)
            .join(
                ResearchBlueprint,
                cast(Any, ResearchBlueprint.id) == cast(Any, ResearchRun.blueprint_id),
            )
            .join(
                ResearchProject,
                cast(Any, ResearchProject.id)
                == cast(Any, ResearchBlueprint.project_id),
            )
            .where(
                cast(Any, ResearchRun.id) == run_id,
                cast(Any, ResearchProject.owner_id) == owner_id,
            )
        )
        if lock:
            statement = statement.with_for_update(read=shared)
        result = await self.session.execute(statement)
        run = result.scalars().first()
        if run is None:
            raise ResearchReviewError(
                status_code=404,
                code="review_not_found",
                message="Run not found",
            )
        return run

    async def _load_step(
        self,
        run_id: UUID,
        step_index: int,
        *,
        lock: bool,
        shared: bool = False,
    ) -> ResearchStep | None:
        statement = select(ResearchStep).where(
            cast(Any, ResearchStep.run_id) == run_id,
            cast(Any, ResearchStep.step_index) == step_index,
        )
        if lock:
            statement = statement.with_for_update(read=shared)
        result = await self.session.execute(statement)
        return result.scalars().first()

    async def _load_review(
        self,
        *,
        run_id: UUID,
        step_index: int,
        output_hash: str,
        review_kind: str,
        lock: bool,
        shared: bool = False,
    ) -> ResearchStageReview | None:
        statement = select(ResearchStageReview).where(
            ResearchStageReview.run_id == run_id,
            ResearchStageReview.step_index == step_index,
            ResearchStageReview.output_hash == output_hash,
            ResearchStageReview.review_kind == review_kind,
        )
        if lock:
            statement = statement.with_for_update(read=shared)
        result = await self.session.execute(statement)
        return result.scalars().first()

    @staticmethod
    def _pending_mapping(run: ResearchRun) -> dict[str, Any] | None:
        manifest = run.reproducibility_manifest
        if not isinstance(manifest, dict):
            return None
        pending = manifest.get("pending_review")
        return dict(pending) if isinstance(pending, dict) else None

    @staticmethod
    def _integer_field(value: Mapping[str, Any], key: str) -> int:
        field = value.get(key)
        if type(field) is not int or field < 0:
            raise ResearchReviewService._review_required()
        return field

    def _validate_gate(
        self,
        run: ResearchRun,
        step: ResearchStep,
        pending: Mapping[str, Any],
        output_hash: str,
    ) -> ReviewDescriptor:
        contract_version = (
            step.output.get("contract_version")
            if isinstance(step.output, dict)
            else None
        )
        review_kind = pending.get("review_kind")
        expected_stage = (
            _STAGE_FOR_KIND.get(review_kind) if isinstance(review_kind, str) else None
        )
        if (
            pending.get("run_id") != str(run.id)
            or pending.get("step_index") != step.step_index
            or pending.get("stage_type") != step.step_type
            or expected_stage != step.step_type
            or step.output.get("stage_type") != step.step_type
            or not isinstance(contract_version, int)
            or isinstance(contract_version, bool)
            or contract_version < 1
            or pending.get("contract_version") != contract_version
            or pending.get("status") not in {"pending", "approved"}
        ):
            raise self._review_required()

        descriptor = ReviewDescriptor.model_validate(
            {
                "run_id": run.id,
                "step_index": step.step_index,
                "stage_type": step.step_type,
                "review_kind": review_kind,
                "contract_version": contract_version,
                "output_hash": output_hash,
                "status": pending.get("status"),
                "created_at": pending.get("created_at"),
                "review_id": pending.get("review_id"),
            }
        )
        if pending.get("output_hash") != output_hash:
            raise self._stale(descriptor)
        return descriptor

    @staticmethod
    def _canonical_decision(request: StageReviewRequest) -> _CanonicalDecision:
        payload = request.decision_payload.model_dump(mode="json", exclude_none=True)
        items = payload.get("items")
        if isinstance(items, list):
            payload["items"] = sorted(
                items,
                key=lambda item: (item["source_id"], item["part_id"]),
            )
        return _CanonicalDecision(
            decision=request.decision.value,
            payload=payload,
            note=request.note,
        )

    def _validate_exact_payload(
        self,
        *,
        request: StageReviewRequest,
        output: Mapping[str, Any],
        decision: _CanonicalDecision,
    ) -> None:
        if request.review_kind == ReviewKind.FINAL:
            if decision.payload:
                raise self._payload_incomplete()
            return

        output_key = (
            "screening"
            if request.review_kind == ReviewKind.SCREENING
            else "extractions"
        )
        records = output.get(output_key)
        items = decision.payload.get("items")
        if not isinstance(records, list) or not isinstance(items, list):
            raise self._payload_incomplete()
        expected = self._identities(records)
        submitted = self._identities(items)
        if (
            len(expected) != len(records)
            or len(submitted) != len(items)
            or submitted != expected
        ):
            raise self._payload_incomplete()

        if request.decision == ReviewDecision.APPROVE:
            if any(item.get("decision") == "unresolved" for item in items):
                raise self._payload_incomplete()

    @staticmethod
    def _identities(records: Sequence[Any]) -> set[tuple[str, str]]:
        identities: set[tuple[str, str]] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            source_id = record.get("source_id")
            part_id = record.get("part_id")
            if isinstance(source_id, str) and isinstance(part_id, str):
                identities.add((source_id, part_id))
        return identities

    async def _validate_final_approval(
        self,
        run: ResearchRun,
        export_step: ResearchStep,
        export_output: Mapping[str, Any],
    ) -> None:
        result = await self.session.execute(
            select(ResearchStep)
            .where(
                cast(Any, ResearchStep.run_id) == cast(UUID, run.id),
                cast(Any, ResearchStep.step_index) < cast(int, export_step.step_index),
                cast(Any, ResearchStep.step_type) == "verify",
            )
            .order_by(cast(Any, ResearchStep.step_index).desc())
            .limit(1)
            .with_for_update()
        )
        verify_step = result.scalars().first()
        if verify_step is None or not isinstance(verify_step.output, dict):
            raise self._payload_incomplete()
        verification = verify_step.output.get("verification")
        if not isinstance(verification, dict):
            raise self._payload_incomplete()
        claims = verification.get("claims")
        verified = (
            verify_step.output.get("contract_version") == 1
            and verify_step.output.get("stage_type") == "verify"
            and verification.get("passed") is True
            and verification.get("deterministic_passed") is True
            and verification.get("schema_passed") is True
            and verification.get("semantic_status") == "supported"
            and verification.get("coverage_complete") is True
            and verification.get("continued_after_failure") is False
            and isinstance(claims, list)
            and all(
                isinstance(claim, dict) and claim.get("status") == "supported"
                for claim in claims
            )
        )
        verification_hash = canonical_stage_output_hash(verify_step.output)
        if (
            not verified
            or export_output.get("verification_output_hash") != verification_hash
        ):
            raise self._payload_incomplete()

        report = export_output.get("artifact")
        if report is None:
            report = export_output.get("exported")
        if not isinstance(report, dict):
            raise self._payload_incomplete()
        report_claims = report.get("claims")
        report_is_verified = (
            report.get("final_status") == "verified"
            and isinstance(report_claims, list)
            and all(
                isinstance(claim, dict)
                and isinstance(claim.get("evidence_ids"), list)
                and bool(claim["evidence_ids"])
                for claim in report_claims
            )
        )
        if not report_is_verified or export_output.get(
            "report_hash"
        ) != canonical_json_sha256(report):
            raise self._payload_incomplete()

    @staticmethod
    def _validation_vocabulary(kind: ReviewKind) -> ReviewValidationVocabulary:
        if kind == ReviewKind.SCREENING:
            return ReviewValidationVocabulary(
                item_decisions=["include", "exclude", "unresolved"],
                reason_required_for=["exclude"],
            )
        if kind == ReviewKind.EXTRACTION:
            return ReviewValidationVocabulary(
                item_decisions=["accept", "reject", "unresolved"],
                reason_required_for=["reject"],
            )
        return ReviewValidationVocabulary()

    def _resolve_existing(
        self, existing: ResearchStageReview, canonical: _CanonicalDecision
    ) -> StageReviewResponse:
        if (
            existing.decision == canonical.decision
            and existing.decision_payload == canonical.payload
            and existing.note == canonical.note
        ):
            return self._response(existing, replay=True)
        raise ResearchReviewError(
            status_code=409,
            code="review_decision_conflict",
            message="A different review decision already exists",
        )

    @staticmethod
    def _response(review: ResearchStageReview, *, replay: bool) -> StageReviewResponse:
        return StageReviewResponse.model_validate(
            {
                "id": review.id,
                "run_id": review.run_id,
                "step_index": review.step_index,
                "stage_type": review.stage_type,
                "review_kind": review.review_kind,
                "reviewer_id": review.reviewer_id,
                "output_hash": review.output_hash,
                "decision": review.decision,
                "decision_payload": copy.deepcopy(review.decision_payload),
                "note": review.note,
                "created_at": review.created_at,
                "replay": replay,
            }
        )

    @staticmethod
    def _review_required(
        descriptor: ReviewDescriptor | None = None,
    ) -> ResearchReviewError:
        return ResearchReviewError(
            status_code=409,
            code="review_required",
            message="The current run state does not accept this review",
            descriptor=descriptor,
        )

    @staticmethod
    def _stale(descriptor: ReviewDescriptor) -> ResearchReviewError:
        return ResearchReviewError(
            status_code=409,
            code="review_output_stale",
            message="Review output is stale",
            descriptor=descriptor,
        )

    @staticmethod
    def _payload_incomplete() -> ResearchReviewError:
        return ResearchReviewError(
            status_code=422,
            code="review_payload_incomplete",
            message="Review decisions must cover the exact persisted item set",
        )
