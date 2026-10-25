"""F3: the KG search adapter must not enumerate the org's documents per query
and must not invent document ids for entities without a source document.

``KnowledgeGraphService.search`` used to ``SELECT id FROM documents WHERE
organization_id = ?`` on every call (full per-tenant enumeration) just to
build a ``source_document_id IN [...]`` list, even though the indexed
``organization_id`` equality is already the primary scope. It also stamped
``uuid.uuid4()`` as the ``document_id`` for entities with no
``source_document_id`` — a random id that 404s as an "open document" target.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.models.graph import EntityType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit


def _entity(entity_id: str, source_document_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=entity_id,
        name=f"Entity {entity_id}",
        entity_type=EntityType.PERSON,
        confidence_score=0.9,
        context="ctx",
        source_document_id=source_document_id,
        created_at=datetime(2026, 1, 1),
        updated_at=None,
    )


def _db_that_must_not_enumerate() -> MagicMock:
    db = MagicMock()
    db.query.side_effect = AssertionError("documents must not be enumerated")
    return db


def test_search_uses_indexed_org_filter_without_document_enumeration() -> None:
    svc = KnowledgeGraphService()
    captured: dict = {}

    def _search_entities(query: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return [_entity("e1", "doc-1")]

    svc.search_entities = _search_entities  # type: ignore[method-assign,assignment]
    db = _db_that_must_not_enumerate()

    response = svc.search(
        search_request=SimpleNamespace(query="alice", limit=10, offset=0),
        user_id="user-1",
        organization_id="org-1",
        db=db,
    )

    db.query.assert_not_called()
    assert captured["organization_id"] == "org-1"
    assert captured.get("source_document_ids") is None
    assert [r.document_id for r in response.results] == ["doc-1"]


def test_entities_without_source_document_are_omitted_not_fabricated() -> None:
    svc = KnowledgeGraphService()
    svc.search_entities = lambda *a, **k: [  # type: ignore[method-assign]
        _entity("e1", "doc-1"),
        _entity("e2", None),
        _entity("e3", ""),
    ]

    response = svc.search(
        search_request=SimpleNamespace(query="alice", limit=10, offset=0),
        user_id="user-1",
        organization_id="org-1",
        db=None,
    )

    assert [r.document_id for r in response.results] == ["doc-1"]
    assert response.total_results == 1
    assert response.returned_results == 1


def test_search_without_organization_fails_closed() -> None:
    """F5: an org-less caller must never reach search_entities (unscoped)."""
    svc = KnowledgeGraphService()

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("search_entities must not run without an org")

    svc.search_entities = _boom  # type: ignore[method-assign]

    response = svc.search(
        search_request=SimpleNamespace(query="alice", limit=10, offset=0),
        user_id="user-1",
        organization_id=None,  # type: ignore[arg-type]
        db=_db_that_must_not_enumerate(),
    )

    assert response.results == []
    assert response.total_results == 0
