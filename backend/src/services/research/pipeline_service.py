"""Pipeline service for managing research project wizard workflow state."""

from typing import Any, Dict, List, Optional
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_pipeline import ResearchPipeline

logger = structlog.get_logger()

TOTAL_STEPS = 5


class PipelineService:
    """Service for research pipeline CRUD and state transitions."""

    @staticmethod
    async def get_or_create(
        db: AsyncSession, project_id: UUID, *, create_if_missing: bool = True
    ) -> ResearchPipeline:
        """Get existing pipeline for a project, or create one."""
        result = await db.execute(
            select(ResearchPipeline).where(
                ResearchPipeline.project_id == project_id,
                ResearchPipeline.is_deleted == False,  # noqa: E712
            )
        )
        pipeline = result.scalar_one_or_none()

        if pipeline is None:
            if not create_if_missing:
                raise ValueError("No pipeline found for project")
            pipeline = ResearchPipeline(
                project_id=project_id,
                current_step=0,
                completed_steps=[],
                skipped_steps=[],
                step_data={},
                invalidated_steps=[],
            )
            db.add(pipeline)
            await db.flush()
            logger.info(
                "pipeline.created",
                project_id=str(project_id),
                pipeline_id=str(pipeline.id),
            )

        return pipeline

    @staticmethod
    async def update_pipeline(
        db: AsyncSession,
        project_id: UUID,
        current_step: Optional[int] = None,
        completed_steps: Optional[List[int]] = None,
        skipped_steps: Optional[List[int]] = None,
        step_data: Optional[Dict[str, Any]] = None,
        invalidated_steps: Optional[List[int]] = None,
    ) -> ResearchPipeline:
        """Update pipeline state. Auto-invalidates downstream steps when
        navigating back to a completed step."""
        result = await db.execute(
            select(ResearchPipeline).where(
                ResearchPipeline.project_id == project_id,
                ResearchPipeline.is_deleted == False,  # noqa: E712
            )
        )
        pipeline = result.scalar_one_or_none()

        if pipeline is None:
            raise ValueError(f"No pipeline found for project {project_id}")

        if current_step is not None:
            # Auto-invalidation: when going back to a completed step,
            # invalidate all downstream completed steps
            if current_step < pipeline.current_step:
                existing_completed = list(pipeline.completed_steps or [])
                downstream = [s for s in existing_completed if s > current_step]
                if downstream:
                    existing_invalidated = list(pipeline.invalidated_steps or [])
                    merged = list(set(existing_invalidated + downstream))
                    pipeline.invalidated_steps = merged
                    logger.info(
                        "pipeline.auto_invalidated",
                        project_id=str(project_id),
                        invalidated=downstream,
                    )
            pipeline.current_step = current_step

        if completed_steps is not None:
            pipeline.completed_steps = completed_steps
        if skipped_steps is not None:
            pipeline.skipped_steps = skipped_steps
        if step_data is not None:
            existing = dict(pipeline.step_data or {})
            existing.update(step_data)
            pipeline.step_data = existing
        if invalidated_steps is not None:
            pipeline.invalidated_steps = invalidated_steps

        await db.flush()
        return pipeline

    @staticmethod
    async def reset_pipeline(db: AsyncSession, project_id: UUID) -> ResearchPipeline:
        """Reset pipeline to initial state."""
        result = await db.execute(
            select(ResearchPipeline).where(
                ResearchPipeline.project_id == project_id,
                ResearchPipeline.is_deleted == False,  # noqa: E712
            )
        )
        pipeline = result.scalar_one_or_none()

        if pipeline is None:
            raise ValueError(f"No pipeline found for project {project_id}")

        pipeline.current_step = 0
        pipeline.completed_steps = []
        pipeline.skipped_steps = []
        pipeline.step_data = {}
        pipeline.invalidated_steps = []

        await db.flush()
        logger.info("pipeline.reset", project_id=str(project_id))
        return pipeline
