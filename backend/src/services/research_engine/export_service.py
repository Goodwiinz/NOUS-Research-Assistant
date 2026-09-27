"""Export service for research engine results."""

from typing import Any, Dict, List, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.services.research_engine.contracts import rehydrate_stage_outputs
from src.services.research_engine.report_rendering import build_report, render_markdown


class ExportService:
    """Exports research run results as structured JSON reports."""

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
            # Existing immutable legacy rows may have gaps and remain readable.
            typed_context = {}
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
