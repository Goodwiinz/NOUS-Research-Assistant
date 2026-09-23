"""
ArXiv LLM Bulk Ingestion API

API endpoints for bulk ingesting papers from Kaggle ArXiv dataset
with full LLM-powered entity extraction, relationship extraction, and embeddings.

WARNING: LLM extraction is expensive! 500k papers = ~$10,000-30,000 in API costs.
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.dependencies import require_platform_operator
from src.services.ingestion.kaggle_llm_bulk_ingestion import (
    KaggleLLMBulkIngestionService,
    LLMIngestionProgress,
)

logger = logging.getLogger(__name__)
# Admin-only: process-global LLM ingestion singleton, resource- and
# cost-intensive (paid LLM extraction). Was reachable unauthenticated —
# MultiTenancyMiddleware lets tokenless requests through, and no route here
# declared an auth dependency.
router = APIRouter(
    prefix="/arxiv/llm-bulk",
    tags=["ArXiv LLM Bulk Ingestion"],
    dependencies=[Depends(require_platform_operator)],
)

# Global progress tracker
_current_ingestion: Optional[LLMIngestionProgress] = None
_ingestion_task: Optional[asyncio.Task] = None


class LLMBulkIngestionRequest(BaseModel):
    """Request model for LLM bulk ingestion"""

    max_papers: int = Field(
        default=1000,
        ge=10,
        le=500000,
        description="Maximum papers to ingest. WARNING: 500k papers costs ~$10,000-30,000",
    )
    batch_size: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Batch size for processing (smaller = more control, larger = faster)",
    )
    categories: Optional[List[str]] = Field(
        default=None,
        description="Filter by ArXiv categories (e.g., ['cs.AI', 'cs.LG'])",
    )
    resume: bool = Field(
        default=True, description="Resume from previous ingestion state"
    )
    enable_embeddings: bool = Field(
        default=True, description="Generate and store vector embeddings in Qdrant"
    )
    enable_entity_extraction: bool = Field(
        default=True, description="Extract entities via Azure OpenAI LLM"
    )
    enable_relationship_extraction: bool = Field(
        default=True, description="Extract relationships via Azure OpenAI LLM"
    )


class LLMBulkIngestionStatus(BaseModel):
    """Response model for LLM ingestion status"""

    is_running: bool
    progress: Optional[dict] = None
    message: str
    estimated_cost_usd: Optional[float] = None


class LLMBulkIngestionResult(BaseModel):
    """Response model for LLM ingestion result"""

    status: str
    total_processed: int
    total_ingested: int
    total_failed: int
    total_skipped: int
    entities_extracted: int
    relationships_extracted: int
    embeddings_generated: int
    llm_calls: int
    estimated_cost_usd: float
    elapsed_seconds: float
    papers_per_second: float


async def _run_llm_ingestion_task(
    max_papers: int,
    batch_size: int,
    categories: Optional[List[str]],
    resume: bool,
    enable_embeddings: bool,
    enable_entity_extraction: bool,
    enable_relationship_extraction: bool,
):
    """Background task for running LLM ingestion"""
    global _current_ingestion

    service = KaggleLLMBulkIngestionService(
        batch_size=batch_size,
        max_papers=max_papers,
        enable_embeddings=enable_embeddings,
        enable_entity_extraction=enable_entity_extraction,
        enable_relationship_extraction=enable_relationship_extraction,
    )

    def progress_callback(progress: LLMIngestionProgress):
        global _current_ingestion
        _current_ingestion = progress

    try:
        result = await service.run_ingestion(
            categories=categories, resume=resume, progress_callback=progress_callback
        )
        logger.info(f"LLM bulk ingestion completed: {result}")
    except Exception as e:
        logger.error(f"LLM bulk ingestion failed: {e}")
        raise


@router.post("/start", response_model=LLMBulkIngestionStatus)
async def start_llm_bulk_ingestion(
    request: LLMBulkIngestionRequest, background_tasks: BackgroundTasks
):
    """
    Start LLM-powered bulk ingestion from Kaggle ArXiv dataset.

    This includes:
    - LLM Entity Extraction (methodologies, models, metrics, tasks, datasets)
    - LLM Relationship Extraction (USES, EVALUATED_ON, ACHIEVED, etc.)
    - Vector Embeddings stored in Qdrant

    **COST WARNING**: LLM extraction is expensive!
    - Entity extraction: ~$0.02 per paper
    - Relationship extraction: ~$0.02 per paper
    - Embeddings: ~$0.0001 per paper
    - **500k papers = $10,000-30,000 in API costs**

    Monitor progress via the /status endpoint.
    """
    global _ingestion_task, _current_ingestion

    # Calculate estimated cost
    estimated_cost = 0.0
    if request.enable_entity_extraction:
        estimated_cost += request.max_papers * 0.02
    if request.enable_relationship_extraction:
        estimated_cost += request.max_papers * 0.02
    if request.enable_embeddings:
        estimated_cost += request.max_papers * 0.0001

    # Check if already running
    if _ingestion_task and not _ingestion_task.done():
        return LLMBulkIngestionStatus(
            is_running=True,
            progress=_current_ingestion,
            message="LLM ingestion already in progress",
            estimated_cost_usd=estimated_cost,
        )

    # Reset progress
    _current_ingestion = None

    # Start background task
    _ingestion_task = asyncio.create_task(
        _run_llm_ingestion_task(
            max_papers=request.max_papers,
            batch_size=request.batch_size,
            categories=request.categories,
            resume=request.resume,
            enable_embeddings=request.enable_embeddings,
            enable_entity_extraction=request.enable_entity_extraction,
            enable_relationship_extraction=request.enable_relationship_extraction,
        )
    )

    features = []
    if request.enable_entity_extraction:
        features.append("entities")
    if request.enable_relationship_extraction:
        features.append("relationships")
    if request.enable_embeddings:
        features.append("embeddings")

    return LLMBulkIngestionStatus(
        is_running=True,
        progress=None,
        message=f"Started LLM bulk ingestion for {request.max_papers:,} papers with {', '.join(features)}",
        estimated_cost_usd=estimated_cost,
    )


@router.get("/status", response_model=LLMBulkIngestionStatus)
async def get_llm_ingestion_status():
    """
    Get current LLM ingestion status and progress.

    Returns detailed progress including:
    - Papers processed/ingested/failed/skipped
    - Entities and relationships extracted
    - Embeddings generated
    - Estimated cost so far
    - ETA for completion
    """
    global _ingestion_task, _current_ingestion

    is_running = _ingestion_task and not _ingestion_task.done()

    if not is_running and not _current_ingestion:
        return LLMBulkIngestionStatus(
            is_running=False,
            progress=None,
            message="No LLM ingestion running or completed",
        )

    estimated_cost = (
        _current_ingestion.get("llm_cost_estimate", 0.0) if _current_ingestion else 0.0
    )

    return LLMBulkIngestionStatus(
        is_running=is_running,
        progress=_current_ingestion,
        message=(
            "LLM ingestion in progress" if is_running else "LLM ingestion completed"
        ),
        estimated_cost_usd=estimated_cost,
    )


@router.post("/stop")
async def stop_llm_ingestion():
    """
    Stop the current LLM ingestion process.

    The ingestion can be resumed later with resume=True.
    """
    global _ingestion_task

    if _ingestion_task and not _ingestion_task.done():
        _ingestion_task.cancel()
        return {
            "status": "stopped",
            "message": "LLM ingestion cancelled. Use resume=True to continue later.",
        }

    return {"status": "not_running", "message": "No LLM ingestion to stop"}


@router.get("/cost-estimate")
async def estimate_ingestion_cost(
    max_papers: int = 1000,
    enable_entity_extraction: bool = True,
    enable_relationship_extraction: bool = True,
    enable_embeddings: bool = True,
):
    """
    Estimate the cost for LLM bulk ingestion.

    Helps you understand the API costs before starting ingestion.
    """
    costs = {
        "entity_extraction_per_paper": 0.02,
        "relationship_extraction_per_paper": 0.02,
        "embedding_per_paper": 0.0001,
    }

    total_cost = 0.0
    breakdown = {}

    if enable_entity_extraction:
        cost = max_papers * costs["entity_extraction_per_paper"]
        breakdown["entity_extraction"] = cost
        total_cost += cost

    if enable_relationship_extraction:
        cost = max_papers * costs["relationship_extraction_per_paper"]
        breakdown["relationship_extraction"] = cost
        total_cost += cost

    if enable_embeddings:
        cost = max_papers * costs["embedding_per_paper"]
        breakdown["embeddings"] = cost
        total_cost += cost

    # Estimate processing time
    papers_per_second = 0.5 if enable_entity_extraction else 5.0
    estimated_hours = max_papers / (papers_per_second * 3600)

    return {
        "max_papers": max_papers,
        "cost_breakdown": breakdown,
        "total_estimated_cost_usd": round(total_cost, 2),
        "estimated_processing_hours": round(estimated_hours, 1),
        "features_enabled": {
            "entity_extraction": enable_entity_extraction,
            "relationship_extraction": enable_relationship_extraction,
            "embeddings": enable_embeddings,
        },
        "warning": "These are estimates. Actual costs may vary based on paper length and API pricing.",
    }


@router.post("/test-small-batch")
async def test_llm_small_batch():
    """
    Test LLM ingestion with a small batch of 50 papers.

    Useful for verifying:
    - Azure OpenAI connectivity
    - LLM extraction quality
    - Qdrant embedding storage
    - Neo4j entity/relationship storage

    Estimated cost: ~$2-4
    """
    service = KaggleLLMBulkIngestionService(
        batch_size=25,
        max_papers=50,
        enable_embeddings=True,
        enable_entity_extraction=True,
        enable_relationship_extraction=True,
    )

    try:
        result = await service.run_ingestion(
            categories=["cs.AI", "cs.LG"], resume=False  # Focus on AI/ML papers
        )
        return {
            "status": "success",
            "result": result,
            "message": "LLM test batch completed successfully",
        }
    except Exception as e:
        logger.error(f"LLM test batch failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats")
async def get_llm_ingestion_stats():
    """
    Get statistics about LLM-ingested papers in Neo4j.

    Shows:
    - Document count (with extraction_type='llm')
    - Entity counts by type
    - Relationship counts by type
    - Top categories
    """
    import os

    from neo4j import AsyncGraphDatabase

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    try:
        driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

        async with driver.session() as session:
            # Get LLM-processed document count
            result = await session.run(
                "MATCH (d:DOCUMENT) WHERE d.extraction_type = 'llm' RETURN count(d) as count"
            )
            record = await result.single()
            llm_doc_count = record["count"] if record else 0

            # Get total document count
            result = await session.run("MATCH (d:DOCUMENT) RETURN count(d) as count")
            record = await result.single()
            total_doc_count = record["count"] if record else 0

            # Get entity counts by type
            result = await session.run("""
                MATCH (e:Entity)
                RETURN labels(e) as types, count(*) as count
                ORDER BY count DESC
            """)
            entity_types = [
                {"types": r["types"], "count": r["count"]} async for r in result
            ]

            # Get relationship counts by type
            result = await session.run("""
                MATCH ()-[r]->()
                WHERE type(r) IN ['USES', 'EVALUATED_ON', 'ACHIEVED', 'COMPARED_WITH', 'EXTENDS', 'MENTIONS']
                RETURN type(r) as rel_type, count(*) as count
                ORDER BY count DESC
            """)
            relationship_types = [
                {"type": r["rel_type"], "count": r["count"]} async for r in result
            ]

            # Get category distribution for LLM-processed papers
            result = await session.run("""
                MATCH (d:DOCUMENT)
                WHERE d.extraction_type = 'llm'
                RETURN d.arxiv_category as category, count(*) as count
                ORDER BY count DESC
                LIMIT 20
            """)
            categories = [
                {"category": r["category"], "count": r["count"]} async for r in result
            ]

        await driver.close()

        return {
            "total_documents": total_doc_count,
            "llm_processed_documents": llm_doc_count,
            "metadata_only_documents": total_doc_count - llm_doc_count,
            "entity_types": entity_types,
            "relationship_types": relationship_types,
            "top_categories": categories,
        }

    except Exception as e:
        logger.error(f"Failed to get LLM stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
