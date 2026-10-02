"""
ArXiv Change Tracking API endpoints
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.dependencies import get_current_user
from src.models.user import User
from src.services.arxiv.arxiv_change_tracker import change_tracker, track_arxiv_changes
from src.services.arxiv.arxiv_service import IngestionError
from src.services.expensive_work_admission import metered_expensive_work

logger = logging.getLogger(__name__)
router = APIRouter()

ARXIV_TRACKING_RETRY_MESSAGE = (
    "ArXiv is temporarily rate limiting category scans. Retry in about a minute "
    "or scan fewer categories."
)

# Cap categories per scan — each category is a separate, slow arXiv call.
MAX_CATEGORIES_PER_REQUEST = 20


def _resolve_org_id(current_user: User) -> str:
    """Resolve the caller's organization id from the ``User`` ORM object.

    ``get_current_user`` returns a ``User`` (organization eagerly loaded), NOT a
    dict. Never fabricate an org — an unscoped tracker call leaks tenants'
    papers into each other's state.
    """
    org_id = getattr(current_user, "organization_id", None)
    if not org_id:
        organization = getattr(current_user, "organization", None)
        org_id = getattr(organization, "id", None) if organization else None
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="No organization associated with this account",
        )
    return str(org_id)


class CategoryTrackingRequest(BaseModel):
    categories: List[str] = Field(
        ...,
        description="ArXiv categories to track",
        min_length=1,
        max_length=MAX_CATEGORIES_PER_REQUEST,
    )
    days_back: int = Field(
        default=1, ge=1, le=30, description="Number of days to look back"
    )
    update_database: bool = Field(
        default=True, description="Whether to apply changes to database"
    )


class ChangeHistoryResponse(BaseModel):
    paper_id: str
    current_hash: str
    last_seen: str
    deleted: bool
    metadata: Dict[str, Any]


@router.post("/track-categories")
@metered_expensive_work
async def track_category_changes(
    request: CategoryTrackingRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Track changes in specific arXiv categories

    This endpoint searches for recent papers in the specified categories,
    detects changes (new, updated, deleted papers), and optionally updates
    the database and knowledge graph with the changes.
    """
    organization_id = _resolve_org_id(current_user)
    user_id = str(current_user.id)

    if len(request.categories) > MAX_CATEGORIES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many categories: max {MAX_CATEGORIES_PER_REQUEST} per request"
            ),
        )

    try:
        logger.info(f"Tracking changes for categories: {request.categories}")

        result = await change_tracker.track_category_changes(
            categories=request.categories,
            organization_id=organization_id,
            user_id=user_id,
            days_back=request.days_back,
            update_db=request.update_database,
        )

        return {
            "status": "success",
            "timestamp": datetime.utcnow().isoformat(),
            "result": result,
        }

    except IngestionError as e:
        logger.warning(f"ArXiv category scan unavailable: {e}")
        detail = str(e).lower()

        if "rate limit" in detail or "timed out" in detail:
            raise HTTPException(
                status_code=503,
                detail=ARXIV_TRACKING_RETRY_MESSAGE,
                headers={"Retry-After": "60"},
            )

        raise HTTPException(
            status_code=502,
            detail="Unable to complete the arXiv category scan right now. Retry shortly.",
        )
    except Exception as e:
        logger.error(f"Failed to track category changes: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/track-all")
@metered_expensive_work
async def track_all_changes(
    days_back: int = Query(default=1, ge=1, le=30, description="Days to look back"),
    current_user: User = Depends(get_current_user),
):
    """
    Track changes across all popular arXiv categories

    This is a convenience endpoint that tracks changes across a predefined
    list of popular arXiv categories.
    """
    organization_id = _resolve_org_id(current_user)
    user_id = str(current_user.id)

    try:
        logger.info(f"Tracking all arXiv changes for last {days_back} days")

        result = await track_arxiv_changes(
            organization_id=organization_id,
            user_id=user_id,
            days_back=days_back,
        )

        return {
            "status": "success",
            "timestamp": datetime.utcnow().isoformat(),
            "result": result,
        }

    except Exception as e:
        logger.error(f"Failed to track all changes: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def get_change_history(
    paper_id: Optional[str] = Query(
        None, description="Specific paper ID to get history for"
    ),
    limit: int = Query(
        default=100, ge=1, le=500, description="Maximum number of records to return"
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Get change history for arXiv papers

    Returns a history of all tracked changes, optionally filtered by paper ID.
    """
    organization_id = _resolve_org_id(current_user)

    try:
        logger.info(f"Getting change history for paper_id: {paper_id}")

        history = await change_tracker.get_change_history(
            organization_id=organization_id, paper_id=paper_id
        )

        # Apply limit
        if limit and len(history) > limit:
            history = history[:limit]

        return {"status": "success", "total_records": len(history), "history": history}

    except Exception as e:
        logger.error(f"Failed to get change history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats")
async def get_tracking_statistics():
    """
    Get statistics about the change tracking system (public endpoint)

    Returns only aggregate counts summed across all organizations. This endpoint
    is unauthenticated, so it deliberately exposes NO per-tenant detail (no
    titles, categories, or the internal state file path) to avoid leaking one
    tenant's data to anonymous callers.
    """
    try:
        # Aggregate across all org-partitioned state without exposing any
        # per-paper metadata (titles/categories) or internal file paths.
        total_tracked = 0
        deleted_count = 0
        orgs = change_tracker.state.get("__orgs__", {})
        for org_papers in orgs.values():
            for data in org_papers.values():
                total_tracked += 1
                if data.get("deleted"):
                    deleted_count += 1

        active_count = total_tracked - deleted_count

        return {
            "status": "success",
            "timestamp": datetime.utcnow().isoformat(),
            "statistics": {
                "total_papers_tracked": total_tracked,
                "active_papers": active_count,
                "deleted_papers": deleted_count,
            },
        }

    except Exception as e:
        logger.error(f"Failed to get tracking statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cleanup")
async def cleanup_old_state(
    days: int = Body(default=90, embed=True, description="Days to keep state for"),
    current_user: User = Depends(get_current_user),
):
    """
    Clean up old tracking state

    Removes tracking records for papers not seen in the specified number of days.
    """
    organization_id = _resolve_org_id(current_user)

    try:
        logger.info(f"Cleaning up state older than {days} days")

        removed_count = await change_tracker.cleanup_old_state(
            organization_id=organization_id, days=days
        )

        return {
            "status": "success",
            "removed_records": removed_count,
            "message": f"Removed {removed_count} stale state records",
        }

    except Exception as e:
        logger.error(f"Failed to cleanup old state: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/force-sync")
@metered_expensive_work
async def force_sync_paper(
    paper_id: str = Body(..., embed=True, description="arXiv paper ID to sync"),
    current_user: User = Depends(get_current_user),
):
    """
    Force synchronization of a specific paper

    Fetches the latest version of the paper from arXiv and updates the database
    and knowledge graph regardless of whether changes are detected.
    """
    organization_id = _resolve_org_id(current_user)
    user_id = str(current_user.id)

    try:
        logger.info(f"Force syncing paper: {paper_id}")

        # Fetch paper details
        paper = await change_tracker._fetch_paper_details(paper_id)
        if not paper:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

        # Apply changes as if it were new/updated. Use the same async session
        # pattern the tracker uses internally (there is no get_db_session()).
        from src.core.database import get_async_session

        async with get_async_session() as db:
            # Check if the paper already exists for this org. The lookup accepts
            # canonical rows plus legacy metadata-only rows pending cleanup.
            existing = (
                await db.execute(
                    change_tracker._paper_lookup_stmt(paper_id, organization_id)
                )
            ).scalar_one_or_none()

            if existing:
                # Update existing
                await change_tracker._update_existing_paper(
                    db, paper, ["force_update"], organization_id
                )
                await change_tracker._update_knowledge_graph(paper, organization_id)
                action = "updated"
            else:
                action = "created"

        # Durable persistence owns its own short sessions and object-storage
        # phase; do not hold the lookup transaction across that I/O.
        if action == "created":
            await change_tracker._ingest_new_paper(
                paper, organization_id, user_id, update_kg=True
            )

        # Update org-partitioned tracking state
        org_state = change_tracker._org_state(organization_id)
        org_state[paper_id] = {
            "hash": change_tracker.compute_paper_hash(paper),
            "last_seen": datetime.utcnow().isoformat(),
            "paper_metadata": {
                "title": paper.get("title", ""),
                "authors": paper.get("authors", [])[:5],
                "primary_category": paper.get("primary_category"),
            },
        }
        change_tracker.save_state()

        return {
            "status": "success",
            "action": action,
            "paper_id": paper_id,
            "title": paper.get("title", ""),
            "message": f"Successfully {action} paper {paper_id}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to force sync paper {paper_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
