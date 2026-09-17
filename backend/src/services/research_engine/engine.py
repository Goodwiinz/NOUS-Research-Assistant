"""Workflow engine that orchestrates research blueprint execution."""

import asyncio
import time
from typing import Any, AsyncGenerator, Callable, Dict
from uuid import UUID

from src.schemas.research_engine import validate_blueprint_runtime
from src.services.research_engine.step_executor import StepExecutor

MAX_RUN_TOKENS = 50_000
MAX_RUN_WALL_TIME_SECONDS = 15 * 60


class WorkflowEngine:
    """Runs a research blueprint, yielding events for each step."""

    def __init__(
        self,
        step_executor: StepExecutor,
        pause_on_quality_failure: bool = True,
        max_total_tokens: int = MAX_RUN_TOKENS,
        max_wall_time_seconds: float = MAX_RUN_WALL_TIME_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.step_executor = step_executor
        self.pause_on_quality_failure = pause_on_quality_failure
        self.max_total_tokens = max_total_tokens
        self.max_wall_time_seconds = max_wall_time_seconds
        self.clock = clock

    async def run(
        self,
        blueprint: Dict,
        run_id: UUID,
        start_from_step: int = 0,
        initial_context: Dict[str, Any] | None = None,
        initial_total_tokens: int = 0,
        started_at: float | None = None,
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
            yield {
                "event": "run_failed",
                "run_id": str(run_id),
                "error": "Blueprint exceeds a server-owned execution limit",
            }
            return

        started_at = self.clock() if started_at is None else started_at
        total_tokens = max(0, int(initial_total_tokens or 0))

        yield {"event": "run_start", "run_id": str(run_id), "total_steps": len(steps)}

        try:
            for idx, step_def in enumerate(
                steps[start_from_step:], start=start_from_step
            ):
                if total_tokens >= self.max_total_tokens:
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run token budget exhausted",
                    }
                    return
                if self.clock() - started_at >= self.max_wall_time_seconds:
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
                        yield {
                            "event": "run_failed",
                            "run_id": str(run_id),
                            "error": "Run wall-time budget exhausted",
                        }
                        return
                    result = await asyncio.wait_for(
                        self.step_executor.execute(step_def, context),
                        timeout=remaining_wall_time,
                    )
                except asyncio.TimeoutError:
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run wall-time budget exhausted",
                    }
                    return
                except Exception as exc:
                    yield {
                        "event": "step_error",
                        "run_id": str(run_id),
                        "step_index": idx,
                        "step_id": step_id,
                        "error": str(exc),
                    }
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": f"Step {step_id} failed: {exc}",
                    }
                    return

                # Merge step output into context for subsequent steps
                context.update(result.output)
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
                    "quality_marks": quality_marks_data,
                    "token_count": result.token_count,
                }

                if total_tokens > self.max_total_tokens:
                    yield {
                        "event": "run_failed",
                        "run_id": str(run_id),
                        "error": "Run token budget exhausted",
                    }
                    return

                # Check for quality failures
                has_failure = any(not qm.passed for qm in result.quality_marks)
                if has_failure and self.pause_on_quality_failure:
                    yield {
                        "event": "run_paused",
                        "run_id": str(run_id),
                        "step_index": idx,
                        "step_id": step_id,
                        "reason": "Quality check failed",
                        "quality_marks": quality_marks_data,
                    }
                    return

            yield {"event": "run_complete", "run_id": str(run_id), "context": context}

        except Exception as exc:
            yield {
                "event": "run_failed",
                "run_id": str(run_id),
                "error": str(exc)[:256],
            }
