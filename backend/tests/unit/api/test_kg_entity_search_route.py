"""GET /knowledge-graph/entities/search must reach the search handler.

FastAPI matches routes in registration order. `/entities/{entity_id}` was
registered before `/entities/search`, so a search request ran
`get_entity("search")` and returned 404 (audit finding KN8). The frontend's
relationship-target search and duplicate-entity check both call this route
and swallow the error, so users saw empty results instead of a failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.search import knowledge_graph as kg

pytestmark = pytest.mark.unit


@pytest.fixture
def client_and_calls(monkeypatch: pytest.MonkeyPatch) -> Any:
    calls: Dict[str, List[Any]] = {"search": [], "get": []}

    def fake_search(query: str, *args: Any, **kwargs: Any) -> List[Any]:
        calls["search"].append(query)
        return []

    def fake_get(entity_id: str, *args: Any, **kwargs: Any) -> None:
        calls["get"].append(entity_id)
        return None

    monkeypatch.setattr(kg.knowledge_graph_service, "search_entities", fake_search)
    monkeypatch.setattr(kg.knowledge_graph_service, "get_entity", fake_get)

    app = FastAPI()
    app.include_router(kg.router)
    app.dependency_overrides[kg.get_current_user] = lambda: SimpleNamespace(
        id="u1", organization_id="org-A"
    )
    app.dependency_overrides[kg.get_db_sync] = lambda: None
    return TestClient(app), calls


def test_entity_search_route_reaches_search_handler(client_and_calls: Any) -> None:
    client, calls = client_and_calls
    response = client.get("/knowledge-graph/entities/search", params={"query": "BERT"})
    assert response.status_code == 200
    assert response.json() == []
    assert calls["search"] == ["BERT"]
    assert calls["get"] == []


def test_entity_by_id_route_still_works(client_and_calls: Any) -> None:
    client, calls = client_and_calls
    response = client.get("/knowledge-graph/entities/some-entity-id")
    assert response.status_code == 404
    assert calls["get"] == ["some-entity-id"]
    assert calls["search"] == []
