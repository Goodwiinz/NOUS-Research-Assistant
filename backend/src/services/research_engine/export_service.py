"""Export service for research engine results."""

import copy
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.schemas.research_engine import ExportFormat
from src.services.research_engine.contracts import (
    canonical_json_bytes,
    canonical_json_sha256,
    canonical_markdown_content,
    canonical_stage_output_hash,
    rehydrate_stage_outputs,
)
from src.services.research_engine.observability import (
    ResearchObservability,
    research_observability,
    safely_observe,
)
from src.services.research_engine.report_rendering import (
    build_report,
    render_csv,
    render_markdown,
)

logger = logging.getLogger(__name__)

_SCOPE_FIELDS = (
    "research_question",
    "inclusion_criteria",
    "exclusion_criteria",
    "providers",
    "limit_per_provider",
    "notes",
)


@dataclass(frozen=True)
class ExportArtifact:
    """Transport-ready bytes and safe download metadata."""

    content: bytes
    media_type: str
    filename: str


class ResearchExportError(RuntimeError):
    """Stable, content-free error returned by the owned export route."""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class ExportService:
    """Exports research run results as structured JSON reports."""

    def __init__(self, *, observer: ResearchObservability | None = None) -> None:
        self.observer = observer or research_observability

    async def export(
        self,
        run_id: UUID,
        owner_id: UUID,
        format: ExportFormat | str,
        db: AsyncSession,
    ) -> ExportArtifact:
        """Return an owner-scoped, deterministic artifact for a terminal run."""
        export_format = ExportFormat(format)
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
            .options(
                selectinload(ResearchRun.blueprint),
                selectinload(ResearchRun.steps),
                selectinload(ResearchRun.sources),
                selectinload(ResearchRun.reviews),
            )
        )
        result = await db.execute(statement)
        run = result.scalar_one_or_none()
        if run is None:
            self._record_export_failure(
                run_id=run_id,
                organization_id=None,
                export_format=export_format,
                error_kind="not_found",
            )
            raise ResearchExportError(
                status_code=404,
                code="run_not_found",
                message="Run not found",
            )
        if run.status != "completed":
            self._record_export_failure(
                run_id=run_id,
                organization_id=self._organization_id(run),
                export_format=export_format,
                error_kind="not_completed",
            )
            raise ResearchExportError(
                status_code=409,
                code="run_not_completed",
                message="Run is not completed",
            )

        manifest: dict[str, Any] = (
            cast(Dict[str, Any], run.reproducibility_manifest)
            if isinstance(run.reproducibility_manifest, dict)
            else {}
        )
        steps = sorted(run.steps, key=lambda item: int(item.step_index))
        try:
            context = rehydrate_stage_outputs(steps)
        except ValueError:
            logger.error(
                "Research export reconstruction failed",
                extra={"run_id": str(run.id), "error_category": "invalid_history"},
            )
            safely_observe(
                self.observer,
                "record_rehydration_error",
                run_id=cast(UUID, run.id),
                organization_id=self._organization_id(run),
                error_kind="invalid_history",
            )
            safely_observe(
                self.observer,
                "record_export",
                run_id=cast(UUID, run.id),
                organization_id=self._organization_id(run),
                format=export_format.value,
                outcome="failed",
                error_kind="reconstruction",
            )
            raise ResearchExportError(
                status_code=500,
                code="export_reconstruction_failed",
                message="Export could not be reconstructed from persisted artifacts",
            ) from None
        review_history = self._review_audit(run, manifest)
        approved_reviews = [
            review for review in review_history if review.get("decision") == "approve"
        ]
        audit_context = copy.deepcopy(context)
        audit_context["approved_review_overlays"] = copy.deepcopy(approved_reviews)
        context = self._apply_review_overlays(context, approved_reviews)
        context["scope_confirmation"] = copy.deepcopy(
            manifest.get("scope_confirmation") or {}
        )
        context["provider_manifest"] = copy.deepcopy(
            manifest.get("provider_manifest") or []
        )
        context["approved_review_overlays"] = copy.deepcopy(approved_reviews)
        context["no_evidence"] = manifest.get("final_status") == "no_evidence"
        context["artifact_provenance"] = self._provenance(run, steps, manifest)

        persisted_output = self._persisted_export_output(steps)
        report = self._persisted_report(steps) or build_report(context)
        trusted_verified = self._is_trusted_verified_daily_brief(
            run=run,
            manifest=manifest,
            steps=steps,
            export_output=persisted_output,
        )
        if (
            run.blueprint.template_source == "daily_research_brief"
            and manifest.get("final_status") == "verified"
            and not trusted_verified
        ):
            self._record_export_failure(
                run_id=run_id,
                organization_id=self._organization_id(run),
                export_format=export_format,
                error_kind="attestation",
            )
            raise ResearchExportError(
                status_code=409,
                code="verified_artifact_attestation_invalid",
                message="Verified artifact approval attestation is missing or invalid",
            )
        if run.blueprint.template_source != "daily_research_brief":
            report["final_status"] = "unverified"
            report["warning"] = (
                "UNVERIFIED: legacy and custom runs cannot be approved as a "
                "Daily Research Brief."
            )
        elif manifest.get("final_status") == "no_evidence":
            report["final_status"] = "no_evidence"
            report["warning"] = (
                "NO EVIDENCE: no reader-facing research conclusion was produced."
            )
        elif not trusted_verified:
            report["final_status"] = "unverified"
            report["warning"] = (
                "UNVERIFIED: this artifact has not passed all verification checks."
            )

        base_name = f"daily-research-brief-{run.id}"
        if export_format is ExportFormat.JSON:
            payload = (
                self._verified_download_envelope(
                    report=report,
                    review_history=review_history,
                    attestation=cast(
                        dict[str, Any], manifest["final_approval_attestation"]
                    ),
                )
                if trusted_verified
                else report
            )
            artifact = ExportArtifact(
                content=canonical_json_bytes(payload),
                media_type="application/json",
                filename=f"{base_name}.json",
            )
        elif export_format is ExportFormat.MARKDOWN:
            if report.get("final_status") == "no_evidence":
                self._record_export_failure(
                    run_id=run_id,
                    organization_id=self._organization_id(run),
                    export_format=export_format,
                    error_kind="no_evidence",
                )
                raise ResearchExportError(
                    status_code=409,
                    code="brief_not_available_no_evidence",
                    message=(
                        "No research brief was produced because no evidence remained."
                    ),
                )
            persisted_markdown = (
                self._persisted_markdown(persisted_output) if trusted_verified else None
            )
            markdown = (
                persisted_markdown
                if persisted_markdown is not None
                else render_markdown(report)
            )
            if trusted_verified:
                markdown = self._markdown_with_approval_audit(
                    markdown=markdown,
                    report_hash=cast(str, persisted_output["report_hash"]),
                    review_history=review_history,
                    attestation=cast(
                        dict[str, Any], manifest["final_approval_attestation"]
                    ),
                )
            artifact = ExportArtifact(
                content=markdown.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                filename=f"{base_name}.md",
            )
        else:
            rows = self._csv_rows(audit_context)
            artifact = ExportArtifact(
                content=render_csv(
                    rows,
                    final_status=str(report.get("final_status") or "unverified"),
                    review_history=review_history if trusted_verified else None,
                    final_attestation=(
                        cast(dict[str, Any], manifest["final_approval_attestation"])
                        if trusted_verified
                        else None
                    ),
                ).encode("utf-8"),
                media_type="text/csv; charset=utf-8",
                filename=f"{base_name}.csv",
            )
        safely_observe(
            self.observer,
            "record_export",
            run_id=cast(UUID, run.id),
            organization_id=self._organization_id(run),
            format=export_format.value,
            outcome="success",
            error_kind="none",
        )
        return artifact

    @staticmethod
    def _organization_id(run: ResearchRun) -> UUID | None:
        value = getattr(run, "organization_id", None)
        return value if isinstance(value, UUID) else None

    def _record_export_failure(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        export_format: ExportFormat,
        error_kind: str,
    ) -> None:
        safely_observe(
            self.observer,
            "record_export",
            run_id=run_id,
            organization_id=organization_id,
            format=export_format.value,
            outcome="failed",
            error_kind=error_kind,
        )

    @staticmethod
    def _persisted_export_output(steps: list[Any]) -> dict[str, Any] | None:
        for step in reversed(steps):
            output = step.output if isinstance(step.output, dict) else {}
            if output.get("stage_type") == "export":
                return copy.deepcopy(output)
        return None

    @staticmethod
    def _persisted_report(steps: list[Any]) -> dict[str, Any] | None:
        output = ExportService._persisted_export_output(steps)
        report = output.get("exported") if isinstance(output, dict) else None
        if isinstance(report, dict):
            return copy.deepcopy(report)
        return None

    @staticmethod
    def _persisted_markdown(output: dict[str, Any] | None) -> str | None:
        return canonical_markdown_content(output)

    @staticmethod
    def _verified_download_envelope(
        *,
        report: dict[str, Any],
        review_history: list[dict[str, Any]],
        attestation: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "download_envelope_version": 1,
            "reviewed_artifact": copy.deepcopy(report),
            "reviewed_artifact_hash": attestation["report_hash"],
            "review_history": copy.deepcopy(review_history),
            "final_approval_attestation": copy.deepcopy(attestation),
        }

    @staticmethod
    def _markdown_with_approval_audit(
        *,
        markdown: str,
        report_hash: str,
        review_history: list[dict[str, Any]],
        attestation: dict[str, Any],
    ) -> str:
        reviewed_bytes = markdown.encode("utf-8")
        audit = {
            "reviewed_artifact": {
                "format": "markdown",
                "byte_length": len(reviewed_bytes),
                "sha256": hashlib.sha256(reviewed_bytes).hexdigest(),
                "report_hash": report_hash,
            },
            "review_history": copy.deepcopy(review_history),
            "final_approval_attestation": copy.deepcopy(attestation),
        }
        separator = "" if not markdown or markdown.endswith("\n") else "\n"
        return (
            markdown
            + separator
            + "\n<!-- DAILY_RESEARCH_BRIEF_POST_APPROVAL_AUDIT_V1 -->\n"
            + "## Post-approval audit\n\n"
            + "The reviewed Markdown is the exact byte prefix described below.\n\n"
            + "```json\n"
            + canonical_json_bytes(audit).decode("utf-8")
            + "\n```\n"
        )

    @classmethod
    def _is_trusted_verified_daily_brief(
        cls,
        *,
        run: ResearchRun,
        manifest: dict[str, Any],
        steps: list[Any],
        export_output: dict[str, Any] | None,
    ) -> bool:
        if (
            run.blueprint.template_source != "daily_research_brief"
            or manifest.get("final_status") != "verified"
            or not cls._trusted_scope(manifest)
            or not isinstance(export_output, dict)
            or export_output.get("contract_version") != 1
            or export_output.get("stage_type") != "export"
            or canonical_markdown_content(export_output) is None
        ):
            return False
        report = export_output.get("exported")
        if (
            not isinstance(report, dict)
            or report.get("final_status") != "verified"
            or export_output.get("report_hash") != canonical_json_sha256(report)
        ):
            return False
        verify_outputs = [
            step.output
            for step in steps
            if isinstance(step.output, dict)
            and step.output.get("stage_type") == "verify"
        ]
        if not verify_outputs:
            return False
        verify_output = verify_outputs[-1]
        verification = verify_output.get("verification")
        if (
            not isinstance(verification, dict)
            or verification.get("passed") is not True
            or verification.get("continued_after_failure") is True
            or export_output.get("verification_output_hash")
            != canonical_stage_output_hash(verify_output)
        ):
            return False
        export_step = next(
            (
                step
                for step in reversed(steps)
                if isinstance(step.output, dict)
                and step.output.get("stage_type") == "export"
            ),
            None,
        )
        if export_step is None:
            return False
        output_hash = canonical_stage_output_hash(export_output)
        reviews: list[Any] = run.reviews if isinstance(run.reviews, list) else []
        final_reviews = [
            review
            for review in reviews
            if review.review_kind == "final"
            and review.decision == "approve"
            and int(review.step_index) == int(export_step.step_index)
            and review.output_hash == output_hash
        ]
        if len(final_reviews) != 1:
            return False
        return cls._attestation_matches(
            manifest.get("final_approval_attestation"),
            review=final_reviews[0],
            export_output=export_output,
        )

    @staticmethod
    def _attestation_matches(
        value: Any,
        *,
        review: Any,
        export_output: dict[str, Any],
    ) -> bool:
        if not isinstance(value, dict) or not isinstance(
            getattr(review, "created_at", None), datetime
        ):
            return False
        unsigned = {
            "schema_version": 1,
            "review_id": str(review.id),
            "reviewer_id": str(review.reviewer_id),
            "reviewed_at": review.created_at.isoformat(),
            "decision": str(review.decision),
            "review_kind": str(review.review_kind),
            "step_index": int(review.step_index),
            "output_hash": str(review.output_hash),
            "report_hash": str(export_output.get("report_hash")),
            "verification_output_hash": str(
                export_output.get("verification_output_hash")
            ),
        }
        expected = {**unsigned, "attestation_hash": canonical_json_sha256(unsigned)}
        return value == expected

    @staticmethod
    def _trusted_scope(manifest: dict[str, Any]) -> bool:
        scope = manifest.get("scope_confirmation")
        if not isinstance(scope, dict) or scope.get("confirmed") is not True:
            return False
        try:
            UUID(str(scope.get("confirmed_by")))
            confirmed_at = datetime.fromisoformat(str(scope.get("confirmed_at")))
        except (TypeError, ValueError):
            return False
        if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
            return False
        configuration = {
            "contract_version": 1,
            **{field: copy.deepcopy(scope.get(field)) for field in _SCOPE_FIELDS},
        }
        if scope.get("configuration_hash") != canonical_json_sha256(configuration):
            return False
        parameters = manifest.get("parameters")
        if isinstance(parameters, dict):
            expected = {
                "contract_version": parameters.get("contract_version"),
                **{
                    field: copy.deepcopy(parameters.get(field))
                    for field in _SCOPE_FIELDS
                },
            }
            if expected != configuration:
                return False
        return True

    @staticmethod
    def _provenance(
        run: ResearchRun, steps: list[Any], manifest: dict[str, Any]
    ) -> dict[str, Any]:
        stage_hashes = {
            str(step.step_index): canonical_stage_output_hash(step.output)
            for step in steps
            if isinstance(step.output, dict)
            and step.output.get("stage_type") != "export"
        }
        models = []
        for step_index, definition in enumerate(run.blueprint.steps or []):
            if not isinstance(definition, dict):
                continue
            parameters = definition.get("parameters") or definition.get("params") or {}
            model_id = definition.get("model_id")
            if not model_id and isinstance(parameters, dict):
                model_id = parameters.get("model_id")
            if isinstance(model_id, str) and model_id:
                models.append({"step_index": step_index, "model_id": model_id})
        return {
            **copy.deepcopy(manifest),
            "run_id": str(run.id),
            "blueprint_id": str(run.blueprint.id),
            "blueprint_version": run.blueprint_version,
            "template_source": run.blueprint.template_source,
            "template_contract_version": 1,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "generated_at": manifest.get("generated_at")
            or manifest.get("completed_at")
            or (run.completed_at.isoformat() if run.completed_at else None)
            or (run.started_at.isoformat() if run.started_at else None)
            or (run.created_at.isoformat() if run.created_at else None),
            "stage_hashes": stage_hashes,
            "models": models,
            "limitations": manifest.get("limitations")
            or ["Bounded provider search; results are not exhaustive."],
        }

    @staticmethod
    def _review_audit(
        run: ResearchRun, manifest: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows: list[Any] = run.reviews if isinstance(run.reviews, list) else []
        audit = [
            {
                "review_id": str(review.id),
                "reviewer_id": (
                    str(review.reviewer_id)
                    if getattr(review, "reviewer_id", None) is not None
                    else None
                ),
                "step_index": review.step_index,
                "stage_type": getattr(review, "stage_type", None),
                "review_kind": review.review_kind,
                "output_hash": review.output_hash,
                "decision": review.decision,
                "decision_payload": copy.deepcopy(review.decision_payload),
                "note": getattr(review, "note", None),
                "created_at": (
                    review.created_at.isoformat() if review.created_at else None
                ),
            }
            for review in rows
        ]
        if audit:
            return sorted(audit, key=ExportService._review_sort_key)
        history = manifest.get("review_history")
        return (
            sorted(copy.deepcopy(history), key=ExportService._review_sort_key)
            if isinstance(history, list)
            else []
        )

    @staticmethod
    def _review_sort_key(review: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(review.get("step_index") or 0),
            str(review.get("review_kind") or ""),
            str(review.get("created_at") or ""),
            str(review.get("review_id") or ""),
        )

    @staticmethod
    def _apply_review_overlays(
        context: dict[str, Any], reviews: list[dict[str, Any]]
    ) -> dict[str, Any]:
        projected = copy.deepcopy(context)
        for review in reviews:
            payload = review.get("decision_payload")
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                continue
            decisions = {
                (item.get("source_id"), item.get("part_id")): item.get("decision")
                for item in items
                if isinstance(item, dict)
            }
            if review.get("review_kind") == "screening":
                projected["included_source_ids"] = sorted(
                    {
                        source_id
                        for (source_id, _part_id), decision in decisions.items()
                        if decision == "include" and isinstance(source_id, str)
                    }
                )
            elif review.get("review_kind") == "extraction":
                extractions = projected.get("extractions")
                if isinstance(extractions, list):
                    projected["extractions"] = [
                        copy.deepcopy(record)
                        for record in extractions
                        if isinstance(record, dict)
                        and decisions.get(
                            (record.get("source_id"), record.get("part_id"))
                        )
                        == "accept"
                    ]
        return projected

    @staticmethod
    def _csv_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
        sources = {
            item.get("source_id"): item
            for item in context.get("source_records") or []
            if isinstance(item, dict)
        }
        decisions: dict[tuple[Any, Any], tuple[Any, Any]] = {}
        for review in context.get("approved_review_overlays") or []:
            payload: dict[str, Any] = {}
            if isinstance(review, dict):
                payload_value = review.get("decision_payload")
                if isinstance(payload_value, dict):
                    payload = payload_value
            if (
                not isinstance(review, dict)
                or review.get("review_kind") != "extraction"
            ):
                continue
            for item in payload.get("items") or []:
                if isinstance(item, dict):
                    decisions[(item.get("source_id"), item.get("part_id"))] = (
                        item.get("decision"),
                        item.get("reason"),
                    )
        rows: list[dict[str, Any]] = []
        for extraction in context.get("extractions") or []:
            if not isinstance(extraction, dict):
                continue
            decision, reason = decisions.get(
                (extraction.get("source_id"), extraction.get("part_id")),
                ("included", ""),
            )
            rows.append(
                {
                    "source": sources.get(extraction.get("source_id"), {}),
                    "extraction": extraction,
                    "decision": decision,
                    "reason": reason,
                }
            )
        return rows

    async def export_json(self, run_id: UUID, db: AsyncSession) -> dict:
        """Export a completed run as a structured JSON report.

        Args:
            run_id: The UUID of the research run to export.
            db: An async database session.

        Returns:
            A dictionary containing the full structured report.

        Raises:
            ValueError: If the run does not exist or is not completed.
        """
        # Load run with relationships
        run_stmt = (
            select(ResearchRun)
            .where(ResearchRun.id == run_id)
            .options(
                selectinload(ResearchRun.blueprint),
                selectinload(ResearchRun.steps),
                selectinload(ResearchRun.sources),
            )
        )
        result = await db.execute(run_stmt)
        run = result.scalar_one_or_none()

        if run is None:
            raise ValueError(f"Research run {run_id} not found")

        if run.status != "completed":
            raise ValueError(
                f"Research run {run_id} is not completed (status: {run.status})"
            )

        # Evidence comes only from persisted, validated typed envelopes. The
        # retired evidence table had no writers and is intentionally unused.
        evidence_by_step: Dict[UUID, List[Dict[str, Any]]] = {}
        evidence_ids: set[str] = set()
        for step in run.steps:
            envelope = step.output if isinstance(step.output, dict) else {}
            records = envelope.get("extractions", [])
            step_evidence: list[Dict[str, Any]] = []
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict):
                    continue
                for item in (
                    record.get("evidence", [])
                    if isinstance(record.get("evidence"), list)
                    else []
                ):
                    if not isinstance(item, dict) or not isinstance(
                        item.get("evidence_id"), str
                    ):
                        continue
                    evidence_ids.add(item["evidence_id"])
                    step_evidence.append(
                        {
                            "evidence_id": item["evidence_id"],
                            "source_id": record.get("source_id"),
                            "part_id": item.get("part_id", record.get("part_id")),
                            "pointer": item.get("pointer"),
                            "quote": item.get("quote"),
                            "page_reference": item.get("page_reference"),
                        }
                    )
                for claim in (
                    record.get("claims", [])
                    if isinstance(record.get("claims"), list)
                    else []
                ):
                    if not isinstance(claim, dict) or not isinstance(
                        claim.get("evidence_id"), str
                    ):
                        continue
                    evidence_ids.add(claim["evidence_id"])
                    step_evidence.append(
                        {
                            "evidence_id": claim["evidence_id"],
                            "source_id": record.get("source_id"),
                            "part_id": claim.get("part_id", record.get("part_id")),
                            "claim_text": claim.get("claim_text"),
                            "quote": claim.get("quote"),
                            "page_reference": claim.get("page_reference"),
                        }
                    )
            evidence_by_step[step.id] = step_evidence

        # Build steps section
        steps_data = []
        for step in sorted(run.steps, key=lambda s: s.step_index):
            steps_data.append(
                {
                    "id": str(step.id),
                    "step_index": step.step_index,
                    "step_type": step.step_type,
                    "mode": step.mode,
                    "model_id": step.model_id,
                    "temperature": step.temperature,
                    "seed": step.seed,
                    "inputs_hash": step.inputs_hash,
                    "outputs_hash": step.outputs_hash,
                    "full_prompt": step.full_prompt,
                    "output": step.output,
                    "quality_marks": step.quality_marks,
                    "token_count": step.token_count,
                    "evidence": evidence_by_step.get(step.id, []),
                }
            )

        # Build sources section
        sources_data = []
        for source in run.sources:
            sources_data.append(
                {
                    "id": str(source.id),
                    "connector_type": source.connector_type,
                    "external_id": source.external_id,
                    "title": source.title,
                    "authors": source.authors,
                    "abstract": source.abstract,
                    "url": source.url,
                    "content_hash": source.content_hash,
                }
            )

        try:
            typed_context = rehydrate_stage_outputs(
                sorted(run.steps, key=lambda item: item.step_index)
            )
        except ValueError:
            logger.error(
                "Research export reconstruction failed",
                extra={"run_id": str(run.id), "error_category": "invalid_history"},
            )
            safely_observe(
                self.observer,
                "record_rehydration_error",
                run_id=cast(UUID, run.id),
                organization_id=self._organization_id(run),
                error_kind="invalid_history",
            )
            self._record_export_failure(
                run_id=cast(UUID, run.id),
                organization_id=self._organization_id(run),
                export_format=ExportFormat.JSON,
                error_kind="reconstruction",
            )
            raise ResearchExportError(
                status_code=500,
                code="export_reconstruction_failed",
                message="Export could not be reconstructed from persisted artifacts",
            ) from None
        report = build_report(typed_context)
        manifest: Dict[str, Any] = (
            cast(Dict[str, Any], run.reproducibility_manifest) or {}
        )
        if manifest.get("continued_after_failure"):
            verification = report.get("verification")
            if not isinstance(verification, dict):
                verification = {"available": False, "passed": False}
            verification["passed"] = False
            verification["continued_after_failure"] = True
            report["verification"] = verification
            report["continued_after_failure"] = True
        report["markdown"] = render_markdown(report)
        # Preserve the established export fields while adding typed evidence
        # and verification projections.
        blueprint = run.blueprint
        report.update(
            {
                "run_id": str(run.id),
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": (
                    run.completed_at.isoformat() if run.completed_at else None
                ),
                "total_tokens": run.total_tokens,
                "blueprint": {
                    "id": str(blueprint.id),
                    "name": blueprint.name,
                    "version": blueprint.version,
                    "template_source": blueprint.template_source,
                },
                "steps": steps_data,
                "sources": sources_data,
                "evidence_count": len(evidence_ids),
            }
        )
        if report.get("contract_version") is None:
            report["evidence_status"] = "unavailable_legacy"
            report["verification"] = {
                "available": False,
                "passed": False,
                "semantic_status": "unverified",
                "reason": "Typed verification evidence is unavailable for this legacy run.",
            }

        return report

    async def export_manifest(self, run_id: UUID, db: AsyncSession) -> dict:
        """Export the reproducibility manifest for a run.

        Args:
            run_id: The UUID of the research run.
            db: An async database session.

        Returns:
            The reproducibility manifest dictionary.

        Raises:
            ValueError: If the run does not exist or has no manifest.
        """
        stmt = select(ResearchRun).where(ResearchRun.id == run_id)
        result = await db.execute(stmt)
        run = result.scalar_one_or_none()

        if run is None:
            raise ValueError(f"Research run {run_id} not found")

        if run.reproducibility_manifest is None:
            raise ValueError(f"Research run {run_id} has no reproducibility manifest")

        return cast(Dict[str, Any], run.reproducibility_manifest)
