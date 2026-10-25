"""F6: public error bodies must carry stable safe messages, never str(e).

- GET /search-quality/health (unauthenticated) returned ``"error": str(e)``.
- GET /search-quality/analytics raised ``HTTPException(500, analytics["error"])``
  where the service puts ``str(e)`` into that key.
- GET /knowledge-graph/entities raised ``HTTPException(503, str(e))`` for
  Neo4j-unavailable errors (driver/circuit-breaker text).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.search import knowledge_graph as kg
from src.api.search import search_quality as quality_api
from src.core.circuit_breaker import ServiceUnavailableError as CircuitBreakerError

pytestmark = pytest.mark.unit

SECRET = "bolt://neo4j:hunter2@10.0.0.7:7687 pool exhausted"


async def test_quality_health_does_not_echo_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quality_api.search_quality_service,
        "_calculate_overall_score",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(SECRET)),
    )

    response = await quality_api.health_check()

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == "unhealthy"
    assert body["service"] == "search_quality_evaluation"
    assert "hunter2" not in json.dumps(body)
    assert body["error"] == "search quality service unavailable"


async def test_quality_health_happy_path_unchanged() -> None:
    response = await quality_api.health_check()
    assert response["status"] == "healthy"
    assert response["service"] == "search_quality_evaluation"


async def test_quality_analytics_error_key_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quality_api.search_quality_service,
        "get_quality_analytics",
        lambda **_k: {"error": SECRET},
    )
    user = SimpleNamespace(id="u", organization_id="org-1")

    with pytest.raises(HTTPException) as exc:
        await quality_api.get_quality_analytics(
            days=30, search_type=None, current_user=user
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Internal server error"


def test_kg_entities_503_uses_stable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "get_all_entities",
        lambda *_a, **_k: (_ for _ in ()).throw(CircuitBreakerError("neo4j", SECRET)),
    )
    user = SimpleNamespace(id="u", organization_id="org-1")

    with pytest.raises(HTTPException) as exc:
        kg.get_all_entities(
            limit=10,
            offset=0,
            entity_types=None,
            connected_only=False,
            project_id=None,
            current_user=user,
            db=MagicMock(),
        )

    assert exc.value.status_code == 503
    assert "hunter2" not in str(exc.value.detail)
    assert exc.value.detail == "Knowledge graph temporarily unavailable"


def test_kg_entities_non_unavailable_errors_stay_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "get_all_entities",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(SECRET)),
    )
    user = SimpleNamespace(id="u", organization_id="org-1")

    with pytest.raises(HTTPException) as exc:
        kg.get_all_entities(
            limit=10,
            offset=0,
            entity_types=None,
            connected_only=False,
            project_id=None,
            current_user=user,
            db=MagicMock(),
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "Internal server error"
