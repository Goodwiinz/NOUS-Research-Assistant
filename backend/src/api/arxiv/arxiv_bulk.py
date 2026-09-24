"""
ArXiv Bulk Ingestion API

API endpoints for bulk ingesting papers from Kaggle ArXiv dataset.
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.dependencies import require_platform_operator
from src.services.ingestion.kaggle_bulk_ingestion import (
    IngestionProgress,
    KaggleBulkIngestionService,
)

logger = logging.getLogger(__name__)
# Admin-only: these endpoints drive a process-global Kaggle ingestion singleton
# (no per-tenant context) and are resource-intensive. They had NO auth at all —
# MultiTenancyMiddleware passes tokenless requests through, so every route here
# was reachable unauthenticated until this router-level guard.
router = APIRouter(
    prefix="/arxiv/bulk",
    tags=["ArXiv Bulk Ingestion"],
    dependencies=[Depends(require_platform_operator)],
)

# Global progress tracker
_current_ingestion: Optional[IngestionProgress] = None
_ingestion_task: Optional[asyncio.Task] = None


class BulkIngestionRequest(BaseModel):
    """Request model for bulk ingestion"""

    max_papers: int = Field(
        default=500000, ge=100, le=2000000, description="Maximum papers to ingest"
    )
    batch_size: int = Field(
        default=1000, ge=100, le=10000, description="Batch size for processing"
    )
    categories: Optional[List[str]] = Field(
        default=None, description="Filter by ArXiv categories"
    )
    resume: bool = Field(default=True, description="Resume from previous state")


class BulkIngestionStatus(BaseModel):
    """Response model for ingestion status"""

    is_running: bool
    progress: Optional[dict] = None
    message: str


class BulkIngestionResult(BaseModel):
    """Response model for ingestion result"""

    status: str
    total_processed: int
    total_ingested: int
    total_failed: int
    total_skipped: int
    elapsed_seconds: float
    papers_per_second: float


async def _run_ingestion_task(
    max_papers: int, batch_size: int, categories: Optional[List[str]], resume: bool
):
    """Background task for running ingestion"""
    global _current_ingestion

    service = KaggleBulkIngestionService(batch_size=batch_size, max_papers=max_papers)

    def progress_callback(progress: dict):
        global _current_ingestion
        _current_ingestion = progress

    try:
        result = await service.run_ingestion(
            categories=categories, resume=resume, progress_callback=progress_callback
        )
        logger.info(f"Bulk ingestion completed: {result}")
    except Exception as e:
        logger.error(f"Bulk ingestion failed: {e}")
        raise


@router.post("/start", response_model=BulkIngestionStatus)
async def start_bulk_ingestion(
    request: BulkIngestionRequest, background_tasks: BackgroundTasks
):
    """
    Start bulk ingestion from Kaggle ArXiv dataset.

    This runs in the background and can be monitored via the /status endpoint.
    """
    global _ingestion_task, _current_ingestion

    # Check if already running
    if _ingestion_task and not _ingestion_task.done():
        return BulkIngestionStatus(
            is_running=True,
            progress=_current_ingestion,
            message="Ingestion already in progress",
        )

    # Reset progress
    _current_ingestion = None

    # Start background task
    _ingestion_task = asyncio.create_task(
        _run_ingestion_task(
            max_papers=request.max_papers,
            batch_size=request.batch_size,
            categories=request.categories,
            resume=request.resume,
        )
    )

    return BulkIngestionStatus(
        is_running=True,
        progress=None,
        message=f"Started bulk ingestion for {request.max_papers:,} papers",
    )


@router.get("/status", response_model=BulkIngestionStatus)
async def get_ingestion_status():
    """
    Get current ingestion status and progress.
    """
    global _ingestion_task, _current_ingestion

    is_running = _ingestion_task and not _ingestion_task.done()

    if not is_running and not _current_ingestion:
        return BulkIngestionStatus(
            is_running=False, progress=None, message="No ingestion running or completed"
        )

    return BulkIngestionStatus(
        is_running=is_running,
        progress=_current_ingestion,
        message="Ingestion in progress" if is_running else "Ingestion completed",
    )


@router.post("/stop")
async def stop_ingestion():
    """
    Stop the current ingestion process.
    """
    global _ingestion_task

    if _ingestion_task and not _ingestion_task.done():
        _ingestion_task.cancel()
        return {"status": "stopped", "message": "Ingestion cancelled"}

    return {"status": "not_running", "message": "No ingestion to stop"}


@router.get("/stats")
async def get_ingestion_stats():
    """
    Get statistics about ingested papers in Neo4j.
    """
    import os

    from neo4j import AsyncGraphDatabase

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    try:
        driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

        async with driver.session() as session:
            # Get document count
            result = await session.run("MATCH (d:DOCUMENT) RETURN count(d) as count")
            record = await result.single()
            doc_count = record["count"] if record else 0

            # Get entity count
            result = await session.run("MATCH (e:Entity) RETURN count(e) as count")
            record = await result.single()
            entity_count = record["count"] if record else 0

            # Get category distribution
            result = await session.run("""
                MATCH (d:DOCUMENT)
                RETURN d.arxiv_category as category, count(*) as count
                ORDER BY count DESC
                LIMIT 20
            """)
            categories = [
                {"category": r["category"], "count": r["count"]} async for r in result
            ]

            # Get author count
            result = await session.run(
                "MATCH (a:Entity:PERSON) RETURN count(a) as count"
            )
            record = await result.single()
            author_count = record["count"] if record else 0

        await driver.close()

        return {
            "total_documents": doc_count,
            "total_entities": entity_count,
            "total_authors": author_count,
            "top_categories": categories,
        }

    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/test-small-batch")
async def test_small_batch():
    """
    Test ingestion with a small batch of 100 papers.
    Useful for verifying setup before large-scale ingestion.
    """
    service = KaggleBulkIngestionService(batch_size=50, max_papers=100)

    try:
        result = await service.run_ingestion(
            categories=["cs.AI", "cs.LG"], resume=False  # Focus on AI/ML papers
        )
        return {"status": "success", "result": result}
    except Exception as e:
        logger.error(f"Test batch failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
