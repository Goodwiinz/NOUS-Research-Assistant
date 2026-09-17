"""
ArXiv API Endpoints for RAG System

Provides REST API endpoints for:
- Searching arXiv papers
- Ingesting arXiv papers into the system
- Creating evaluation datasets
- Managing arXiv-specific features
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.config import get_settings
from src.core.dependencies import get_current_user
from src.services.arxiv.arxiv_service import ArXivIngestionService
from src.services.arxiv.persistence import persist_arxiv_documents
from src.services.expensive_work_admission import admit_expensive_work
from src.shared.schemas import UserResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/arxiv", tags=["arxiv"])
settings = get_settings()

MAX_ARXIV_INGEST_PAPERS = 50
MAX_ARXIV_DATASET_WORK = 500
ARXIV_BACKGROUND_DEADLINE_SECONDS = 5 * 60
_ARXIV_ID_PATTERN = re.compile(
    r"^(?:arxiv:)?(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z][a-z0-9-]*(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)$",
    re.IGNORECASE,
)


# Pydantic models
class ArXivSearchRequest(BaseModel):
    query: str = Field(..., description="Search query for arXiv papers")
    max_results: int = Field(
        100, ge=1, le=1000, description="Maximum number of results"
    )
    date_from: Optional[datetime] = Field(None, description="Start date for filtering")
    date_to: Optional[datetime] = Field(None, description="End date for filtering")
    categories: Optional[List[str]] = Field(None, description="Categories to filter by")
    sort_by: str = Field("submittedDate", description="Sort field")
    sort_order: str = Field("descending", description="Sort order")


class ArXivIngestRequest(BaseModel):
    paper_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_ARXIV_INGEST_PAPERS,
        description="List of arXiv paper IDs to ingest",
    )
    download_pdfs: bool = Field(True, description="Whether to download PDFs")
    extract_content: bool = Field(
        True, description="Whether to extract full text content"
    )
    batch_size: int = Field(10, ge=1, le=50, description="Batch size for processing")

    @field_validator("paper_ids")
    @classmethod
    def validate_paper_ids(cls, value: List[str]) -> List[str]:
        normalized = [paper_id.strip() for paper_id in value]
        if any(not _ARXIV_ID_PATTERN.fullmatch(paper_id) for paper_id in normalized):
            raise ValueError("paper_ids contains an invalid arXiv identifier")
        return normalized


class ArXivPaperResponse(BaseModel):
    id: str
    title: str
    authors: List[str]
    abstract: str
    published: datetime
    updated: datetime
    categories: List[str]
    primary_category: Optional[str]
    comment: Optional[str]
    journal_ref: Optional[str]
    pdf_url: Optional[str]
    doi: Optional[str]


class EvaluationDatasetRequest(BaseModel):
    query: str = Field(
        "machine learning",
        max_length=1000,
        description="Query to find papers for dataset",
    )
    num_papers: int = Field(50, ge=1, le=200, description="Number of papers to include")
    questions_per_paper: int = Field(5, ge=1, le=10, description="Questions per paper")
    difficulty_levels: List[str] = Field(
        ["easy", "medium", "hard"],
        min_length=1,
        max_length=3,
        description="Difficulty levels",
    )

    @model_validator(mode="after")
    def validate_aggregate_work(self):
        if self.num_papers * self.questions_per_paper > MAX_ARXIV_DATASET_WORK:
            raise ValueError(
                f"dataset work exceeds the {MAX_ARXIV_DATASET_WORK}-question limit"
            )
        return self


def _get_verified_organization_id(current_user: Any) -> Any:
    """Return only the authenticated user's server-side organization ID."""
    organization_id = getattr(current_user, "organization_id", None)
    if not organization_id:
        organization = getattr(current_user, "organization", None)
        organization_id = getattr(organization, "id", None)
    if not organization_id:
        raise HTTPException(
            status_code=403,
            detail="No organization associated with this account",
        )
    return organization_id


def _background_deadline() -> float:
    return time.monotonic() + ARXIV_BACKGROUND_DEADLINE_SECONDS


# Dependency injection
async def get_arxiv_service() -> ArXivIngestionService:
    """Get arXiv service instance"""
    config = {
        "arxiv_download_dir": settings.get("arxiv_download_dir", "data/arxiv"),
        "max_concurrent_downloads": settings.get("arxiv_max_downloads", 10),
    }
    return ArXivIngestionService(config)


# Endpoints
@router.post("/search", response_model=List[ArXivPaperResponse])
async def search_arxiv_papers(
    request: ArXivSearchRequest,
):
    """
    Search for papers on arXiv

    This endpoint allows searching the arXiv database for papers
    matching specific criteria.
    """
    try:
        async with ArXivIngestionService() as arxiv_service:
            papers = await arxiv_service.search_papers(
                query=request.query,
                max_results=request.max_results,
                date_from=request.date_from,
                date_to=request.date_to,
                categories=request.categories,
                sort_by=request.sort_by,
                sort_order=request.sort_order,
            )

            # Convert to response model
            response = []
            for paper in papers:
                response.append(
                    ArXivPaperResponse(
                        id=paper["id"],
                        title=paper["title"],
                        authors=paper["authors"],
                        abstract=paper["abstract"],
                        published=datetime.fromisoformat(
                            paper["published"].replace("Z", "+00:00")
                        ),
                        updated=datetime.fromisoformat(
                            paper["updated"].replace("Z", "+00:00")
                        ),
                        categories=paper["categories"],
                        primary_category=paper.get("primary_category"),
                        comment=paper.get("comment"),
                        journal_ref=paper.get("journal_ref"),
                        pdf_url=paper["links"].get("pdf"),
                        doi=paper["links"].get("doi"),
                    )
                )

            return response

    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/ingest", response_model=Dict[str, Any])
async def ingest_arxiv_papers(
    request: ArXivIngestRequest,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Ingest arXiv papers into the RAG system

    Starts a background task to download and process the specified papers.
    """
    try:
        organization_id = _get_verified_organization_id(current_user)
        if not await admit_expensive_work(
            user_id=current_user.id,
            organization_id=organization_id,
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many expensive research jobs; retry later",
            )

        # Add to background tasks
        background_tasks.add_task(
            _process_arxiv_ingestion,
            paper_ids=request.paper_ids,
            user_id=current_user.id,
            organization_id=organization_id,
            download_pdfs=request.download_pdfs,
            extract_content=request.extract_content,
            batch_size=request.batch_size,
            deadline=_background_deadline(),
        )

        return {
            "message": "ArXiv paper ingestion started",
            "paper_count": len(request.paper_ids),
            "status": "processing",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/create-dataset", response_model=Dict[str, Any])
async def create_evaluation_dataset(
    request: EvaluationDatasetRequest,
    background_tasks: BackgroundTasks,
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Create an evaluation dataset from arXiv papers

    Generates questions and evaluation data for testing the RAG system.
    """
    try:
        organization_id = _get_verified_organization_id(current_user)
        if not await admit_expensive_work(
            user_id=current_user.id,
            organization_id=organization_id,
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many expensive research jobs; retry later",
            )

        # Start dataset creation in background
        task_id = f"arxiv_dataset_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        background_tasks.add_task(
            _create_arxiv_dataset,
            task_id=task_id,
            query=request.query,
            num_papers=request.num_papers,
            questions_per_paper=request.questions_per_paper,
            difficulty_levels=request.difficulty_levels,
            user_id=current_user.id,
            organization_id=organization_id,
            deadline=_background_deadline(),
        )

        return {
            "message": "Evaluation dataset creation started",
            "task_id": task_id,
            "status": "processing",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/categories", response_model=Dict[str, Any])
async def get_arxiv_categories(current_user: UserResponse = Depends(get_current_user)):
    """
    Get available arXiv categories and their descriptions
    """
    categories = {
        "computer_science": {
            "description": "Computer Science",
            "subcategories": {
                "cs.AI": "Artificial Intelligence",
                "cs.CL": "Computation and Language",
                "cs.CV": "Computer Vision and Pattern Recognition",
                "cs.LG": "Machine Learning",
                "cs.NE": "Neural and Evolutionary Computing",
                "cs.CR": "Cryptography and Security",
                "cs.DB": "Databases",
                "cs.IR": "Information Retrieval",
                "cs.MM": "Multimedia",
                "cs.NI": "Networking and Internet Architecture",
                "cs.RO": "Robotics",
                "cs.SY": "Systems and Control",
            },
        },
        "mathematics": {
            "description": "Mathematics",
            "subcategories": {
                "math.AG": "Algebraic Geometry",
                "math.AT": "Algebraic Topology",
                "math.CA": "Classical Analysis and ODEs",
                "math.CO": "Combinatorics",
                "math.NA": "Numerical Analysis",
                "math.PR": "Probability",
                "math.ST": "Statistics Theory",
            },
        },
        "physics": {
            "description": "Physics",
            "subcategories": {
                "astro-ph": "Astrophysics",
                "cond-mat": "Condensed Matter",
                "quant-ph": "Quantum Physics",
                "physics.app-ph": "Applied Physics",
                "physics.comp-ph": "Computational Physics",
            },
        },
        "quantitative_biology": {
            "description": "Quantitative Biology",
            "subcategories": {
                "q-bio.BM": "Biomolecules",
                "q-bio.CB": "Cell Behavior",
                "q-bio.GN": "Genomics",
                "q-bio.NC": "Neurons and Cognition",
                "q-bio.QM": "Quantitative Methods",
                "q-bio.TO": "Tissues and Organs",
            },
        },
        "statistics": {
            "description": "Statistics",
            "subcategories": {
                "stat.ML": "Machine Learning",
                "stat.ME": "Methodology",
                "stat.TH": "Statistics Theory",
            },
        },
    }

    return {
        "categories": categories,
        "total_categories": sum(
            len(cat["subcategories"]) for cat in categories.values()
        ),
    }


@router.get("/download/{paper_id}")
async def download_paper(
    paper_id: str, current_user: UserResponse = Depends(get_current_user)
):
    """
    Download PDF for a specific arXiv paper
    """
    try:
        async with ArXivIngestionService() as arxiv_service:
            pdf_content = await arxiv_service.download_paper_pdf(paper_id)

            if pdf_content:
                # Return PDF file
                return JSONResponse(
                    content={
                        "message": "PDF downloaded successfully",
                        "paper_id": paper_id,
                        "size_bytes": len(pdf_content),
                    }
                )
            else:
                raise HTTPException(status_code=404, detail="PDF not found")

    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/statistics")
async def get_arxiv_statistics(
    query: Optional[str] = Query(None, description="Query to filter papers"),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get statistics about arXiv papers in the system
    """
    try:
        # Calculate date range
        date_to = datetime.now()
        date_from = date_to - timedelta(days=days)

        # Search for papers in the date range
        search_query = query or "all"
        search_query += f" AND submittedDate:[{date_from.strftime('%Y%m%d')}0000 TO {date_to.strftime('%Y%m%d')}2359]"

        async with ArXivIngestionService() as arxiv_service:
            papers = await arxiv_service.search_papers(
                query=search_query,
                max_results=1000,
                date_from=date_from,
                date_to=date_to,
            )

            # Get category statistics
            stats = arxiv_service.get_category_statistics(papers)

            # Add additional statistics
            stats["date_range"] = {
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "days": days,
            }
            stats["query"] = query

            return stats

    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


# Background task functions
async def _process_arxiv_ingestion(
    paper_ids: List[str],
    user_id: str,
    organization_id: "str | UUID",
    download_pdfs: bool,
    extract_content: bool,
    batch_size: int,
    deadline: Optional[float] = None,
):
    """Background task to process arXiv paper ingestion"""
    try:
        if (
            not paper_ids
            or len(paper_ids) > MAX_ARXIV_INGEST_PAPERS
            or any(
                not _ARXIV_ID_PATTERN.fullmatch(str(paper_id).strip())
                for paper_id in paper_ids
            )
        ):
            logger.warning("Background arXiv ingestion exceeded paper limit")
            return
        if not user_id or not organization_id:
            logger.warning("Background arXiv ingestion missing verified actor scope")
            return
        effective_deadline = (
            deadline if deadline is not None else _background_deadline()
        )
        remaining = effective_deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(
            _process_arxiv_ingestion_work(
                paper_ids=paper_ids,
                user_id=user_id,
                organization_id=organization_id,
                download_pdfs=download_pdfs,
                extract_content=extract_content,
                batch_size=batch_size,
            ),
            timeout=remaining,
        )

    except asyncio.TimeoutError:
        logger.warning("Background arXiv ingestion exceeded its deadline")
    except Exception as e:
        logger.error(f"Background arXiv ingestion failed: {e}")


async def _process_arxiv_ingestion_work(
    *,
    paper_ids: List[str],
    user_id: str,
    organization_id: "str | UUID",
    download_pdfs: bool,
    extract_content: bool,
    batch_size: int,
):
    async with ArXivIngestionService() as arxiv_service:
        # First, get paper metadata
        papers = []
        for paper_id in paper_ids:
            search_results = await arxiv_service.search_papers(
                query=f"id:{paper_id}", max_results=1
            )
            if search_results:
                papers.extend(search_results)

        if papers:
            documents = await arxiv_service.ingest_papers(
                papers=papers,
                download_pdfs=download_pdfs,
                extract_content=extract_content,
                batch_size=batch_size,
            )

            persisted = await persist_arxiv_documents(
                documents,
                user_id=user_id,
                organization_id=organization_id,
            )
            if persisted.document_ids:
                logger.info(
                    "arXiv ingestion persisted/reused %d/%d documents for org %s",
                    len(persisted.document_ids),
                    len(documents),
                    organization_id,
                )
            if persisted.failed_papers:
                logger.warning(
                    "arXiv ingestion skipped papers after durable-storage failures: %s",
                    sorted(persisted.failed_papers),
                )


async def _create_arxiv_dataset(
    task_id: str,
    query: str,
    num_papers: int,
    questions_per_paper: int,
    difficulty_levels: List[str],
    user_id: str,
    organization_id: Optional["str | UUID"] = None,
    deadline: Optional[float] = None,
):
    """Background task to create evaluation dataset"""
    try:
        if (
            num_papers < 1
            or questions_per_paper < 1
            or num_papers * questions_per_paper > MAX_ARXIV_DATASET_WORK
        ):
            logger.warning("Background dataset creation exceeded aggregate work limit")
            return
        if not user_id or not organization_id:
            logger.warning("Background dataset creation missing verified actor scope")
            return
        effective_deadline = (
            deadline if deadline is not None else _background_deadline()
        )
        remaining = effective_deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(
            _create_arxiv_dataset_work(
                task_id=task_id,
                query=query,
                num_papers=num_papers,
                questions_per_paper=questions_per_paper,
                difficulty_levels=difficulty_levels,
            ),
            timeout=remaining,
        )

    except asyncio.TimeoutError:
        logger.warning("Background dataset creation exceeded its deadline")
    except Exception as e:
        logger.error(f"Background dataset creation failed: {e}")


async def _create_arxiv_dataset_work(
    *,
    task_id: str,
    query: str,
    num_papers: int,
    questions_per_paper: int,
    difficulty_levels: List[str],
):
    async with ArXivIngestionService() as arxiv_service:
        papers = await arxiv_service.search_papers(query=query, max_results=num_papers)

        if papers:
            dataset = await arxiv_service.create_evaluation_dataset(
                papers=papers,
                num_questions=questions_per_paper,
                difficulty_levels=difficulty_levels,
            )
            logger.info(
                "Created evaluation dataset %s with %d test cases",
                task_id,
                len(dataset["test_cases"]),
            )
