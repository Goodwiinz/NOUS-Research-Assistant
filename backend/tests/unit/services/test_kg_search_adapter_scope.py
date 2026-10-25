"""The KG search adapter scopes by organization (plus the org's document
ids, for environments whose organization_id backfill has not run), never
widens to unscoped, and never invents document ids for entities without a
source document.

It used to stamp ``uuid.uuid4()`` as the ``document_id`` for entities with
no ``source_document_id``: a random id that 404s as an "open document"
target.
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


def _db_that_must_not_be_queried() -> MagicMock:
    db = MagicMock()
    db.query.side_effect = AssertionError("no query may run without an org")
    return db


def _db_with_org_documents(*doc_ids: str) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [(d,) for d in doc_ids]
    return db


def _capturing(captured: dict) -> Any:
    def _search_entities(query: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return [_entity("e1", "doc-1")]

    return _search_entities


def test_search_scopes_by_org_and_org_documents() -> None:
    svc = KnowledgeGraphService()
    captured: dict = {}
    svc.search_entities = _capturing(captured)  # type: ignore[method-assign]

    response = svc.search(
        search_request=SimpleNamespace(query="alice", limit=10, offset=0),
        user_id="user-1",
        organization_id="org-1",
        db=_db_with_org_documents("doc-1", "doc-2"),
    )

    assert captured["organization_id"] == "org-1"
    assert captured["source_document_ids"] == ["doc-1", "doc-2"]
    # Source-less entities are filtered in the query, before LIMIT.
    assert captured["require_source_document"] is True
    assert [r.document_id for r in response.results] == ["doc-1"]


def test_document_lookup_failure_keeps_org_scope() -> None:
    """A failed document lookup leaves the indexed org equality in place; it
    must never drop to an unscoped query."""
    svc = KnowledgeGraphService()
    captured: dict = {}
    svc.search_entities = _capturing(captured)  # type: ignore[method-assign]
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")

    svc.search(
        search_request=SimpleNamespace(query="alice", limit=10, offset=0),
        user_id="user-1",
        organization_id="org-1",
        db=db,
    )

    assert captured["organization_id"] == "org-1"
    assert captured["source_document_ids"] is None


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
        db=_db_that_must_not_be_queried(),
    )

    assert response.results == []
    assert response.total_results == 0


@pytest.mark.parametrize("query", ["alice", ""])
def test_require_source_document_filters_before_limit(query: str) -> None:
    """The filter is in the Cypher WHERE, so LIMIT counts only entities that
    can be mapped to a document (fulltext and list/CONTAINS paths)."""
    from unittest.mock import patch

    session = MagicMock()
    session.__enter__.return_value = session
    session.run.return_value = []

    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        KnowledgeGraphService().search_entities(
            query, limit=5, organization_id="org-1", require_source_document=True
        )

    cypher = session.run.call_args.args[0]
    where = cypher[: cypher.index("LIMIT")]
    assert "e.source_document_id IS NOT NULL" in where
