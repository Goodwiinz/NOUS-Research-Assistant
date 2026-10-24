"""Re-extraction reconciles a document's graph membership per extractor.

KN2 (#2047) gave every :Entity a per-document reference list
``e.document_ids`` that writes append to and ``delete_document_graph``
subtracts from. Re-extraction was append-only: when a document was
re-extracted (``POST /documents/{id}/extract-entities``, the LLM
``kg_extract_entities_job``, the reconciler's ``repair_document_graph``) and
no longer emitted entity X, the document stayed in X's ``document_ids`` and
its per-document X edges stayed in the graph. That stale membership kept X
alive and, when the document had created X, visible in the document's scoped
graph (scoped reads key on the ``source_document_id`` creator stamp).

Owner decisions (2026-10-24): a *diff*, not a replace, and *per extractor*.
spaCy (ingestion fan-out, extract endpoint, repair) and the LLM job write
different entity sets for the same document; neither may drop the other's.

Storage: ``e.document_links`` holds ``"<extractor>:<document_id>"`` strings
next to the unchanged ``e.document_ids`` union; RELATED_TO edges (already
per-document) carry ``r.extractors``. A document stays a member while any of
its links remains. Writes without an extractor, and everything written before
links existed, read as ``unknown`` and are never reconciled away.

``reconcile_document_graph(extractor=...)`` runs after a clean re-extraction
and, in one transaction,

1. withdraws ``extractor`` from this document's edges the new extraction did
   not emit (keyed ``[source.id, target.id, r.type]``), deleting an edge only
   when no extractor asserts it any more, then
2. removes the ``extractor:document`` link from entities it no longer emits
   (keyed ``[e.type, e.canonical_key]``); the document leaves the entity only
   when no other link to it remains, and orphans die with exactly the guards
   ``delete_document_graph`` uses.

Mutation verification (docs/engineering/testing.md), from ``backend/``::

    python -m pytest -q -p no:cacheprovider \\
        tests/services/knowledge_graph/test_reextract_reconcile.py

- Drop ``AND NOT [e.type, e.canonical_key] IN $kept_entities`` → the diff
  test fails (it would strip every linked entity).
- Anchor the node reconcile on ``$source_document_id IN document_ids``
  instead of ``$document_link IN e.document_links`` → the
  keeps-the-other-extractor tests fail.
- Make the ELSE branch unconditional (drop the other-link check) → the
  stays-a-member test fails.
- Drop the kept-edge predicate, or delete edges without the
  ``size(r.extractors) = 0`` guard → the edge tests fail.
- Remove the ``reconcile_document_graph`` call or the extractor argument from
  any call site → that site's test fails; remove an error guard → its skip
  test fails.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from src.models.graph import (
    BatchEntityRequest,
    CreateEntityRequest,
    CreateRelationshipRequest,
    EntityType,
    RelationshipType,
)
from src.services.knowledge_graph.knowledge_graph_service import (
    GraphExtractor,
    KnowledgeGraphService,
)

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


def _reconcile(
    extractor: GraphExtractor,
    entities: List[CreateEntityRequest],
    relationships: List[CreateRelationshipRequest] | None = None,
    org: str | None = "org-A",
) -> Tuple[Any, Any, Tuple[int, int]]:
    ctx, tx = _tx_session(rel_count=1, node_count=1)
    with patch(GET_SESSION, return_value=ctx):
        result = KnowledgeGraphService().reconcile_document_graph(
            "doc-A",
            org,
            extractor=extractor,
            entities=entities,
            relationships=relationships or [],
        )
    return ctx, tx, result


# --------------------------------------------------------------------------
# Service: reconcile_document_graph
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_rerun_strips_its_link_only_from_entities_it_no_longer_emits() -> None:
    """THE stale-membership case: spaCy first linked doc A to BERT and GPT;
    the spaCy re-run emits only BERT. spaCy's link to GPT goes (and GPT dies
    if nothing else holds it) while BERT is not touched at all."""
    _, tx, result = _reconcile(
        GraphExtractor.SPACY,
        [_entity("  BERT "), _entity("bert"), _entity("Transformer")],
    )

    assert result == (1, 1)
    _, node_query = _queries(tx)
    params = _params(tx, 1)
    assert "$document_link IN e.document_links" in node_query
    assert "AND NOT [e.type, e.canonical_key] IN $kept_entities" in node_query
    assert params["document_link"] == "spacy:doc-A"
    # Same canonical key the MERGE uses (strip + lower), de-duplicated.
    assert params["kept_entities"] == [["CONCEPT", "bert"], ["CONCEPT", "transformer"]]
    assert params["source_document_id"] == "doc-A"
    # Kept entities are excluded BEFORE anything is SET.
    assert node_query.index("$kept_entities") < node_query.index("SET ")
    assert (
        "[l IN e.document_links WHERE l <> $document_link] "
        "AS remaining_document_links"
    ) in node_query


@pytest.mark.unit
def test_spacy_rerun_keeps_llm_only_entities() -> None:
    """An entity only the LLM job linked to doc A has document_links
    ['llm:doc-A']. A spaCy re-run that no longer emits it must not match it:
    the anchor is spaCy's own link, never document membership."""
    _, tx, _ = _reconcile(GraphExtractor.SPACY, [_entity("BERT")])

    rel_query, node_query = _queries(tx)
    match_clause = node_query.split("WITH e,", 1)[0]
    assert "$document_link IN e.document_links" in match_clause
    assert "$source_document_id IN" not in match_clause
    assert _params(tx, 1)["document_link"] == "spacy:doc-A"
    assert _params(tx, 0)["document_extractor"] == "spacy"
    assert "llm" not in repr(_params(tx, 0)) + repr(_params(tx, 1))


@pytest.mark.unit
def test_llm_rerun_keeps_spacy_entities() -> None:
    """The converse: entities the spaCy ingestion fan-out linked
    ('spacy:doc-A') survive an LLM re-run that does not emit them."""
    _, tx, _ = _reconcile(GraphExtractor.LLM, [_entity("BERT")])

    _, node_query = _queries(tx)
    match_clause = node_query.split("WITH e,", 1)[0]
    assert "$document_link IN e.document_links" in match_clause
    assert _params(tx, 1)["document_link"] == "llm:doc-A"
    assert _params(tx, 0)["document_extractor"] == "llm"
    assert "spacy" not in repr(_params(tx, 0)) + repr(_params(tx, 1))


@pytest.mark.unit
def test_document_stays_member_while_another_link_to_it_remains() -> None:
    """An entity both extractors linked to doc A (['spacy:doc-A',
    'llm:doc-A']): the spaCy re-run removes only its own link; doc A stays in
    document_ids, so the creator stamp is not re-pointed and the node is
    never deleted."""
    _, tx, _ = _reconcile(GraphExtractor.SPACY, [_entity("BERT")])

    _, node_query = _queries(tx)
    assert (
        "WHEN any(l IN remaining_document_links WHERE l ENDS WITH "
        "$document_link_suffix)\n"
        f"                        THEN {MEMBER_IDS}\n"
        f"                        ELSE [d IN {MEMBER_IDS} WHERE d <> "
        "$source_document_id] END AS remaining_document_ids"
    ) in node_query
    assert _params(tx, 1)["document_link_suffix"] == ":doc-A"
    assert "AND NOT $source_document_id IN remaining_document_ids" in node_query
    assert "SET e.document_ids = remaining_document_ids" in node_query
    assert "e.document_links = remaining_document_links" in node_query


@pytest.mark.unit
def test_reconcile_uses_the_delete_guards_for_dropped_entities() -> None:
    """Once the doc has no link left, the node is handled exactly like a
    document delete: re-point the creator stamp to a remaining member, delete
    only when no document, a non-null stamp and no edges remain. Plain
    DELETE, never DETACH."""
    _, tx, _ = _reconcile(GraphExtractor.SPACY, [_entity("BERT")])

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
def test_rerun_withdraws_only_its_own_assertion_from_unemitted_edges() -> None:
    """Per-document edges carry the extractors that asserted them. A re-run
    withdraws itself from the doc's edges it no longer emits and deletes an
    edge only when no extractor asserts it any more; edges it did emit, and
    other documents' edges, are not touched."""
    _, tx, _ = _reconcile(
        GraphExtractor.SPACY,
        [_entity("BERT"), _entity("Transformer")],
        [_edge("n1", "n2")],
    )

    rel_query, _ = _queries(tx)
    assert (
        "MATCH (source)-[r:RELATED_TO {source_document_id: $source_document_id}]"
        "->(target)"
    ) in rel_query
    assert "$document_extractor IN r.extractors" in rel_query
    assert "NOT [source.id, target.id, r.type] IN $kept_relationships" in rel_query
    assert (
        "SET r.extractors = [x IN r.extractors WHERE x <> $document_extractor]"
    ) in rel_query
    assert rel_query.index("WHERE size(r.extractors) = 0") < rel_query.index("DELETE r")
    assert "(r.organization_id = $organization_id" in rel_query
    assert "DETACH" not in rel_query
    assert _params(tx, 0)["kept_relationships"] == [["n1", "n2", "PART_OF"]]


@pytest.mark.unit
def test_reconcile_edges_before_nodes_in_one_transaction() -> None:
    """The orphan guard must observe the stale-edge deletes, and a failure
    must not strand a half-reconciled graph: one transaction, edges first."""
    ctx, tx, _ = _reconcile(GraphExtractor.SPACY, [_entity("BERT")])

    ctx.begin_transaction.assert_called_once()
    ctx.run.assert_not_called()
    rel_query, node_query = _queries(tx)
    assert "DELETE r" in rel_query and "kept_entities" not in rel_query
    assert "DELETE e" in node_query
    # No emitted edges → every one of this extractor's edges is stale.
    assert _params(tx, 0)["kept_relationships"] == []


@pytest.mark.unit
def test_reconcile_without_org_drops_org_predicates() -> None:
    _, tx, _ = _reconcile(GraphExtractor.SPACY, [_entity("BERT")], org=None)

    rel_query, node_query = _queries(tx)
    assert "organization_id" not in rel_query and "organization_id" not in node_query
    assert "WHERE $document_extractor IN r.extractors" in rel_query
    assert "WHERE $document_link IN e.document_links" in node_query
    assert "organization_id" not in _params(tx, 1)


@pytest.mark.unit
def test_legacy_links_are_never_reconciled() -> None:
    """Nodes/edges written before links existed have no document_links /
    extractors. The anchors read the raw property (a null list never
    contains anything), never the unknown-link fallback, so a reconcile can
    not touch them."""
    _, tx, _ = _reconcile(GraphExtractor.LLM, [_entity("BERT")])

    rel_query, node_query = _queries(tx)
    assert "coalesce(e.document_links" not in node_query
    assert "coalesce(r.extractors" not in rel_query


@pytest.mark.unit
@patch(GET_SESSION)
def test_unknown_extractor_cannot_reconcile(mock_session: MagicMock) -> None:
    with pytest.raises(ValueError):
        KnowledgeGraphService().reconcile_document_graph(
            "doc-A",
            "org-A",
            extractor=GraphExtractor.UNKNOWN,
            entities=[_entity("BERT")],
            relationships=[],
        )
    mock_session.assert_not_called()


@pytest.mark.unit
@patch(GET_SESSION)
def test_reconcile_with_no_emitted_entities_is_a_noop(
    mock_session: MagicMock,
) -> None:
    """An empty extraction is far more likely an extractor problem than a
    document that truly names nothing; never strip the whole document on it."""
    assert KnowledgeGraphService().reconcile_document_graph(
        "doc-A",
        "org-A",
        extractor=GraphExtractor.SPACY,
        entities=[_entity("   ")],
        relationships=[],
    ) == (0, 0)
    mock_session.assert_not_called()


@pytest.mark.unit
@patch(GET_SESSION)
def test_document_delete_removes_every_link_of_the_document(
    mock_session: MagicMock,
) -> None:
    """A document delete still removes ALL of the doc: every edge, every
    extractor's link (by ``:<doc>`` suffix), then the membership. No
    reconcile predicates leak into it."""
    ctx, tx = _tx_session()
    mock_session.return_value = ctx
    KnowledgeGraphService().delete_document_graph("doc-A", "org-A")

    rel_query, node_query = _queries(tx)
    assert "kept_" not in rel_query + node_query
    assert "MATCH ()-[r:RELATED_TO {source_document_id: $source_document_id}]->()" in (
        rel_query
    )
    assert f"$source_document_id IN {MEMBER_IDS}" in node_query
    assert (
        "e.document_links = [l IN e.document_links WHERE NOT l ENDS WITH "
        "$document_link_suffix]"
    ) in node_query
    assert _params(tx, 1)["document_link_suffix"] == ":doc-A"
    assert "kept_entities" not in _params(tx, 1)


# --------------------------------------------------------------------------
# Write paths: every link and edge records its extractor
# --------------------------------------------------------------------------


class _Capture:
    """Records every (query, params) pair run on a session or transaction."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def run(self, query: str, params: Dict[str, Any]) -> Any:
        self.calls.append((query, params))
        record = {
            "resolved_id": "e1",
            "created_at": 1,
            "updated_at": 1,
            "idx": 0,
            "id": "e1",
            "r": {"id": "r1"},
            "source": {},
            "target": {},
        }
        result = MagicMock()
        result.single.return_value = record
        result.__iter__ = lambda self: iter([record])
        return result


def _svc_with_session(capture: _Capture) -> KnowledgeGraphService:
    svc = KnowledgeGraphService()

    @contextlib.contextmanager
    def _fake_get_session(database: str = "neo4j") -> Iterator[_Capture]:
        yield capture

    svc.get_session = _fake_get_session  # type: ignore[method-assign,assignment]
    return svc


UNKNOWN_BACKFILL = (
    "coalesce(e.document_links, [d IN existing_document_ids | 'unknown:' + d]) "
    "AS existing_document_links"
)


@pytest.mark.unit
def test_entity_write_records_its_extractor_link() -> None:
    """A write appends '<extractor>:<doc>' next to the document_ids append.
    New nodes start with [] links (ON CREATE); a node written before links
    existed backfills its current members as 'unknown:' links first, so its
    old membership survives every reconcile."""
    capture = _Capture()
    _svc_with_session(capture).create_entity(
        _entity("BERT"), extractor=GraphExtractor.SPACY
    )

    query, params = capture.calls[-1]
    on_create = query.split("ON CREATE SET", 1)[1].split("ON MATCH SET", 1)[0]
    assert "e.document_links = []" in on_create
    assert UNKNOWN_BACKFILL in query
    assert "e.document_links = existing_document_links + [" in query
    assert "AND NOT ($document_extractor + ':' + d) IN existing_document_links" in (
        query
    )
    assert "| $document_extractor + ':' + d]" in query
    assert params["document_extractor"] == "spacy"


@pytest.mark.unit
def test_entity_write_without_extractor_records_unknown() -> None:
    """POST /entities, arXiv, multimodal: no extractor → an 'unknown' link,
    which no reconcile removes."""
    capture = _Capture()
    _svc_with_session(capture).create_entity(_entity("BERT"))
    assert capture.calls[-1][1]["document_extractor"] == "unknown"


@pytest.mark.unit
def test_ingestion_adapter_threads_extractor() -> None:
    capture = _Capture()
    _svc_with_session(capture).create_entity_node(
        entity_text="BERT",
        entity_type="CONCEPT",
        document_id="doc-A",
        organization_id="org-A",
        extractor=GraphExtractor.SPACY,
    )
    assert capture.calls[-1][1]["document_extractor"] == "spacy"


@pytest.mark.unit
def test_batch_threads_extractor_to_entities_and_edges() -> None:
    """create_entities_batch records its extractor on the UNWIND entity merge
    (rows start with [] links) and on every edge."""
    capture = _Capture()
    svc = KnowledgeGraphService()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    tx = MagicMock()
    tx.__enter__ = MagicMock(return_value=capture)
    tx.__exit__ = MagicMock(return_value=False)
    ctx.begin_transaction.return_value = tx
    with patch(GET_SESSION, return_value=ctx):
        response = svc.create_entities_batch(
            BatchEntityRequest(
                entities=[_entity("BERT")],
                relationships=[_edge("n1", "n2")],
                document_id="doc-A",
            ),
            extractor=GraphExtractor.LLM,
        )

    assert not response.errors
    (entity_query, entity_params), (edge_query, edge_params) = capture.calls
    assert "apoc.merge.node" in entity_query
    assert entity_params["document_extractor"] == "llm"
    assert entity_params["rows"][0]["props"]["document_links"] == []
    assert "node.document_links = existing_document_links + [" in entity_query
    assert edge_params["document_extractor"] == "llm"
    on_create = edge_query.split("ON CREATE SET", 1)[1].split("ON MATCH SET", 1)[0]
    assert "r.extractors = []" in on_create
    assert "coalesce(r.extractors, ['unknown']) AS existing_extractors" in edge_query
    assert "WHEN $document_extractor IN existing_extractors THEN []" in edge_query


@pytest.mark.unit
def test_single_relationship_write_records_unknown_by_default() -> None:
    capture = _Capture()
    _svc_with_session(capture).create_relationship(_edge("n1", "n2"))
    query, params = capture.calls[-1]
    assert params["document_extractor"] == "unknown"
    assert "SET r.extractors = existing_extractors + CASE" in query
    assert query.index("SET r.extractors = existing_extractors") < query.index(
        "RETURN r, source, target"
    )


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
    assert kwargs["extractor"] == GraphExtractor.SPACY
    assert [e.name for e in kwargs["entities"]] == ["BERT"]
    assert kwargs["relationships"] == []
    # The write records the same extractor the reconcile scopes to.
    assert kg.create_entities_batch.call_args.kwargs["extractor"] == (
        GraphExtractor.SPACY
    )


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
    assert kwargs["extractor"] == GraphExtractor.LLM
    assert [e.name for e in kwargs["entities"]] == ["BERT"]
    assert kg.create_entities_batch.call_args.kwargs["extractor"] == (
        GraphExtractor.LLM
    )


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
    assert kwargs["extractor"] == GraphExtractor.SPACY
    assert [e.name for e in kwargs["entities"]] == ["BERT"]
    assert kg.create_entities_batch.call_args.kwargs["extractor"] == (
        GraphExtractor.SPACY
    )


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


@pytest.mark.unit
def test_ingestion_fan_out_writes_spacy_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spaCy ingestion fan-out is what the LLM job must not undo, so its
    links have to be recorded as 'spacy' (create_entity_node stamps
    extraction_method=LLM_EXTRACTION, so the method cannot be used)."""
    from src.tasks import processing_tasks as pt

    kg = MagicMock()
    kg.create_entity_node.return_value = "n1"
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)
    document = SimpleNamespace(id="doc-A", organization_id="org-A")
    entity = SimpleNamespace(
        name="BERT", entity_type=SimpleNamespace(value="CONCEPT"), confidence=0.9
    )

    assert pt._index_entities_to_graph(document, [entity]) == 1
    assert kg.create_entity_node.call_args.kwargs["extractor"] == (GraphExtractor.SPACY)
