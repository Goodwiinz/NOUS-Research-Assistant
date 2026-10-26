"""F5: search routes must reject an org-less user before any search call.

Every search handler did ``organization_id=str(current_user.organization_id)``,
turning a nullable column into the literal string ``"None"`` — which either
scoped the query to a shared ``"None"`` bucket or (for services that only
filter ``if organization_id:``) was indistinguishable from a real tenant.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api.search import search as search_api
from src.api.search import search_quality as quality_api
from src.models.search_schemas import SearchQuery, SearchType
from src.models.user import UserRole

pytestmark = pytest.mark.unit


def _user(org_id: str | None, role: UserRole = UserRole.USER) -> SimpleNamespace:
    return SimpleNamespace(id="user-1", role=role, organization_id=org_id)


def _forbid(name: str) -> Callable[..., Any]:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"{name} must not be called for an org-less user")

    return _boom


@pytest.fixture
def no_search_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_api.hybrid_search_service, "search", _forbid("hybrid.search")
    )
    monkeypatch.setattr(
        search_api.fulltext_search_service, "search", _forbid("fulltext.search")
    )


@pytest.mark.parametrize("org_id", [None, ""])
@pytest.mark.parametrize(
    "search_type", [SearchType.HYBRID, SearchType.FULLTEXT, SearchType.KNOWLEDGE_GRAPH]
)
async def test_search_documents_rejects_org_less_user(
    no_search_services: None, org_id: str | None, search_type: SearchType
) -> None:
    with pytest.raises(HTTPException) as exc:
        await search_api.search_documents(
            search_request=SearchQuery(query="hello", search_type=search_type),
            background_tasks=BackgroundTasks(),
            current_user=_user(org_id),
            db=MagicMock(),
        )
    assert exc.value.status_code == 403


async def test_hybrid_search_rejects_org_less_user(no_search_services: None) -> None:
    with pytest.raises(HTTPException) as exc:
        await search_api.hybrid_search(
            search_request=SearchQuery(query="hello"),
            background_tasks=BackgroundTasks(),
            current_user=_user(None),
            db=MagicMock(),
        )
    assert exc.value.status_code == 403


async def test_search_health_rejects_org_less_user(no_search_services: None) -> None:
    with pytest.raises(HTTPException) as exc:
        await search_api.search_health_check(current_user=_user(None), db=MagicMock())
    assert exc.value.status_code == 403


async def test_rebuild_rejects_org_less_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_api.fulltext_search_service,
        "get_rebuild_documents",
        _forbid("get_rebuild_documents"),
    )
    with pytest.raises(HTTPException) as exc:
        await search_api.rebuild_search_indexes(
            current_user=_user(None, role=UserRole.ADMIN), db=MagicMock()
        )
    assert exc.value.status_code == 403


async def test_benchmark_rejects_org_less_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quality_api.search_quality_service,
        "run_quality_benchmark",
        _forbid("run_quality_benchmark"),
    )
    with pytest.raises(HTTPException) as exc:
        await quality_api.run_quality_benchmark(
            request=quality_api.BenchmarkRequest(test_queries=["q"]),
            background_tasks=BackgroundTasks(),
            current_user=_user(None, role=UserRole.ADMIN),
        )
    assert exc.value.status_code == 403


async def test_search_documents_forwards_real_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scoped users keep working and the org is forwarded as a string."""
    seen: dict = {}

    def _search(
        search_request: Any, user_id: Any, organization_id: Any, db: Any
    ) -> SimpleNamespace:
        seen["organization_id"] = organization_id
        return SimpleNamespace(search_id="s", results=[], search_time_ms=1.0)

    monkeypatch.setattr(search_api.hybrid_search_service, "search", _search)
    monkeypatch.setattr(search_api, "log_search_query", lambda *a, **k: None)

    await search_api.search_documents(
        search_request=SearchQuery(query="hello", search_type=SearchType.HYBRID),
        background_tasks=BackgroundTasks(),
        current_user=_user("org-1"),
        db=MagicMock(),
    )
    assert seen["organization_id"] == "org-1"


@pytest.mark.parametrize("route", ["search_documents", "hybrid_search"])
async def test_saturated_hybrid_search_returns_503(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Out-of-capacity hybrid search is a retryable 503, not a 500 or an
    empty 200 that looks like NO_MATCH."""
    from src.services.search.hybrid_search_service import HybridSearchUnavailableError

    def _saturated(*_a: Any, **_k: Any) -> Any:
        raise HybridSearchUnavailableError("saturated")

    monkeypatch.setattr(search_api.hybrid_search_service, "search", _saturated)

    with pytest.raises(HTTPException) as exc:
        await getattr(search_api, route)(
            search_request=SearchQuery(query="hello", search_type=SearchType.HYBRID),
            background_tasks=BackgroundTasks(),
            current_user=_user("org-1"),
            db=MagicMock(),
        )
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "5"}
