"""Workflow engine that orchestrates research blueprint execution."""

import asyncio
import inspect
import logging
import time
from typing import Any, AsyncGenerator, Callable, Dict
from uuid import UUID

from src.schemas.research_engine import validate_blueprint_runtime
from src.services.research_engine.observability import (
    ResearchObservability,
    research_observability,
    safely_observe,
)
from src.services.research_engine.step_executor import (
    ExecutionBudget,
    StepExecutionError,
    StepExecutor,
)

MAX_RUN_TOKENS = 50_000
MAX_RUN_WALL_TIME_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Runs a research blueprint, yielding events for each step."""

    def __init__(
        self,
        step_executor: StepExecutor,
        pause_on_quality_failure: bool = True,
        max_total_tokens: int = MAX_RUN_TOKENS,
        max_wall_time_seconds: float = MAX_RUN_WALL_TIME_SECONDS,
        clock: Callable[[], float] = time.time,
        observer: ResearchObservability | None = None,
    ) -> None:
        self.step_executor = step_executor
        self.pause_on_quality_failure = pause_on_quality_failure
        self.max_total_tokens = max_total_tokens
        self.max_wall_time_seconds = max_wall_time_seconds
        self.clock = clock
        self.observer = observer or research_observability

    async def run(
        self,
        blueprint: Dict,
        run_id: UUID,
        start_from_step: int = 0,
        initial_context: Dict[str, Any] | None = None,
        initial_total_tokens: int = 0,
        started_at: float | None = None,
        organization_id: UUID | None = None,
        record_run_started: bool = True,
    ) -> AsyncGenerator[Dict, None]:
        """Execute a blueprint and yield events as dicts.

        Events emitted:
            run_start, step_start, step_complete, step_error,
            run_paused, run_complete, run_failed
        """
        steps = blueprint.get("steps", [])
        context: Dict[str, Any] = dict(blueprint.get("parameters") or {})
        if initial_context:
            # R5-M19: persisted outputs of already-completed steps
            context.update(initial_context)

        try:
            validate_blueprint_runtime(blueprint)
        except ValueError:
            safely_observe(
                self.observer,
                "record_validation_error",
                run_id=run_id,
                organization_id=organization_id,
                stage_type="blueprint",
                error_kind="runtime_limits",
                count=1,
            )
            safely_observe(
                self.observer,
                "record_run",
                run_id=run_id,
                organization_id=organization_id,
                status="failed",
            )
            yield {
                "event": "run_failed",
                "run_id": str(run_id),
                "error": "Blueprint exceeds a server-owned execution limit",
            }
            return

        started_at = self.clock() if started_at is None else started_at
        total_tokens = max(0, int(initial_total_tokens or 0))
        prior_steps = list((context.get("stage_results") or {}).values())
        prior_calls = sum(
            max(0, int((item.get("usage") or {}).get("model_calls", 0)))
            for item in prior_steps
            if isinstance(item, dict)
        )
        budget = ExecutionBudget(
            max_total_tokens=self.max_total_tokens,
            used_tokens=total_tokens,
            used_calls=prior_calls,
        )

        if record_run_started:
            safely_observe(
                self.observer,
                "record_run",
                run_id=run_id,
                organization_id=organization_id,
                status="started",
            )

        yield {"event": "run_start", "run_id": str(run_id), "total_steps": len(steps)}

        try:
            for idx, step_def in enumerate(
                steps[start_from_step:], start=start_from_step
            ):
                if total_tokens >= self.max_total_tokens:
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run token budget exhausted",
                    }
                    return
                if self.clock() - started_at >= self.max_wall_time_seconds:
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run wall-time budget exhausted",
                    }
                    return

                step_id = step_def.get("id", f"step_{idx}")

                yield {
                    "event": "step_start",
                    "run_id": str(run_id),
                    "step_index": idx,
                    "step_id": step_id,
                }

                stage_started = time.monotonic()
                try:
                    if step_def.get("type") == "search":
                        # Previous search outputs (including resumed ones) contain
                        # their effective selection, not the blueprint default.
                        context["selected_sources"] = (
                            blueprint.get("parameters") or {}
                        ).get("sources", [])
                    remaining_wall_time = self.max_wall_time_seconds - (
                        self.clock() - started_at
                    )
                    if remaining_wall_time <= 0:
                        safely_observe(
                            self.observer,
                            "record_run",
                            run_id=run_id,
                            organization_id=organization_id,
                            status="failed",
                        )
                        yield {
                            "event": "run_failed",
                            "run_id": str(run_id),
                            "error": "Run wall-time budget exhausted",
                        }
                        return
                    budget.active_step_index = idx
                    execute = self.step_executor.execute
                    try:
                        parameters = inspect.signature(execute).parameters.values()
                        accepts_budget = any(
                            parameter.name == "budget"
                            or parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters
                        )
                    except (TypeError, ValueError):
                        accepts_budget = isinstance(self.step_executor, StepExecutor)
                    execution = (
                        execute(step_def, context, budget=budget)
                        if accepts_budget
                        else execute(step_def, context)
                    )
                    result = await asyncio.wait_for(
                        execution,
                        timeout=remaining_wall_time,
                    )
                except asyncio.TimeoutError:
                    safely_observe(
                        self.observer,
                        "record_stage_duration",
                        run_id=run_id,
                        organization_id=organization_id,
                        step_index=idx,
                        stage_type=str(step_def.get("type") or "unknown"),
                        duration_seconds=time.monotonic() - stage_started,
                        status="failed",
                    )
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run wall-time budget exhausted",
                    }
                    return
                except StepExecutionError as exc:
                    consumed_tokens = max(0, int(exc.consumed_tokens))
                    total_tokens += consumed_tokens
                    safely_observe(
                        self.observer,
                        "record_stage_duration",
                        run_id=run_id,
                        organization_id=organization_id,
                        step_index=idx,
                        stage_type=str(step_def.get("type") or "unknown"),
                        duration_seconds=time.monotonic() - stage_started,
                        status="failed",
                    )
                    safely_observe(
                        self.observer,
                        "record_validation_error",
                        run_id=run_id,
                        organization_id=organization_id,
                        stage_type=str(step_def.get("type") or "unknown"),
                        error_kind="step_execution",
                        count=1,
                    )
                    yield {
                        "event": "step_error",
                        "run_id": str(run_id),
                        "step_index": idx,
                        "step_id": step_id,
                        "error": "Research step execution failed",
                        "error_category": "step_execution_error",
                        "consumed_tokens": consumed_tokens,
                        "model_calls": max(0, int(exc.model_calls)),
                        "batch_metadata": exc.batch_metadata,
                    }
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Research step execution failed",
                        "error_category": "step_execution_error",
                        "step_index": idx,
                        "step_id": step_id,
                    }
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    return
                except Exception:
                    logger.error(
                        "Research step execution failed",
                        extra={
                            "run_id": str(run_id),
                            "step_index": idx,
                            "error_category": "unexpected_step_error",
                        },
                    )
                    safely_observe(
                        self.observer,
                        "record_stage_duration",
                        run_id=run_id,
                        organization_id=organization_id,
                        step_index=idx,
                        stage_type=str(step_def.get("type") or "unknown"),
                        duration_seconds=time.monotonic() - stage_started,
                        status="failed",
                    )
                    safely_observe(
                        self.observer,
                        "record_validation_error",
                        run_id=run_id,
                        organization_id=organization_id,
                        stage_type=str(step_def.get("type") or "unknown"),
                        error_kind="unexpected_step_error",
                        count=1,
                    )
                    yield {
                        "event": "step_error",
                        "run_id": str(run_id),
                        "step_index": idx,
                        "step_id": step_id,
                        "error": "Research step execution failed",
                        "error_category": "unexpected_step_error",
                    }
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Research step execution failed",
                        "error_category": "unexpected_step_error",
                        "step_index": idx,
                        "step_id": step_id,
                    }
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    return

                stage_type = str(step_def.get("type") or "unknown")
                safely_observe(
                    self.observer,
                    "record_stage_duration",
                    run_id=run_id,
                    organization_id=organization_id,
                    step_index=idx,
                    stage_type=stage_type,
                    duration_seconds=time.monotonic() - stage_started,
                    status="completed",
                )
                self._record_stage_outcomes(
                    run_id=run_id,
                    organization_id=organization_id,
                    stage_type=stage_type,
                    output=result.output,
                )

                # Versioned envelopes merge only stage-owned data. Legacy
                # custom executors retain their existing output shape.
                from src.services.research_engine.contracts import (
                    canonical_stage_output_hash,
                    merge_stage_output,
                    no_evidence_reason,
                )

                context = merge_stage_output(context, result.output, idx)
                persisted_output_hash = (
                    canonical_stage_output_hash(result.output)
                    if result.output.get("contract_version") == 1
                    else result.outputs_hash
                )
                total_tokens += max(0, int(result.token_count or 0))

                quality_marks_data = [
                    {
                        "check_type": qm.check_type,
                        "passed": qm.passed,
                        "details": qm.details,
                    }
                    for qm in result.quality_marks
                ]

                yield {
                    "event": "step_complete",
                    "run_id": str(run_id),
                    "step_index": idx,
                    "step_id": step_id,
                    "output": result.output,
                    "step_type": step_def.get("type", ""),
                    "inputs_hash": result.inputs_hash,
                    "outputs_hash": persisted_output_hash,
                    "full_prompt": result.full_prompt,
                    "prompt_metadata": result.prompt_metadata,
                    "quality_marks": quality_marks_data,
                    "token_count": result.token_count,
                    "model_id": result.model_id,
                    "model_version": result.model_version,
                    "temperature": result.temperature,
                    "seed": result.seed,
                }

                if total_tokens > self.max_total_tokens:
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="failed",
                    )
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run token budget exhausted",
                    }
                    return

                terminal_reason = no_evidence_reason(result.output)
                if terminal_reason is not None:
                    safely_observe(
                        self.observer,
                        "record_run",
                        run_id=run_id,
                        organization_id=organization_id,
                        status="no_evidence",
                    )
                    yield {
                        "event": "run_complete",
                        "run_id": str(run_id),
                        "final_status": "no_evidence",
                        "terminal_reason": terminal_reason,
                    }
                    return

                params = step_def.get("params") or step_def.get("parameters") or {}
                review_gate = (
                    params.get("review_gate") if isinstance(params, dict) else None
                )
                if (
                    review_gate == "final"
                    and context.get("continued_after_failure") is True
                ):
                    review_gate = None
                event_output_hash = (
                    persisted_output_hash or canonical_stage_output_hash(result.output)
                )
                verification = result.output.get("verification")
                if (
                    step_def.get("type") == "verify"
                    and isinstance(verification, dict)
                    and verification.get("passed") is not True
                ):
                    yield {
                        "event": "run_paused",
                        "run_id": str(run_id),
                        "pause_reason": "verification_failed",
                        "step_index": idx,
                        "output_hash": event_output_hash,
                    }
                    return
                if review_gate in {"screening", "extraction", "final"}:
                    yield {
                        "event": "run_paused",
                        "run_id": str(run_id),
                        "step_index": idx,
                        "step_id": step_id,
                        "reason": "Review required",
                        "pause_reason": "review_required",
                        "review_kind": review_gate,
                        "output_hash": event_output_hash,
                    }
                    return

                # Legacy quality-only pauses still expose a bounded descriptor.
                has_failure = any(not qm.passed for qm in result.quality_marks)
                if has_failure and self.pause_on_quality_failure:
                    yield {
                        "event": "run_paused",
                        "run_id": str(run_id),
                        "pause_reason": "user_paused",
                        "step_index": idx,
                        "output_hash": event_output_hash,
                    }
                    return

            safely_observe(
                self.observer,
                "record_run",
                run_id=run_id,
                organization_id=organization_id,
                status="completed",
            )
            yield {"event": "run_complete", "run_id": str(run_id), "context": context}

        except Exception:
            logger.error(
                "Research workflow failed",
                extra={"run_id": str(run_id), "error_category": "workflow_error"},
            )
            safely_observe(
                self.observer,
                "record_run",
                run_id=run_id,
                organization_id=organization_id,
                status="failed",
            )
            yield {
                "event": "run_failed",
                "run_id": str(run_id),
                "error": "Research workflow failed",
                "error_category": "workflow_error",
            }

    def _record_stage_outcomes(
        self,
        *,
        run_id: UUID,
        organization_id: UUID | None,
        stage_type: str,
        output: dict[str, Any],
    ) -> None:
        if stage_type == "search":
            coverage = output.get("coverage")
            if isinstance(coverage, dict):
                providers = coverage.get("providers")
                if isinstance(providers, dict):
                    for provider, result in sorted(providers.items()):
                        if not isinstance(result, dict):
                            continue
                        safely_observe(
                            self.observer,
                            "record_provider_outcome",
                            run_id=run_id,
                            organization_id=organization_id,
                            provider=str(provider),
                            outcome=str(result.get("status") or "unknown"),
                            returned_count=result.get("returned", 0),
                        )
                deduplication = coverage.get("deduplication")
                if isinstance(deduplication, dict):
                    safely_observe(
                        self.observer,
                        "record_deduplication",
                        run_id=run_id,
                        organization_id=organization_id,
                        before_count=deduplication.get("before", 0),
                        after_count=deduplication.get("after", 0),
                    )
        elif stage_type == "verify":
            verification = output.get("verification")
            outcome = (
                "passed"
                if isinstance(verification, dict) and verification.get("passed") is True
                else "failed"
            )
            safely_observe(
                self.observer,
                "record_verification",
                run_id=run_id,
                organization_id=organization_id,
                outcome=outcome,
            )
        elif stage_type == "export":
            safely_observe(
                self.observer,
                "record_export",
                run_id=run_id,
                organization_id=organization_id,
                format=str(output.get("format") or "unknown"),
                outcome="success",
                error_kind="none",
            )
