"""Graph data retrieval routes must reject an org-less caller up front.

Historically every tenant-scoped read route in ``api/search/knowledge_graph``
built its scope with ``str(current_user.organization_id)``. ``organization_id``
is nullable (an org delete SET NULLs it), so such a user produced the literal
string ``"None"``, which was handed to ``knowledge_graph_service`` — and, for
the project/doc-id scoping helpers, to Postgres — as if it were a tenant. Most
of those handlers also wrapped the call in ``except Exception``, so any
downstream failure became a 500 rather than a clear authorization error.

All fourteen read routes (entity get/list/search/relationships/related/
neighborhood/visualization, relationship get/list, graph search, path
finding, per-document entities, analytics, health) now call
``_require_org_id`` before their ``try`` block — the convention the
maintenance endpoints already used — returning 403 without touching the
database or the service. ``get_all_entities`` / ``search_entities`` have
dedicated tests; the rest are covered by the parametrized case below.
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.search import knowledge_graph as kg
from src.models.graph import GraphSearchRequest

pytestmark = pytest.mark.unit


def _org_less_user() -> MagicMock:
    user = MagicMock()
    user.id = "user-1"
    user.organization_id = None
    return user


def _forbidden_db() -> MagicMock:
    db = MagicMock()
    db.query.side_effect = AssertionError(
        "db.query must not run for an org-less caller"
    )
    db.execute.side_effect = AssertionError(
        "db.execute must not run for an org-less caller"
    )
    return db


_SERVICE_METHODS = (
    "get_all_entities",
    "count_entities",
    "search_entities",
    "get_entity",
    "get_relationships",
    "find_related_entities",
    "get_neighborhood",
    "get_all_relationships",
    "get_relationship",
    "get_relationships_for_entities",
    "find_paths",
    "get_graph_analytics",
    "get_health_status",
)

# Module-level Postgres scoping helpers the routes call before the service.
_DB_HELPERS = ("_scope_doc_ids", "_get_org_document_ids", "_get_project_document_ids")


@pytest.fixture
def service_must_not_be_called(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict[str, int] = {}

    def _forbid(name: str) -> Callable[..., Any]:
        def _boom(*_a: Any, **_k: Any) -> Any:
            calls[name] = calls.get(name, 0) + 1
            raise AssertionError(f"{name} must not run for an org-less caller")

        return _boom

    for name in _SERVICE_METHODS:
        monkeypatch.setattr(kg.knowledge_graph_service, name, _forbid(name))
    for name in _DB_HELPERS:
        monkeypatch.setattr(kg, name, _forbid(name))
    return calls


def test_get_all_entities_rejects_org_less_user(
    service_must_not_be_called: dict,
) -> None:
    with pytest.raises(HTTPException) as exc:
        kg.get_all_entities(
            limit=10,
            offset=0,
            entity_types=None,
            connected_only=False,
            project_id=None,
            current_user=_org_less_user(),
            db=_forbidden_db(),
        )

    assert exc.value.status_code == 403
    assert service_must_not_be_called == {}


def test_search_entities_rejects_org_less_user(
    service_must_not_be_called: dict,
) -> None:
    with pytest.raises(HTTPException) as exc:
        kg.search_entities(
            query="alice",
            entity_types=None,
            limit=10,
            project_id=None,
            current_user=_org_less_user(),
            db=_forbidden_db(),
        )

    assert exc.value.status_code == 403
    assert service_must_not_be_called == {}


# ---------------------------------------------------------------------------
# Remaining graph data retrieval routes (same pattern, one case per route).
# Each callable invokes the route function directly with an org-less user
# and a DB stub that fails on any query.
# ---------------------------------------------------------------------------

_RETRIEVAL_ROUTES: dict[str, Callable[[MagicMock, MagicMock], object]] = {
    "get_entity": lambda user, db: kg.get_entity(
        entity_id="entity-1", current_user=user, db=db
    ),
    "get_entity_relationships": lambda user, db: kg.get_entity_relationships(
        entity_id="entity-1", relationship_types=None, current_user=user, db=db
    ),
    "get_related_entities": lambda user, db: kg.get_related_entities(
        entity_id="entity-1",
        max_depth=2,
        min_strength=0.1,
        limit=10,
        current_user=user,
        db=db,
    ),
    "get_entity_neighborhood": lambda user, db: kg.get_entity_neighborhood(
        entity_id="entity-1",
        max_depth=2,
        min_strength=0.1,
        limit=10,
        current_user=user,
        db=db,
    ),
    "get_all_relationships": lambda user, db: kg.get_all_relationships(
        limit=10,
        offset=0,
        relationship_types=None,
        project_id=None,
        current_user=user,
        db=db,
    ),
    "get_relationship": lambda user, db: kg.get_relationship(
        relationship_id="rel-1", current_user=user, db=db
    ),
    "search_graph": lambda user, db: kg.search_graph(
        request=GraphSearchRequest(query="alice"), current_user=user, db=db
    ),
    "find_paths": lambda user, db: kg.find_paths(
        source_id="entity-1",
        target_id="entity-2",
        max_depth=3,
        min_strength=0.1,
        current_user=user,
        db=db,
    ),
    "get_document_entities": lambda user, db: kg.get_document_entities(
        document_id="doc-1", current_user=user, db=db
    ),
    "get_graph_analytics": lambda user, db: kg.get_graph_analytics(
        project_id=None, current_user=user, db=db
    ),
    "get_graph_health": lambda user, db: kg.get_graph_health(current_user=user),
    "get_entity_visualization": lambda user, db: kg.get_entity_visualization(
        entity_id="entity-1", depth=2, max_nodes=10, current_user=user, db=db
    ),
}


@pytest.mark.parametrize(
    "route", sorted(_RETRIEVAL_ROUTES), ids=sorted(_RETRIEVAL_ROUTES)
)
def test_graph_retrieval_route_rejects_org_less_user(
    route: str, service_must_not_be_called: dict
) -> None:
    invoke = _RETRIEVAL_ROUTES[route]

    with pytest.raises(HTTPException) as exc:
        invoke(_org_less_user(), _forbidden_db())

    assert exc.value.status_code == 403, route
    assert service_must_not_be_called == {}, route


@pytest.mark.parametrize(
    "call",
    [
        lambda kg, user: kg.update_entity(
            "e1", MagicMock(), current_user=user, db=MagicMock()
        ),
        lambda kg, user: kg.delete_entity("e1", current_user=user, db=MagicMock()),
        lambda kg, user: kg.delete_relationship(
            "r1", current_user=user, db=MagicMock()
        ),
    ],
    ids=["update_entity", "delete_entity", "delete_relationship"],
)
def test_kg_writes_reject_orgless_caller(call: Callable[..., Any]) -> None:
    """Writes must 403 an org-less caller, never act on a phantom "None" tenant."""
    from fastapi import HTTPException

    from src.api.search import knowledge_graph as kg

    user = MagicMock()
    user.organization_id = None
    with pytest.raises(HTTPException) as exc:
        call(kg, user)
    assert exc.value.status_code == 403
