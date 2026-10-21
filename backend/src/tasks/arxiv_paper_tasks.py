"""Administrator-triggered arXiv import on the existing Celery worker."""

from uuid import UUID

from src.services.arxiv.recent import import_recent_papers
from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app


@celery_app.task(
    name="src.tasks.arxiv_paper_tasks.import_recent",
    soft_time_limit=900,
    time_limit=960,
)
def import_recent(
    organization_id: str,
    user_id: str,
    categories: list[str],
    max_results: int,
    days: int,
) -> dict[str, int]:
    return run_async(
        import_recent_papers(
            UUID(organization_id), UUID(user_id), categories, max_results, days
        ),
        timeout=850,
    )
