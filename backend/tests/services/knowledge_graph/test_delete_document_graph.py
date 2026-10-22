"""Cypher-level tests for KnowledgeGraphService.delete_document_graph (audit D2).

:Entity nodes are SHARED across documents: both creation paths MERGE on
(canonical_key, type, organization_id) — no document component — and
`e.source_document_id` is set only ON CREATE, so it records merely the FIRST
creator document. A node-anchored DETACH DELETE would therefore destroy other
documents' relationships whenever the deleted doc happened to be the creator.

The method must instead delete in two steps inside one transaction:

1. DELETE this document's RELATIONSHIPS (`r.source_document_id` — a true
   per-document MERGE-key property on RELATED_TO edges).
2. Remove the doc from every member node's `document_ids` reference count
   (KN2; legacy nodes without the property count as `[source_document_id]`),
   then DELETE only nodes left with NO remaining document AND no
   relationships — the `size(remaining_document_ids) = 0` membership guard
   keeps nodes other docs still mention alive, and the `COUNT { (e)--() } = 0`
   guard keeps nodes other docs' edges still touch alive.

Org scoping is defense-in-depth (property-based graph tenancy): AND'd with the
doc anchor, never OR'd (an OR would widen the delete to the whole org's graph),
with the legacy NULL-org escape so pre-backfill data is still reaped.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

GET_SESSION = (
    "src.services.knowledge_graph.knowledge_graph_service."
    "KnowledgeGraphService.get_session"
)

ORPHAN_GUARD = "COUNT { (e)--() } = 0"
MEMBERSHIP_GUARD = "size(remaining_document_ids) = 0"
# Effective per-document membership: document_ids, or the legacy creator stamp.
MEMBER_IDS = (
    "coalesce(e.document_ids, CASE WHEN e.source_document_id IS NULL "
    "THEN [] ELSE [e.source_document_id] END)"
)


def _session(rel_count=0, node_count=0):
    """A patched `get_session()` whose transaction reports per-step counts."""

    def _run(query, params=None):
        result = MagicMock()
        count = rel_count if "RELATED_TO" in query else node_count
        result.single.return_value = {"deleted_count": count}
        return result

    tx = MagicMock()
    tx.run.side_effect = _run
    tx.__enter__ = MagicMock(return_value=tx)
    tx.__exit__ = MagicMock(return_value=False)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.begin_transaction.return_value = tx
    return ctx, tx


def _queries(tx):
    return [call.args[0] for call in tx.run.call_args_list]


def _params(tx):
    return [call.args[1] for call in tx.run.call_args_list]


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_two_step_relationships_then_orphan_nodes(
    mock_session,
):
    """Step 1 deletes the doc's relationships; step 2 deletes only orphaned
    nodes — in that order, in one transaction."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session(rel_count=5, node_count=2)
    mock_session.return_value = ctx

    doc_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    deleted = KnowledgeGraphService().delete_document_graph(doc_id, org_id)

    assert deleted == (5, 2)
    queries = _queries(tx)
    assert len(queries) == 2

    rel_query, node_query = queries
    # Step 1: relationship delete anchored on the edge's own per-document
    # provenance property (the MERGE-key r.source_document_id).
    assert "[r:RELATED_TO {source_document_id: $source_document_id}]" in rel_query
    assert "DELETE r" in rel_query
    # Edge org defense-in-depth, with legacy NULL escape.
    assert "r.organization_id = $organization_id" in rel_query
    assert "r.organization_id IS NULL" in rel_query

    # Step 2: node step anchored on per-document MEMBERSHIP (document_ids, or
    # the legacy creator stamp), not on the creator stamp alone...
    assert "MATCH (e:Entity)" in node_query
    assert f"$source_document_id IN {MEMBER_IDS}" in node_query
    # ...guarded so only nodes with no remaining document AND no edges die.
    # Both are load-bearing: without the membership guard, deleting the
    # creator doc destroys nodes other docs still mention (KN2); without the
    # orphan guard it destroys nodes still wired to other docs' edges.
    assert MEMBERSHIP_GUARD in node_query
    assert ORPHAN_GUARD in node_query
    assert "e.organization_id = $organization_id" in node_query
    assert "e.organization_id IS NULL" in node_query

    for params in _params(tx):
        assert params["source_document_id"] == doc_id
        assert params["organization_id"] == org_id


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_never_detach_deletes_nodes(mock_session):
    """Regression for the HIGH audit finding: a node-anchored DETACH DELETE
    destroys other documents' relationships on shared entity nodes (entity
    MERGE identity has no document component; e.source_document_id = first
    creator only). The node step must be a plain DELETE behind the orphan
    guard so a still-connected node makes Neo4j raise instead of silently
    losing cross-document graph data."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-1", "org-1")

    for query in _queries(tx):
        assert "DETACH" not in query
    node_query = _queries(tx)[1]
    assert ORPHAN_GUARD in node_query


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_shared_node_semantics_documented(mock_session):
    """Shared-node scenario (docs A + B both name entity E, A created it):

    - A's relationships match step 1 (r.source_document_id = A) → deleted.
    - E.document_ids = [A, B] → step 2 drops A, re-points E.source_document_id
      to B, and keeps E (membership guard) regardless of edge count (KN2).
    - E.document_ids = [A] but E still has B's edges → COUNT { (e)--() } != 0
      → step 2 keeps it.
    - E.document_ids = [A] and E's only edges were A's → orphaned after step 1
      → step 2 deletes it.
    - A node whose membership never contained A does not match step 2's
      anchor, so it survives regardless of degree.

    Mock-level: assert the query shape that encodes exactly these semantics, so
    the orphan guard cannot silently regress.
    """
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session(rel_count=3, node_count=1)
    mock_session.return_value = ctx
    doc_a = str(uuid.uuid4())
    KnowledgeGraphService().delete_document_graph(doc_a, "org-1")

    rel_query, node_query = _queries(tx)
    # Relationships die by THEIR OWN doc stamp, not their endpoints' stamps.
    assert "r:RELATED_TO {source_document_id: $source_document_id}" in rel_query
    assert "e.source_document_id" not in rel_query
    # Nodes die only when (a) this doc was a member, (b) no document remains
    # AND (c) nothing references them any more — all present, AND'd (no OR).
    assert f"$source_document_id IN {MEMBER_IDS}" in node_query
    assert MEMBERSHIP_GUARD in node_query
    assert ORPHAN_GUARD in node_query
    where = node_query.split("WHERE", 1)[1].split("DELETE", 1)[0]
    assert "AND" in where and ORPHAN_GUARD in where
    final_where = where.rsplit("WHERE", 1)[1]
    assert " OR " not in final_where
    assert MEMBERSHIP_GUARD in final_where and ORPHAN_GUARD in final_where
    assert "AND e.source_document_id IS NOT NULL" in final_where


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_runs_in_single_transaction(mock_session):
    """Step 2's orphan check must observe step 1's deletions, and a failure
    between steps must not strand a half-cleaned graph → both queries run on
    one explicit transaction, not autocommit session.run."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-1", "org-1")

    ctx.begin_transaction.assert_called_once()
    assert tx.run.call_count == 2
    ctx.run.assert_not_called()


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_stringifies_ids(mock_session):
    """Non-string ids (e.g. UUID objects) are coerced to match stored string
    properties."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session()
    mock_session.return_value = ctx

    doc_id = uuid.uuid4()
    org_id = uuid.uuid4()
    KnowledgeGraphService().delete_document_graph(doc_id, org_id)

    for params in _params(tx):
        assert params["source_document_id"] == str(doc_id)
        assert params["organization_id"] == str(org_id)


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_omits_org_predicate_when_org_none(mock_session):
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session()
    mock_session.return_value = ctx

    doc_id = str(uuid.uuid4())
    deleted = KnowledgeGraphService().delete_document_graph(doc_id, None)

    assert deleted == (0, 0)
    for query, params in zip(_queries(tx), _params(tx)):
        assert "organization_id" not in query  # no org predicate emitted
        assert "organization_id" not in params  # and no org param bound
        assert params["source_document_id"] == doc_id
    # The orphan guard is unconditional — never dropped with the org filter.
    assert ORPHAN_GUARD in _queries(tx)[1]


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_does_not_widen_to_whole_org(mock_session):
    """Regression: both steps must AND the doc anchor with org, never OR them
    (an OR would delete the entire org's graph). Step 1's doc anchor stays a
    MATCH pattern property; step 2's is the membership predicate AND'd after
    the org predicate; org lives only in the WHERE."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    ctx, tx = _session(rel_count=1, node_count=1)
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-1", "org-1")

    rel_query, node_query = _queries(tx)
    assert "source_document_id: $source_document_id" in rel_query
    org_pred, _, anchor = node_query.partition("\n                      AND ")
    assert "e.organization_id = $organization_id" in org_pred
    assert anchor.startswith(f"$source_document_id IN {MEMBER_IDS}")
    for query in (rel_query, node_query):
        # Must NOT reuse the read-scope helper's OR'd source_document_id IN-list.
        assert "IN $source_document_ids" not in query


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_document_graph_zero_when_no_record(mock_session):
    """A missing aggregation row is treated as zero deletions, not a crash."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    result = MagicMock()
    result.single.return_value = None
    tx = MagicMock()
    tx.run.return_value = result
    tx.__enter__ = MagicMock(return_value=tx)
    tx.__exit__ = MagicMock(return_value=False)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.begin_transaction.return_value = tx
    mock_session.return_value = ctx

    assert KnowledgeGraphService().delete_document_graph("doc-1", "org-1") == (0, 0)
