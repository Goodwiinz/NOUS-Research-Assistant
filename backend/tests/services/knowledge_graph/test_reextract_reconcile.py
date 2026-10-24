"""Re-extraction reconciles a document's graph membership (KN2 follow-up).

KN2 (#2047) gave every :Entity a per-document reference list
``e.document_ids`` that writes append to and ``delete_document_graph``
subtracts from. Re-extraction was append-only: when a document was
re-extracted (``POST /documents/{id}/extract-entities``, the LLM
``kg_extract_entities_job``, the reconciler's ``repair_document_graph``) and
no longer emitted entity X, the document stayed in X's ``document_ids`` and
its per-document X edges stayed in the graph. That stale membership kept X
alive and, when the document had created X, visible in the document's scoped
graph (scoped reads key on the ``source_document_id`` creator stamp).

The fix is a *diff*, not a replace (owner decision, 2026-10-24):
``reconcile_document_graph`` runs after a clean re-extraction and, in one
transaction,

1. deletes this document's edges that the new extraction did not emit
   (keyed ``[source.id, target.id, r.type]``), then
2. strips the document only from member entities it no longer emits (keyed
   ``[e.type, e.canonical_key]``), re-pointing the creator stamp and deleting
   orphans with exactly the guards ``delete_document_graph`` uses.

Entities the extraction still emits are never touched, so their ids and
creator stamps stay stable (a replace would re-point the stamp of shared
nodes to another document and never restore it).

Mutation verification (docs/engineering/testing.md), from ``backend/``::

    python -m pytest -q -p no:cacheprovider \\
        tests/services/knowledge_graph/test_reextract_reconcile.py

- Drop ``AND NOT [e.type, e.canonical_key] IN $kept_entities`` from the
  reconcile node query → the diff test fails (it would strip every member).
- Drop the kept-edge predicate → the edge diff test fails.
- Remove the ``reconcile_document_graph`` call from any of the three call
  sites → that site's test fails; remove an error guard → its skip test fails.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from src.models.graph import (
    CreateEntityRequest,
    CreateRelationshipRequest,
    EntityType,
    RelationshipType,
)
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit

GET_SESSION = (
    "src.services.knowledge_graph.knowledge_graph_service."
    "KnowledgeGraphService.get_session"
)

MEMBER_IDS = (
    "coalesce(e.document_ids, CASE WHEN e.source_document_id IS NULL "
    "THEN [] ELSE [e.source_document_id] END)"
)


def _tx_session(rel_count: int = 0, node_count: int = 0) -> Tuple[Any, Any]:
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


def _entity(name: str, etype: EntityType = EntityType.CONCEPT) -> CreateEntityRequest:
    return CreateEntityRequest(
        name=name,
        entity_type=etype,
        organization_id="org-A",
        source_document_id="doc-A",
        position=None,
        context=None,
    )


def _edge(src: str, tgt: str) -> CreateRelationshipRequest:
    return CreateRelationshipRequest(
        source_entity_id=src,
        target_entity_id=tgt,
        relationship_type=RelationshipType.PART_OF,
        source_document_id="doc-A",
        organization_id="org-A",
        context=None,
    )


def _queries(tx: Any) -> Tuple[str, str]:
    first, second = (cast(str, call.args[0]) for call in tx.run.call_args_list)
    return first, second


def _params(tx: Any, index: int) -> Dict[str, Any]:
    return cast(Dict[str, Any], tx.run.call_args_list[index].args[1])


# --------------------------------------------------------------------------
# Service: reconcile_document_graph
# --------------------------------------------------------------------------


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_strips_document_only_from_entities_it_no_longer_emits(
    mock_session: MagicMock,
) -> None:
    """THE stale-membership case: doc A first named BERT and GPT; the
    re-extraction emits only BERT. A must leave GPT's membership (and GPT
    dies if nothing else holds it) while BERT is not touched at all."""
    ctx, tx = _tx_session(rel_count=1, node_count=1)
    mock_session.return_value = ctx

    result = KnowledgeGraphService().reconcile_document_graph(
        "doc-A",
        "org-A",
        entities=[_entity("  BERT "), _entity("bert"), _entity("Transformer")],
        relationships=[],
    )

    assert result == (1, 1)
    _, node_query = _queries(tx)
    params = _params(tx, 1)
    assert f"$source_document_id IN {MEMBER_IDS}" in node_query
    assert "AND NOT [e.type, e.canonical_key] IN $kept_entities" in node_query
    # Same canonical key the MERGE uses (strip + lower), de-duplicated.
    assert params["kept_entities"] == [["CONCEPT", "bert"], ["CONCEPT", "transformer"]]
    assert params["source_document_id"] == "doc-A"
    # Kept entities are excluded BEFORE the strip/re-point SET.
    assert node_query.index("$kept_entities") < node_query.index("SET e.document_ids")


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_uses_the_delete_guards_for_dropped_entities(
    mock_session: MagicMock,
) -> None:
    """A dropped entity is handled exactly like a document delete: re-point
    the creator stamp to a remaining member, delete only when no document,
    a non-null stamp and no edges remain. Plain DELETE, never DETACH."""
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().reconcile_document_graph(
        "doc-A", "org-A", entities=[_entity("BERT")], relationships=[]
    )

    _, node_query = _queries(tx)
    assert (
        "THEN remaining_document_ids[0]\n"
        "                            ELSE e.source_document_id END"
    ) in node_query
    assert (
        "WHERE size(remaining_document_ids) = 0\n"
        "                      AND e.source_document_id IS NOT NULL\n"
        "                      AND COUNT { (e)--() } = 0"
    ) in node_query
    assert "DETACH" not in node_query


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_deletes_only_this_documents_unemitted_edges(
    mock_session: MagicMock,
) -> None:
    """Per-document edges the re-extraction no longer emits are removed
    (they would otherwise hold dropped nodes alive via the orphan guard);
    edges it did emit, and other documents' edges, are kept."""
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().reconcile_document_graph(
        "doc-A",
        "org-A",
        entities=[_entity("BERT"), _entity("Transformer")],
        relationships=[_edge("n1", "n2")],
    )

    rel_query, _ = _queries(tx)
    assert (
        "MATCH (source)-[r:RELATED_TO {source_document_id: $source_document_id}]"
        "->(target)"
    ) in rel_query
    assert "NOT [source.id, target.id, r.type] IN $kept_relationships" in rel_query
    assert "(r.organization_id = $organization_id" in rel_query
    assert "DELETE r" in rel_query and "DETACH" not in rel_query
    assert _params(tx, 0)["kept_relationships"] == [["n1", "n2", "PART_OF"]]


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_edges_before_nodes_in_one_transaction(
    mock_session: MagicMock,
) -> None:
    """The orphan guard must observe the stale-edge deletes, and a failure
    must not strand a half-reconciled graph: one transaction, edges first."""
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().reconcile_document_graph(
        "doc-A", "org-A", entities=[_entity("BERT")], relationships=[]
    )

    ctx.begin_transaction.assert_called_once()
    ctx.run.assert_not_called()
    rel_query, node_query = _queries(tx)
    assert "DELETE r" in rel_query and "kept_entities" not in rel_query
    assert "DELETE e" in node_query
    # No emitted edges → every one of this doc's edges is stale.
    assert _params(tx, 0)["kept_relationships"] == []


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_without_org_drops_org_predicates(mock_session: MagicMock) -> None:
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().reconcile_document_graph(
        "doc-A", None, entities=[_entity("BERT")], relationships=[]
    )

    rel_query, node_query = _queries(tx)
    assert "organization_id" not in rel_query and "organization_id" not in node_query
    assert "WHERE NOT [source.id, target.id, r.type] IN $kept_relationships" in (
        rel_query
    )
    assert "organization_id" not in _params(tx, 1)


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_with_no_emitted_entities_is_a_noop(
    mock_session: MagicMock,
) -> None:
    """An empty extraction is far more likely an extractor problem than a
    document that truly names nothing; never strip the whole document on it."""
    assert KnowledgeGraphService().reconcile_document_graph(
        "doc-A", "org-A", entities=[_entity("   ")], relationships=[]
    ) == (0, 0)
    mock_session.assert_not_called()


@pytest.mark.unit
@patch(GET_SESSION)
def test_document_delete_queries_are_unchanged(mock_session: MagicMock) -> None:
    """Sharing the query builder must not leak the reconcile predicates into
    a plain document delete (which removes ALL of the doc's membership)."""
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-A", "org-A")

    rel_query, node_query = _queries(tx)
    assert "kept_" not in rel_query + node_query
    assert "MATCH ()-[r:RELATED_TO {source_document_id: $source_document_id}]->()" in (
        rel_query
    )
    assert "kept_entities" not in _params(tx, 1)


# --------------------------------------------------------------------------
# Call sites: every full re-extraction reconciles after a clean write
# --------------------------------------------------------------------------


def _batch_result(created=(), errors=(), relationships=()) -> SimpleNamespace:
    return SimpleNamespace(
        created_entities=list(created),
        created_relationships=list(relationships),
        errors=list(errors),
        processing_time=0.0,
    )


def _api_extract(monkeypatch: pytest.MonkeyPatch, kg: MagicMock) -> None:
    from src.api.search import knowledge_graph as api

    doc = SimpleNamespace(id="doc-A", content_text="BERT uses attention.", content="")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = doc
    ent = SimpleNamespace(
        name="BERT", entity_type="CONCEPT", confidence=0.9, properties={}
    )
    extractor = MagicMock()
    extractor.extract_entities_and_relationships_from_text.return_value = ([ent], [])
    monkeypatch.setattr(
        api, "EntityExtractionService", MagicMock(return_value=extractor)
    )
    monkeypatch.setattr(
        api, "_map_processing_entity_type_to_graph", lambda t: EntityType.CONCEPT
    )
    monkeypatch.setattr(api, "knowledge_graph_service", kg)
    caller = SimpleNamespace(id="u1", organization_id="org-A")
    api.extract_entities_from_document(
        document_id="doc-A", current_user=cast(Any, caller), db=db
    )


@pytest.mark.unit
def test_extract_endpoint_reconciles_after_clean_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", entity_type=EntityType.CONCEPT, name="BERT")]
    )
    _api_extract(monkeypatch, kg)

    kg.reconcile_document_graph.assert_called_once()
    args, kwargs = kg.reconcile_document_graph.call_args
    assert args == ("doc-A", "org-A")
    assert [e.name for e in kwargs["entities"]] == ["BERT"]
    assert kwargs["relationships"] == []


@pytest.mark.unit
def test_extract_endpoint_skips_reconcile_on_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write leaves relationship endpoints unresolved; reconciling
    then would delete edges the extraction actually produced."""
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(errors=[{"error": "x"}])
    _api_extract(monkeypatch, kg)
    kg.reconcile_document_graph.assert_not_called()


@pytest.mark.unit
def test_extract_endpoint_survives_reconcile_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extraction itself succeeded; a failed cleanup must not 500 it."""
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result()
    kg.reconcile_document_graph.side_effect = ConnectionError("neo4j down")
    _api_extract(monkeypatch, kg)  # does not raise
    kg.reconcile_document_graph.assert_called_once()


def _run_llm_job(
    monkeypatch: pytest.MonkeyPatch, kg: MagicMock, *, extraction_error=None
) -> Dict[str, Any]:
    from src.models.processing import JobStatus
    from src.tasks import processing_tasks as pt

    doc_id = str(uuid.uuid4())
    job = MagicMock()
    job.status = JobStatus.PENDING
    job.parameters = {"document_id": doc_id}
    captured: Dict[str, Any] = {"doc_id": doc_id}
    job.complete_job.side_effect = lambda result: captured.update(result=result)
    document = SimpleNamespace(
        id=doc_id, organization_id="org-A", content_text="BERT uses attention."
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job, document]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    extraction = SimpleNamespace(
        entities=[
            SimpleNamespace(
                name="BERT",
                type="concept",
                confidence=0.9,
                description=None,
                aliases=[],
            )
        ],
        relationships=[],
        error=extraction_error,
    )
    service = MagicMock()
    service.extract_entities = MagicMock(return_value=extraction)
    monkeypatch.setattr(pt, "LLMEntityExtractionService", lambda: service)
    monkeypatch.setattr(pt.asyncio, "run", lambda value: value)
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)
    pt.kg_extract_entities_job.apply(args=("job-1",))
    return captured


@pytest.mark.unit
def test_llm_job_reconciles_each_document(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", name="BERT")]
    )
    captured = _run_llm_job(monkeypatch, kg)

    kg.reconcile_document_graph.assert_called_once()
    args, kwargs = kg.reconcile_document_graph.call_args
    assert args == (captured["doc_id"], "org-A")
    assert [e.name for e in kwargs["entities"]] == ["BERT"]


@pytest.mark.unit
def test_llm_job_skips_reconcile_on_partial_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out / partial LLM extraction must not strip the entities it
    simply did not get to (owner decision (a))."""
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", name="BERT")]
    )
    _run_llm_job(monkeypatch, kg, extraction_error="timeout after 300s")
    kg.reconcile_document_graph.assert_not_called()


@pytest.mark.unit
def test_llm_job_counts_reconcile_failure_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", name="BERT")]
    )
    kg.reconcile_document_graph.side_effect = ConnectionError("neo4j down")
    captured = _run_llm_job(monkeypatch, kg)
    assert captured["result"]["errors"] == 1
    assert captured["result"]["entities_created"] == 1


def _repair(kg: MagicMock):
    from src.services.knowledge_graph.repair import repair_document_graph

    doc = SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), content_text="BERT."
    )
    ent = SimpleNamespace(
        name="BERT",
        entity_type=SimpleNamespace(value="concept"),
        confidence=0.9,
        extraction_method=SimpleNamespace(value="spacy"),
        properties={},
    )
    extractor = MagicMock()
    extractor.extract_entities_and_relationships_from_text.return_value = ([ent], [])
    return doc, repair_document_graph(
        cast(Any, doc), extraction_service=extractor, kg_service=kg
    )


@pytest.mark.unit
def test_repair_reconciles_after_clean_write() -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", name="BERT")]
    )
    doc, outcome = _repair(kg)

    assert outcome.ok is True
    args, kwargs = kg.reconcile_document_graph.call_args
    assert args == (str(doc.id), str(doc.organization_id))
    assert [e.name for e in kwargs["entities"]] == ["BERT"]


@pytest.mark.unit
def test_repair_skips_reconcile_on_batch_errors() -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(errors=[{"error": "x"}])
    _, outcome = _repair(kg)
    assert outcome.ok is False
    kg.reconcile_document_graph.assert_not_called()


@pytest.mark.unit
def test_repair_reports_reconcile_failure_and_stays_reconcilable() -> None:
    kg = MagicMock()
    kg.create_entities_batch.return_value = _batch_result(
        created=[SimpleNamespace(id="n1", name="BERT")]
    )
    kg.reconcile_document_graph.side_effect = ConnectionError("neo4j down")
    _, outcome = _repair(kg)
    assert outcome.ok is False
    assert outcome.error_message == "neo4j down"
    assert outcome.entities_created == 1
