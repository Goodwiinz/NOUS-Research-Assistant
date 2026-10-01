"""GOO-289: every user-reachable expensive arXiv route shares the org budget.

#1661 metered ``/ingest`` and ``/create-dataset``; the sibling routers that
also search arXiv, download/parse PDFs or call the entity-extraction LLM were
still unmetered, so the aggregate budget could be bypassed by switching path.
"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from src.api.arxiv import (
    arxiv_change_router,
    arxiv_extraction_router,
    arxiv_kg_router,
    arxiv_local_router,
)
from src.models.user import User
from src.services import expensive_work_admission
from src.services.expensive_work_admission import require_expensive_work_admission

EXPENSIVE = [
    (arxiv_kg_router, "/bulk-ingest"),
    (arxiv_extraction_router, "/extract-features"),
    (arxiv_extraction_router, "/bulk-extract"),
    (arxiv_local_router, "/extract-local-features"),
    (arxiv_local_router, "/process-batch"),
    (arxiv_change_router, "/track-categories"),
    (arxiv_change_router, "/track-all"),
    (arxiv_change_router, "/force-sync"),
]


def _route(router: Any, path: str) -> APIRoute:
    return next(r for r in router.routes if isinstance(r, APIRoute) and r.path == path)


@pytest.mark.parametrize(("router", "path"), EXPENSIVE)
def test_expensive_arxiv_route_requires_shared_admission(
    router: Any, path: str
) -> None:
    calls = [d.call for d in _route(router, path).dependant.dependencies]
    assert require_expensive_work_admission in calls


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
