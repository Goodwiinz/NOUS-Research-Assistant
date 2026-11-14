"""KG routes return 503 with a stable message when Neo4j is unavailable.

Audit #2123 item 4: the service's read methods used to swallow driver errors,
so ``GET /knowledge-graph/entities/{id}`` answered 404 "Entity not found" and
the search routes answered 200 ``[]`` during an outage. Now that the service
re-raises, every route that calls one of those methods must map a
Neo4j-unavailable error to 503 (never a 404, never a 500 with raw text) and
leave genuine misses at 404.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from neo4j.exceptions import ServiceUnavailable

from src.api.search import knowledge_graph as kg
from src.core.circuit_breaker import ServiceUnavailableError as CircuitBreakerError
from src.models.graph import GraphSearchRequest, UpdateEntityRequest

pytestmark = pytest.mark.unit

SECRET = "bolt://neo4j:hunter2@10.0.0.7:7687 pool exhausted"
USER = SimpleNamespace(id="u", organization_id="org-1")


def _raiser(exc: Exception) -> Any:
    def _fn(*_a: Any, **_k: Any) -> Any:
        raise exc

    return _fn


def _assert_503(exc: HTTPException) -> None:
    assert exc.status_code == 503
    assert exc.detail == "Knowledge graph temporarily unavailable"
    assert "hunter2" not in str(exc.detail)


@pytest.mark.parametrize(
    "exc",
    [ServiceUnavailable(SECRET), CircuitBreakerError("neo4j", SECRET)],
    ids=["driver", "breaker"],
)
def test_get_entity_route_returns_503_not_404(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(kg.knowledge_graph_service, "get_entity", _raiser(exc))
    with pytest.raises(HTTPException) as info:
        kg.get_entity("e1", current_user=USER, db=MagicMock())
    _assert_503(info.value)


def test_get_entity_route_genuine_miss_stays_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service, "get_entity", lambda *_a, **_k: None
    )
    with pytest.raises(HTTPException) as info:
        kg.get_entity("e1", current_user=USER, db=MagicMock())
    assert info.value.status_code == 404


def test_update_entity_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "update_entity",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.update_entity(
            "e1", UpdateEntityRequest(name="x"), current_user=USER, db=MagicMock()
        )
    _assert_503(info.value)


def test_delete_entity_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "delete_entity",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.delete_entity("e1", current_user=USER, db=MagicMock())
    _assert_503(info.value)


def test_search_entities_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "search_entities",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.search_entities(
            query="alice",
            entity_types=None,
            limit=10,
            project_id=None,
            current_user=USER,
            db=MagicMock(),
        )
    _assert_503(info.value)


def test_search_entities_route_other_errors_stay_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service, "search_entities", _raiser(RuntimeError(SECRET))
    )
    with pytest.raises(HTTPException) as info:
        kg.search_entities(
            query="alice",
            entity_types=None,
            limit=10,
            project_id=None,
            current_user=USER,
            db=MagicMock(),
        )
    assert info.value.status_code == 500
    assert info.value.detail == "Internal server error"


def test_related_entities_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "find_related_entities",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.get_related_entities(
            "e1", max_depth=2, min_strength=0.1, limit=10, current_user=USER, db=MagicMock()
        )
    _assert_503(info.value)


def test_neighborhood_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "get_neighborhood",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.get_entity_neighborhood(
            "e1", max_depth=2, min_strength=0.1, limit=10, current_user=USER, db=MagicMock()
        )
    _assert_503(info.value)


def test_graph_search_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "search_entities",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.search_graph(
            GraphSearchRequest(query="alice"), current_user=USER, db=MagicMock()
        )
    _assert_503(info.value)


def test_analytics_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "get_graph_analytics",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.get_graph_analytics(project_id=None, current_user=USER, db=MagicMock())
    _assert_503(info.value)


def test_visualization_route_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "get_entity",
        _raiser(ServiceUnavailable(SECRET)),
    )
    with pytest.raises(HTTPException) as info:
        kg.get_entity_visualization(
            "e1", depth=2, max_nodes=20, current_user=USER, db=MagicMock()
        )
    _assert_503(info.value)
