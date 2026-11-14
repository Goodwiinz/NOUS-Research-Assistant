"""KG read methods must not turn a Neo4j outage into "no data" (audit #2123, item 4).

Before this fix ``search_entities``, ``get_entity``, ``count_entities``,
``find_related_entities``, ``get_neighborhood``, ``get_graph_analytics``,
``update_entity`` and ``delete_entity`` caught every exception and returned an
empty value ([] / None / 0 / empty GraphAnalytics / False), so a driver or
connection failure was indistinguishable from a genuine miss. ``get_all_entities``
already re-raised; the others now follow the same pattern.

Mutation check: revert the ``raise`` in any one method back to its old empty
return and the matching ``*_raises`` test below fails with ``DID NOT RAISE``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from neo4j.exceptions import ServiceUnavailable

from src.core.circuit_breaker import ServiceUnavailableError as CircuitBreakerError
from src.models.graph import UpdateEntityRequest
from src.models.search_schemas import SearchQuery, SearchType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService
from src.services.search.hybrid_search_service import HybridSearchService

pytestmark = pytest.mark.unit

ORG = "org-1"
GET_SESSION = (
    "src.services.knowledge_graph.knowledge_graph_service."
    "KnowledgeGraphService.get_session"
)


def _raising_session(exc: Exception) -> Any:
    """A get_session replacement whose context manager raises on entry.

    Mirrors the real ``get_session``: an open circuit breaker or a dead driver
    raises before any Cypher runs.
    """

    @contextmanager
    def _session(*_a: Any, **_k: Any) -> Iterator[Any]:
        raise exc
        yield  # pragma: no cover - unreachable, keeps the generator shape

    return _session


def _query_failing_session(exc: Exception) -> Any:
    """A get_session replacement whose ``session.run`` raises (query error)."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.run.side_effect = exc
    return MagicMock(return_value=ctx)


def _empty_session() -> Any:
    """A get_session replacement that returns a genuine empty result set."""
    result = MagicMock()
    result.single.return_value = None
    result.__iter__ = MagicMock(return_value=iter([]))
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.run.return_value = result
    return MagicMock(return_value=ctx)


READ_CALLS = [
    pytest.param(
        lambda s: s.search_entities("alice", organization_id=ORG),
        id="search_entities",
    ),
    pytest.param(
        lambda s: s.get_entity("e1", organization_id=ORG),
        id="get_entity",
    ),
    pytest.param(
        lambda s: s.count_entities(organization_id=ORG),
        id="count_entities",
    ),
    pytest.param(
        lambda s: s.find_related_entities("e1", organization_id=ORG),
        id="find_related_entities",
    ),
    pytest.param(
        lambda s: s.get_neighborhood("e1", organization_id=ORG),
        id="get_neighborhood",
    ),
    pytest.param(
        lambda s: s.get_graph_analytics(organization_id=ORG),
        id="get_graph_analytics",
    ),
    pytest.param(
        lambda s: s.update_entity(
            "e1", UpdateEntityRequest(name="renamed"), organization_id=ORG
        ),
        id="update_entity",
    ),
    pytest.param(
        lambda s: s.delete_entity("e1", organization_id=ORG),
        id="delete_entity",
    ),
]


@pytest.mark.parametrize("call", READ_CALLS)
def test_driver_unavailable_raises(call: Any) -> None:
    """A dead driver (ServiceUnavailable) must propagate, not become empty."""
    with patch(GET_SESSION, _raising_session(ServiceUnavailable("neo4j down"))):
        with pytest.raises(ServiceUnavailable):
            call(KnowledgeGraphService())


@pytest.mark.parametrize("call", READ_CALLS)
def test_open_circuit_breaker_raises(call: Any) -> None:
    """An open neo4j circuit breaker must propagate as CircuitBreakerError."""
    exc = CircuitBreakerError("neo4j", "Neo4j circuit breaker is open")
    with patch(GET_SESSION, _raising_session(exc)):
        with pytest.raises(CircuitBreakerError):
            call(KnowledgeGraphService())


@pytest.mark.parametrize("call", READ_CALLS)
def test_query_error_raises(call: Any) -> None:
    """A failure inside ``session.run`` (bad Cypher, server error) propagates too."""
    with patch(GET_SESSION, _query_failing_session(RuntimeError("query failed"))):
        with pytest.raises(RuntimeError, match="query failed"):
            call(KnowledgeGraphService())


def test_genuine_miss_still_returns_empty_values() -> None:
    """A real "not found" keeps the old empty-value contract."""
    with patch(GET_SESSION, _empty_session()):
        svc = KnowledgeGraphService()
        assert svc.get_entity("missing", organization_id=ORG) is None
        assert svc.search_entities("nothing", organization_id=ORG) == []
        assert svc.count_entities(organization_id=ORG) == 0
        assert svc.find_related_entities("missing", organization_id=ORG) == []
        assert (
            svc.update_entity(
                "missing", UpdateEntityRequest(name="x"), organization_id=ORG
            )
            is None
        )


def test_delete_genuine_miss_returns_false() -> None:
    result = MagicMock()
    result.single.return_value = {"deleted_count": 0}
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.run.return_value = result
    with patch(GET_SESSION, MagicMock(return_value=ctx)):
        assert (
            KnowledgeGraphService().delete_entity("missing", organization_id=ORG)
            is False
        )


def test_hybrid_graph_arm_reports_failure_when_neo4j_down() -> None:
    """The hybrid search KG arm must record success=False on an outage
    instead of a successful empty result."""
    with patch(GET_SESSION, _raising_session(ServiceUnavailable("neo4j down"))):
        svc = HybridSearchService()
        out = svc._execute_knowledge_graph_search(
            SearchQuery(query="alice", search_type=SearchType.HYBRID),
            user_id="u1",
            organization_id=ORG,
        )
    assert out.success is False
    assert out.results == []
