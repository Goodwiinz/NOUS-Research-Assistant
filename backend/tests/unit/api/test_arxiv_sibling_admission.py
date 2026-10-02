"""GOO-289: every user-reachable expensive arXiv route shares the org budget.

#1661 metered ``/ingest`` and ``/create-dataset``; the sibling routers that
also search arXiv, download/parse PDFs or call the entity-extraction LLM were
still unmetered, so the aggregate budget could be bypassed by switching path.
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api.arxiv import (
    arxiv_bulk_router,
    arxiv_change_router,
    arxiv_extraction_router,
    arxiv_kg_router,
    arxiv_llm_bulk_router,
    arxiv_local_router,
    arxiv_router,
)
from src.core.dependencies import get_current_user
from src.models.user import User
from src.services import expensive_work_admission
from src.services.expensive_work_admission import require_expensive_work_admission

USER_ROUTERS = [
    arxiv_router,
    arxiv_kg_router,
    arxiv_change_router,
    arxiv_extraction_router,
    arxiv_local_router,
]
EXPENSIVE = {
    "/api/v1/arxiv/ingest",  # in-handler admission (#1661)
    "/api/v1/arxiv/create-dataset",  # in-handler admission (#1661)
    "/api/v1/arxiv/download/{paper_id}",  # external PDF download (<=50 MiB)
    "/api/v1/arxiv/statistics",  # arXiv search, max_results=1000
    "/bulk-ingest",
    "/extract-features",
    "/bulk-extract",
    "/extract-local-features",
    "/process-batch",
    "/track-categories",
    "/track-all",
    "/force-sync",
}
CHEAP = {
    "/api/v1/arxiv/search",  # public discovery search, own row cap
    "/api/v1/arxiv/categories",  # static
    "/history",
    "/stats",
    "/cleanup",
    "/extracted-features",
    "/local-papers",
    "/local-stats",
}
IN_HANDLER = {"/api/v1/arxiv/ingest", "/api/v1/arxiv/create-dataset"}


def _routes(router: Any) -> list[APIRoute]:
    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_route_inventory_is_exhaustive() -> None:
    """A new user-reachable arXiv route must be classified, not silently free."""
    paths = {r.path for router in USER_ROUTERS for r in _routes(router)}
    assert paths == EXPENSIVE | CHEAP


def test_bulk_routers_are_platform_operator_only() -> None:
    # Exempt from the org budget only because no tenant user can reach them.
    for router in (arxiv_bulk_router, arxiv_llm_bulk_router):
        names = {d.dependency.__name__ for d in router.dependencies}
        assert "require_platform_operator" in names


@pytest.mark.parametrize("path", sorted(EXPENSIVE - IN_HANDLER))
def test_expensive_arxiv_route_requires_shared_admission(path: str) -> None:
    route = next(
        r for router in USER_ROUTERS for r in _routes(router) if r.path == path
    )
    assert getattr(route.endpoint, "expensive_work_admission", False)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(arxiv_change_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), organization_id=uuid4(), organization=None
    )
    return TestClient(app)


def test_invalid_request_does_not_consume_budget() -> None:
    """Codex P2: validation (422) must run before the budget is debited."""
    admit = AsyncMock(return_value=True)
    with patch.object(expensive_work_admission, "admit_expensive_work", admit):
        response = _client().get("/track-all", params={"days_back": 0})
    assert response.status_code == 422
    admit.assert_not_awaited()


def test_valid_request_is_metered_and_denied_with_429() -> None:
    admit = AsyncMock(return_value=False)
    with patch.object(expensive_work_admission, "admit_expensive_work", admit):
        response = _client().get("/track-all", params={"days_back": 1})
    assert response.status_code == 429
    admit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admission_denied_is_429(monkeypatch: pytest.MonkeyPatch) -> None:
    async def deny(**_: Any) -> bool:
        return False

    monkeypatch.setattr(expensive_work_admission, "admit_expensive_work", deny)
    user = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))
    with pytest.raises(HTTPException) as exc:
        await require_expensive_work_admission(user)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_admission_requires_verified_org(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(**_: Any) -> bool:
        return True

    monkeypatch.setattr(expensive_work_admission, "admit_expensive_work", allow)
    user = cast(User, SimpleNamespace(id=uuid4(), organization_id=None))
    with pytest.raises(HTTPException) as exc:
        await require_expensive_work_admission(user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admission_passes_org_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, Any]] = []

    async def allow(*, user_id: Any, organization_id: Any) -> bool:
        seen.append((user_id, organization_id))
        return True

    monkeypatch.setattr(expensive_work_admission, "admit_expensive_work", allow)
    user = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))
    assert await require_expensive_work_admission(user) is user
    assert seen == [(user.id, user.organization_id)]
