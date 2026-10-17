"""Bounded recent-paper import; no inferred automatic refresh schedule."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from src.core.database import AsyncSessionLocal
from src.models.user import User, UserRole

from .arxiv_service import ArXivIngestionService
from .papers import CATEGORY_PATTERN, _store, normalize_paper_id, store_pdf


async def enqueue_recent(
    organization_id: UUID,
    user_id: UUID,
    categories: list[str],
    max_results: int,
    days: int,
) -> str:
    from src.tasks.arxiv_paper_tasks import import_recent

    result = await asyncio.to_thread(
        import_recent.delay,
        str(organization_id),
        str(user_id),
        categories,
        max_results,
        days,
    )
    return str(result.id)


async def _authorized(organization_id: UUID, user_id: UUID) -> bool:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(User.id).where(
                    User.id == user_id,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                    User.role == UserRole.ADMIN,
                )
            )
        ).scalar_one_or_none() is not None


async def import_recent_papers(
    organization_id: UUID,
    user_id: UUID,
    categories: list[str],
    max_results: int,
    days: int,
) -> dict[str, int]:
    # Validate even for direct worker invocation, not just HTTP callers.
    import re

    if (
        not 1 <= max_results <= 50
        or not 1 <= days <= 7
        or not 1 <= len(categories) <= 10
        or any(
            not re.fullmatch(CATEGORY_PATTERN, category) or len(category) > 32
            for category in categories
        )
    ):
        raise ValueError("Invalid recent-paper import bounds")
    if not await _authorized(organization_id, user_id):
        raise PermissionError("Recent-paper import actor is no longer authorized")
    observed_at = datetime.now(timezone.utc)
    async with ArXivIngestionService() as upstream:
        papers = await upstream.search_papers(
            query="",
            categories=categories,
            max_results=max_results,
            date_from=observed_at - timedelta(days=days),
            date_to=observed_at,
            sort_by="submittedDate",
            sort_order="descending",
        )
    imported = failed = 0
    for paper in papers[:max_results]:
        # Delayed and long-running work must honor revoked tenant/admin access.
        if not await _authorized(organization_id, user_id):
            raise PermissionError("Recent-paper import actor is no longer authorized")
        async with AsyncSessionLocal() as db:
            paper_id = normalize_paper_id(paper["id"])
            await _store(
                db,
                organization_id,
                paper_id,
                {**paper, "withdrawn": None, "latest_id": None, "source": "arxiv_atom"},
                observed_at,
                if_missing=True,
            )
            try:
                await store_pdf(db, organization_id, user_id, paper_id)
            except Exception:  # noqa: BLE001 - preserve successfully imported papers
                failed += 1
            else:
                imported += 1
    return {"imported": imported, "failed": failed}
