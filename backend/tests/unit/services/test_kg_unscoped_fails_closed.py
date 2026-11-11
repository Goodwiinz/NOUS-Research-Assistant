"""D2: KG reads and writes with no tenant scope must fail closed.

Every production caller passes an organization_id or a document list, so a
call with neither is a dropped scope. Before this change the helpers warned
and returned an unscoped query that spanned every organization; the
destructive paths (delete_entity, delete_relationship) would then act on
another tenant's graph by bare id, and the relationship listings, the
relationship write and graph analytics ran over the whole graph.

``_FakeSession`` stands in for a neo4j session over a graph that holds one
node/edge belonging to ANOTHER tenant. Like the real engine, a query matches
that row unless its WHERE carries the fail-closed ``false`` predicate. So the
delete tests assert the real outcome (nothing deleted, ``False`` returned, no
swallowed exception), not only the Cypher text.

Mutation check (docs/engineering/testing.md), run from ``backend/``:

1. ``_entity_scope_predicate`` guard: change ``return UNSCOPED_MATCHES_NOTHING``
   to ``return ""`` (the pre-D2 unscoped branch). Then
   ``pytest -q tests/unit/services/test_kg_unscoped_fails_closed.py -k delete_entity``
   fails: delete_entity returns True (it deleted another tenant's node).
2. ``_two_endpoint_scope`` guard: change
   ``return f"\\n  AND {UNSCOPED_MATCHES_NOTHING}", {}`` to ``return "", {}``.
   Then ``-k delete_relationship`` fails the same way.
3. ``get_all_relationships`` guard: delete the
   ``if not _has_tenant_scope(source_document_ids, organization_id):`` block.
   Then ``-k "unscoped_read and get_all_relationships"`` fails: a query runs.
4. ``create_relationship`` guard: delete the
   ``if not request.organization_id and source_document_ids is None:`` block.
   Then ``-k create_relationship`` fails: the MERGE runs unscoped.

Restore each guard and confirm ``git diff`` on the service shows only the fix.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterator, Optional
from unittest.mock import patch

import pytest

from src.models.graph import CreateRelationshipRequest, GraphAnalytics, RelationshipType
from src.services.knowledge_graph.knowledge_graph_service import (
    UNSCOPED_ERROR_CATEGORY,
    KnowledgeGraphService,
    RelationshipScopeError,
)

pytestmark = pytest.mark.unit


def _where_false(query: str) -> bool:
    return "WHERE false" in query or "AND false" in query


class _FakeResult:
    def __init__(self, query: str, matches: bool) -> None:
        self._query = query
        self._matches = matches

    def single(self) -> Optional[dict[str, Any]]:
        if "deleted_count" in self._query:
            # A real DELETE ... RETURN count(x) always yields one row.
            return {"deleted_count": 1 if self._matches else 0}
        return None

    def __iter__(self) -> Iterator[Any]:
        return iter([])


class _FakeSession:
    """Session over a graph holding one row that belongs to another tenant."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(
        self, query: str, params: Optional[dict[str, Any]] = None, **kw: Any
    ) -> "_FakeResult":
        self.queries.append(str(query))
        self.params.append(dict(params or {}, **kw))
        return _FakeResult(str(query), matches=not _where_false(str(query)))


def _run(call: Callable[[KnowledgeGraphService], Any]) -> tuple[Any, _FakeSession]:
    session = _FakeSession()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        return call(KnowledgeGraphService()), session


def _fail_closed_logged(caplog: pytest.LogCaptureFixture, method: str) -> bool:
    return any(
        getattr(r, "error_category", None) == UNSCOPED_ERROR_CATEGORY
        and method in getattr(r, "kg_method", "")
        and r.levelno == logging.ERROR
        for r in caplog.records
    )


# --- destructive paths: the query runs but cannot match ----------------------


def test_unscoped_delete_entity_deletes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result, session = _run(lambda s: s.delete_entity("entity-of-another-tenant"))

    assert result is False
    deletes = [q for q in session.queries if "DELETE" in q.upper()]
    assert deletes and all(_where_false(q) for q in deletes)
    # The False must come from deleted_count == 0, not a swallowed exception.
    assert not any("Error deleting entity" in r.getMessage() for r in caplog.records)
    assert _fail_closed_logged(caplog, "_entity_scope_predicate")


def test_scoped_delete_entity_deletes_in_tenant() -> None:
    result, session = _run(lambda s: s.delete_entity("e-1", organization_id="org-1"))

    assert result is True
    assert not any(_where_false(q) for q in session.queries)
    assert session.params[0]["organization_id"] == "org-1"


def test_unscoped_delete_relationship_deletes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result, session = _run(lambda s: s.delete_relationship("rel-of-another-tenant"))

    assert result is False
    deletes = [q for q in session.queries if "DELETE" in q.upper()]
    assert deletes and all(_where_false(q) for q in deletes)
    assert not any(
        "Error deleting relationship" in r.getMessage() for r in caplog.records
    )
    assert _fail_closed_logged(caplog, "_two_endpoint_scope")


def test_scoped_delete_relationship_deletes_in_tenant() -> None:
    result, session = _run(
        lambda s: s.delete_relationship("rel-1", organization_id="org-1")
    )

    assert result is True
    assert not any(_where_false(q) for q in session.queries)
    assert session.params[0]["organization_id"] == "org-1"


def test_unscoped_get_entity_cannot_match_any_node() -> None:
    result, session = _run(lambda s: s.get_entity("entity-of-another-tenant"))
    assert result is None
    assert any(_where_false(q) for q in session.queries)


def test_unscoped_find_paths_cannot_traverse() -> None:
    _, session = _run(lambda s: s.find_paths("a", "b"))
    assert session.queries and all(_where_false(q) for q in session.queries)


def test_org_scoped_call_keeps_the_org_predicate() -> None:
    _, session = _run(lambda s: s.get_entity("e-1", organization_id="org-1"))
    assert any("e.organization_id = $organization_id" in q for q in session.queries)
    assert not any(_where_false(q) for q in session.queries)
    assert session.params[-1]["organization_id"] == "org-1"


# --- reads and analytics: no query runs at all --------------------------------

_UNSCOPED_READS: list[tuple[str, Callable[..., Any], Any]] = [
    ("get_all_relationships", lambda s, **kw: s.get_all_relationships(**kw), []),
    ("get_relationships", lambda s, **kw: s.get_relationships("e-1", **kw), []),
    (
        "get_relationships_among",
        lambda s, **kw: s.get_relationships_among(["e-1", "e-2"], **kw),
        [],
    ),
    (
        "get_relationships_for_entities",
        lambda s, **kw: s.get_relationships_for_entities(["e-1"], **kw),
        [],
    ),
    (
        "get_graph_analytics",
        lambda s, **kw: s.get_graph_analytics(**kw),
        GraphAnalytics(),
    ),
]


@pytest.mark.parametrize(
    ("method", "call", "empty"), _UNSCOPED_READS, ids=[m for m, _, _ in _UNSCOPED_READS]
)
def test_unscoped_read_runs_no_query(
    method: str,
    call: Callable[..., Any],
    empty: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result, session = _run(call)

    assert result == empty
    assert session.queries == [], f"{method} ran an unscoped query"
    assert _fail_closed_logged(caplog, method)


@pytest.mark.parametrize(
    ("method", "call", "empty"), _UNSCOPED_READS, ids=[m for m, _, _ in _UNSCOPED_READS]
)
def test_org_scoped_read_still_queries(
    method: str, call: Callable[..., Any], empty: Any
) -> None:
    # Positive control: the guard must not short-circuit a scoped call, or the
    # "no query ran" assertion above would hold for the wrong reason.
    _, session = _run(lambda s: call(s, organization_id="org-1"))

    assert session.queries, f"{method} skipped a scoped query"
    assert all(p.get("organization_id") == "org-1" for p in session.params)


def test_unscoped_strict_get_relationships_raises() -> None:
    # strict callers (entity merge) would read [] as "no edges" and then
    # DETACH DELETE the duplicate, so an unscoped strict read must raise.
    with pytest.raises(RelationshipScopeError):
        _run(lambda s: s.get_relationships("e-1", strict=True))


# --- the write: refused before any query --------------------------------------


def _rel_request(organization_id: Optional[str] = None) -> CreateRelationshipRequest:
    return CreateRelationshipRequest(
        source_entity_id="e-1",
        target_entity_id="e-of-another-tenant",
        relationship_type=RelationshipType.RELATED_TO,
        context=None,
        source_document_id=None,
        organization_id=organization_id,
    )


def test_unscoped_create_relationship_is_refused_without_writing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _FakeSession()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        with pytest.raises(RelationshipScopeError) as refused:
            KnowledgeGraphService().create_relationship(_rel_request())

    assert session.queries == [], "create_relationship ran an unscoped MERGE"
    assert "refused" in str(refused.value)
    assert _fail_closed_logged(caplog, "create_relationship")


def test_org_scoped_create_relationship_scopes_both_endpoints() -> None:
    session = _FakeSession()
    with patch.object(KnowledgeGraphService, "get_session", return_value=session):
        # The fake graph has no in-scope endpoint pair, so the scoped MERGE
        # matches nothing and reports it; what matters is that it ran scoped.
        with pytest.raises(RelationshipScopeError, match="no in-scope"):
            KnowledgeGraphService().create_relationship(_rel_request("org-1"))

    assert len(session.queries) == 1
    assert "source.organization_id = $organization_id" in session.queries[0]
    assert "target.organization_id = $organization_id" in session.queries[0]
    assert session.params[0]["organization_id"] == "org-1"
