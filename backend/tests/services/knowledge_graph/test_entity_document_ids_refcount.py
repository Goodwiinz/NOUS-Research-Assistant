"""Cypher-level tests for the KN2 per-document entity reference count.

:Entity nodes are shared by every document in an org that names the entity
(the MERGE key is (canonical_key, type, organization_id) — no document
component), and ``e.source_document_id`` is set only ON CREATE, so it records
merely the FIRST creator. The spaCy ingestion fan-out writes nodes but never
edges, so ``delete_document_graph``'s old creator-anchored step 2 deleted a
node that OTHER live documents still mentioned (docs A and B both name "BERT",
A ingested first, delete A → B silently loses the entity while its
``neo4j_index_status`` stays ``completed``, so the reconciler never repairs it).

The fix records membership on the node:

- every entity write path (``create_entity``, ``_create_entity_in_transaction``,
  ``_batch_merge_entities``) idempotently appends the writing document to
  ``e.document_ids`` (null doc id → list unchanged, no duplicates);
- ``delete_document_graph`` removes only the deleted doc id, re-points
  ``source_document_id`` when the creator stamp was the deleted doc and other
  members remain, and deletes the node only when NO document remains AND it
  has no relationships;
- legacy nodes with no ``document_ids`` property are read as
  ``[source_document_id]`` (``[]`` when that is null), i.e. exactly the
  pre-KN2 behaviour — nothing already in Neo4j is deleted more aggressively.

Mutation verification (docs/engineering/testing.md), focused command from
``backend/``::

    python -m pytest -q -p no:cacheprovider \\
        tests/services/knowledge_graph/test_entity_document_ids_refcount.py \\
        tests/services/knowledge_graph/test_delete_document_graph.py

- Membership guard: in ``delete_document_graph``'s ``node_query`` replace
  ``WHERE size(remaining_document_ids) = 0 AND COUNT { (e)--() } = 0`` with
  ``WHERE COUNT { (e)--() } = 0`` → ``test_delete_keeps_nodes_with_remaining_
  documents`` (and 3 more) fail.
- Idempotent append: in ``_append_entity_document_id_clause`` replace the
  CASE with ``SET {alias}.document_ids = existing_document_ids + [{doc_expr}]``
  → the six write-path tests fail.
- Whole fix reverted (HEAD service file): all 12 tests here fail.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any, Dict, Iterator, List, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from src.models.graph import BatchEntityRequest, CreateEntityRequest, EntityType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

GET_SESSION = (
    "src.services.knowledge_graph.knowledge_graph_service."
    "KnowledgeGraphService.get_session"
)


def _member_ids(alias: str) -> str:
    """Effective membership list: document_ids, or the legacy creator stamp."""
    return (
        f"coalesce({alias}.document_ids, CASE WHEN {alias}.source_document_id "
        f"IS NULL THEN [] ELSE [{alias}.source_document_id] END)"
    )


def _append_clause(alias: str, doc_expr: str) -> str:
    """The idempotent append every write path must emit."""
    return (
        f"SET {alias}.document_ids = CASE\n"
        f"    WHEN {doc_expr} IS NULL OR {doc_expr} IN existing_document_ids\n"
        f"        THEN existing_document_ids\n"
        f"    ELSE existing_document_ids + [{doc_expr}] END"
    )


# --------------------------------------------------------------------------
# Session / transaction doubles
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, record: Dict[str, Any]) -> None:
        self._record = record

    def single(self) -> Dict[str, Any]:
        return self._record

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter([self._record])


class _Capture:
    """Records every (query, params) pair run on a session or transaction."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def run(self, query: str, params: Dict[str, Any]) -> _Result:
        self.calls.append((query, params))
        return _Result(
            {
                "resolved_id": "e1",
                "created_at": 1,
                "updated_at": 1,
                "idx": 0,
                "id": "e1",
            }
        )

    @property
    def query(self) -> str:
        return self.calls[-1][0]

    @property
    def params(self) -> Dict[str, Any]:
        return self.calls[-1][1]


def _svc_with_session(capture: _Capture) -> KnowledgeGraphService:
    svc = KnowledgeGraphService()

    @contextlib.contextmanager
    def _fake_get_session(database: str = "neo4j") -> Iterator[_Capture]:
        yield capture

    svc.get_session = _fake_get_session  # type: ignore[method-assign]
    return svc


def _delete_session(rel_count: int = 0, node_count: int = 0) -> Tuple[Any, Any]:
    """A patched ``get_session()`` whose transaction reports per-step counts."""

    def _run(query: str, params: Dict[str, Any]) -> Any:
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


def _node_query(tx: Any) -> str:
    return cast(str, tx.run.call_args_list[1].args[0])


def _node_params(tx: Any) -> Dict[str, Any]:
    return cast(Dict[str, Any], tx.run.call_args_list[1].args[1])


def _request(doc_id: str | None, org: str = "org-A") -> CreateEntityRequest:
    return CreateEntityRequest(
        name="BERT",
        entity_type=EntityType.CONCEPT,
        organization_id=org,
        source_document_id=doc_id,
    )


# --------------------------------------------------------------------------
# Write paths: every MERGE appends the writing doc idempotently
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_create_entity_appends_document_id_idempotently() -> None:
    """The single-entity MERGE (ingestion fan-out via create_entity_node, the
    API create endpoint, arxiv_local, multimodal) must append the writing doc
    to document_ids on create AND on match, never duplicating it."""
    capture = _Capture()
    doc_id = str(uuid.uuid4())
    _svc_with_session(capture).create_entity(_request(doc_id))

    query = capture.query
    # Creator stamp unchanged: still ON CREATE only, reads keep working.
    on_create = query.split("ON CREATE SET", 1)[1].split("ON MATCH SET", 1)[0]
    assert "e.source_document_id = $source_document_id" in on_create
    on_match = query.split("ON MATCH SET", 1)[1].split("WITH e,", 1)[0]
    assert "source_document_id" not in on_match
    # Membership is appended AFTER the MERGE so it runs for both branches.
    assert f"WITH e, {_member_ids('e')} AS existing_document_ids" in query
    assert _append_clause("e", "$source_document_id") in query
    assert query.index("ON MATCH SET") < query.index("SET e.document_ids")
    assert query.index("SET e.document_ids") < query.index("RETURN e.id")
    assert capture.params["source_document_id"] == doc_id
    # Org identity untouched by the membership change.
    assert "organization_id: $org_key" in query
    assert capture.params["org_key"] == "org-A"


@pytest.mark.unit
def test_create_entity_null_document_id_leaves_list_unchanged() -> None:
    """A write with no document (e.g. API create without source_document_id)
    must not append null — Cypher cannot store [null] — nor clear the list."""
    capture = _Capture()
    _svc_with_session(capture).create_entity(_request(None))

    assert capture.params["source_document_id"] is None
    assert (
        "WHEN $source_document_id IS NULL OR $source_document_id IN "
        "existing_document_ids\n        THEN existing_document_ids"
    ) in capture.query


@pytest.mark.unit
def test_create_entity_node_adapter_threads_document_id() -> None:
    """The spaCy ingestion fan-out (processing_tasks) goes through the
    create_entity_node adapter; its document_id must reach the membership
    param, otherwise ingested docs never become members."""
    capture = _Capture()
    doc_id = str(uuid.uuid4())
    svc = _svc_with_session(capture)
    assert (
        svc.create_entity_node(
            entity_text="BERT",
            entity_type="CONCEPT",
            document_id=doc_id,
            organization_id="org-A",
        )
        == "e1"
    )
    assert capture.params["source_document_id"] == doc_id
    assert _append_clause("e", "$source_document_id") in capture.query


@pytest.mark.unit
def test_create_entity_in_transaction_appends_document_id() -> None:
    """The per-entity fallback used by create_entities_batch (project-add
    kg_extract_entities_job, graph repair, API batch) when the UNWIND path
    fails must append membership exactly like create_entity."""
    capture = _Capture()
    doc_id = str(uuid.uuid4())
    KnowledgeGraphService()._create_entity_in_transaction(capture, _request(doc_id))

    query = capture.query
    assert f"WITH e, {_member_ids('e')} AS existing_document_ids" in query
    assert _append_clause("e", "$source_document_id") in query
    assert query.index("ON MATCH SET") < query.index("SET e.document_ids")
    assert capture.params["source_document_id"] == doc_id
    assert "organization_id: $org_key" in query


@pytest.mark.unit
def test_batch_merge_appends_document_id_per_row() -> None:
    """The apoc UNWIND batch (create_entities_batch's primary path) must
    append each row's own document after apoc.merge.node, carrying `row`
    through the WITH so the RETURN still has it."""
    capture = _Capture()
    doc_id = str(uuid.uuid4())
    out = KnowledgeGraphService()._batch_merge_entities(capture, [_request(doc_id)])

    assert out is not None and out[0].source_document_id == doc_id
    query = capture.query
    assert "apoc.merge.node" in query
    assert f"WITH row, node, {_member_ids('node')} AS existing_document_ids" in query
    assert _append_clause("node", "row.props.source_document_id") in query
    assert query.index("YIELD node") < query.index("SET node.document_ids")
    assert query.index("SET node.document_ids") < query.index("RETURN row.idx")
    assert capture.params["rows"][0]["props"]["source_document_id"] == doc_id
    # Identity map (tenant isolation) unchanged.
    assert "organization_id: row.org_key" in query


@pytest.mark.unit
def test_create_entities_batch_stamps_every_entity_with_its_document() -> None:
    """End-to-end through create_entities_batch: the project-add job and the
    repair job build one request per entity with source_document_id =
    str(document.id); every row must carry it into the membership append."""
    ctx, tx = _delete_session()

    def _run(query: str, params: Dict[str, Any]) -> Any:
        return _Result({"idx": 0, "id": "e1"})

    tx.run.side_effect = _run
    doc_id = str(uuid.uuid4())
    with patch(GET_SESSION, return_value=ctx):
        response = KnowledgeGraphService().create_entities_batch(
            BatchEntityRequest(
                entities=[_request(doc_id), _request(doc_id)],
                relationships=[],
                document_id=doc_id,
            )
        )

    assert not response.errors
    query, params = tx.run.call_args_list[0].args
    assert _append_clause("node", "row.props.source_document_id") in query
    assert [row["props"]["source_document_id"] for row in params["rows"]] == [
        doc_id,
        doc_id,
    ]


# --------------------------------------------------------------------------
# delete_document_graph: subtract membership, delete only when nothing remains
# --------------------------------------------------------------------------


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_removes_only_the_deleted_doc_from_membership(
    mock_session: MagicMock,
) -> None:
    """Step 2 anchors on membership (not the creator stamp), strips exactly
    the deleted id, and writes the remainder back."""
    ctx, tx = _delete_session(rel_count=2, node_count=1)
    mock_session.return_value = ctx
    doc_id = str(uuid.uuid4())
    assert KnowledgeGraphService().delete_document_graph(doc_id, "org-1") == (2, 1)

    query = _node_query(tx)
    assert "MATCH (e:Entity)" in query
    assert f"$source_document_id IN {_member_ids('e')}" in query
    assert (
        f"[d IN {_member_ids('e')} WHERE d <> $source_document_id] "
        "AS remaining_document_ids"
    ) in query
    assert "SET e.document_ids = remaining_document_ids" in query
    assert _node_params(tx)["source_document_id"] == doc_id
    # The old creator-only anchor is gone: a node A created but B still
    # mentions no longer matches by stamp alone.
    assert "(e:Entity {source_document_id: $source_document_id})" not in query


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_keeps_nodes_with_remaining_documents(mock_session: MagicMock) -> None:
    """THE KN2 guard: a node is deleted only when no document remains AND it
    has no relationships. Shared node E with document_ids [A, B]: deleting A
    leaves [B] → survives regardless of edge count."""
    ctx, tx = _delete_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-A", "org-1")

    query = _node_query(tx)
    tail = query.split("WITH e, remaining_document_ids", 1)[1]
    assert "WHERE size(remaining_document_ids) = 0 AND COUNT { (e)--() } = 0" in tail
    assert tail.index("WHERE size(remaining_document_ids) = 0") < tail.index("DELETE e")
    # Plain DELETE behind both guards, never DETACH.
    assert "DETACH" not in query
    assert "RETURN count(e) as deleted_count" in query


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_repoints_creator_stamp_to_a_remaining_document(
    mock_session: MagicMock,
) -> None:
    """When the deleted doc was the node's creator and other members remain,
    source_document_id is re-pointed so doc-scoped reads keep finding the
    node; otherwise it is left alone."""
    ctx, tx = _delete_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-A", "org-1")

    query = _node_query(tx)
    assert (
        "e.source_document_id = CASE\n"
        "                            WHEN e.source_document_id = $source_document_id\n"
        "                                 AND size(remaining_document_ids) > 0\n"
        "                            THEN remaining_document_ids[0]\n"
        "                            ELSE e.source_document_id END"
    ) in query
    # Re-pointing happens before the delete decision, on the surviving node.
    assert query.index("e.source_document_id = CASE") < query.index("DELETE e")


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_legacy_nodes_fall_back_to_creator_stamp(
    mock_session: MagicMock,
) -> None:
    """Nodes written before document_ids existed are read as
    [source_document_id] (or [] when null) in BOTH the anchor and the
    remainder, so they match and die exactly as before: only when this doc
    created them and they are orphaned. No migration is run."""
    ctx, tx = _delete_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-A", "org-1")

    query = _node_query(tx)
    fallback = (
        "coalesce(e.document_ids, CASE WHEN e.source_document_id IS NULL "
        "THEN [] ELSE [e.source_document_id] END)"
    )
    assert query.count(fallback) == 2
    # Never a bare document_ids read that would make a legacy node unmatched
    # (silently never cleaned) or matched with a null-bearing list.
    assert "IN e.document_ids" not in query
    assert "[e.source_document_id]" in query
    assert "CALL {" not in query and "MERGE" not in query  # no backfill here


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_org_predicates_unchanged_and_anded_with_membership(
    mock_session: MagicMock,
) -> None:
    """Tenant defense-in-depth stays exactly as strict: org equality with the
    legacy NULL escape, AND'd (never OR'd) in front of the membership anchor,
    and omitted entirely (query and params) when no org is given."""
    ctx, tx = _delete_session()
    mock_session.return_value = ctx
    org_id = str(uuid.uuid4())
    KnowledgeGraphService().delete_document_graph("doc-A", org_id)

    query = _node_query(tx)
    assert (
        "WHERE\n"
        "                          (e.organization_id = $organization_id\n"
        "                        OR e.organization_id IS NULL)\n"
        "                      AND $source_document_id IN "
    ) in query
    assert _node_params(tx)["organization_id"] == org_id
    assert "IN $source_document_ids" not in query  # not the OR'd read helper
    rel_query = tx.run.call_args_list[0].args[0]
    assert "[r:RELATED_TO {source_document_id: $source_document_id}]" in rel_query
    assert "r.organization_id = $organization_id" in rel_query

    ctx2, tx2 = _delete_session()
    mock_session.return_value = ctx2
    KnowledgeGraphService().delete_document_graph("doc-A", None)
    query2 = _node_query(tx2)
    assert "organization_id" not in query2
    assert "organization_id" not in _node_params(tx2)
    assert f"WHERE $source_document_id IN {_member_ids('e')}" in query2
    assert "size(remaining_document_ids) = 0 AND COUNT { (e)--() } = 0" in query2


@pytest.mark.unit
@patch(GET_SESSION)
def test_delete_still_two_steps_in_one_transaction(mock_session: MagicMock) -> None:
    """Membership subtraction lives inside step 2, so the delete is still
    exactly two statements on one explicit transaction (relationships first,
    so the orphan guard observes their removal)."""
    ctx, tx = _delete_session(rel_count=4, node_count=0)
    mock_session.return_value = ctx
    assert KnowledgeGraphService().delete_document_graph("doc-A", "org-1") == (4, 0)

    ctx.begin_transaction.assert_called_once()
    assert tx.run.call_count == 2
    ctx.run.assert_not_called()
    first, second = (call.args[0] for call in tx.run.call_args_list)
    assert "DELETE r" in first and "document_ids" not in first
    assert "DELETE e" in second and "remaining_document_ids" in second
