"""D2: KG reads and writes with no tenant scope must match nothing.

Every production caller passes an organization_id or a document list, so a
call with neither is a dropped scope. Before this change the helpers warned
and returned an unscoped query that spanned every organization; the
destructive paths (delete_entity, delete_relationship) would then act on
another tenant's graph by bare id.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit


def _session() -> MagicMock:
    session = MagicMock()
    session.__enter__.return_value = session
    empty = MagicMock()
    empty.single.return_value = None
    empty.__iter__.return_value = iter([])
    session.run.return_value = empty
    return session


def _queries(session: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in session.run.call_args_list]


def _where_false(query: str) -> bool:
    return "WHERE false" in query or "AND false" in query


def test_unscoped_delete_entity_cannot_match_any_node() -> None:
    session = _session()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        KnowledgeGraphService().delete_entity("entity-of-another-tenant")
    deletes = [q for q in _queries(session) if "DELETE" in q.upper()]
    assert deletes, "delete_entity issued no DELETE query"
    assert all(_where_false(q) for q in deletes)


def test_unscoped_get_entity_cannot_match_any_node() -> None:
    session = _session()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        result = KnowledgeGraphService().get_entity("entity-of-another-tenant")
    assert result is None
    assert any(_where_false(q) for q in _queries(session))


def test_unscoped_find_paths_cannot_traverse() -> None:
    session = _session()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        KnowledgeGraphService().find_paths("a", "b")
    queries = _queries(session)
    assert queries and all(_where_false(q) for q in queries)


def test_org_scoped_call_keeps_the_org_predicate() -> None:
    session = _session()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        KnowledgeGraphService().get_entity("e-1", organization_id="org-1")
    queries = _queries(session)
    params: dict[str, Any] = session.run.call_args.args[1]
    assert any("e.organization_id = $organization_id" in q for q in queries)
    assert not any(_where_false(q) for q in queries)
    assert params["organization_id"] == "org-1"
