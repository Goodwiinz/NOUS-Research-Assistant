"""Regression: relationship creation MERGEs (idempotent) instead of CREATE.

create_relationship / _create_relationship_in_transaction used CREATE with a
fresh uuid every call, so re-ingesting the same document piled up duplicate
RELATED_TO edges between the same two entities. They now MERGE on
(source, target, type, source_document_id) and refresh mutable fields,
returning the edge's actual id (which, on an existing edge, is NOT the freshly
minted uuid). source_document_id is part of the key so two DIFFERENT documents
asserting the same pair keep separate provenance edges.
"""

import contextlib
from typing import Any, Dict, Iterator

import pytest

from src.models.graph import CreateRelationshipRequest, RelationshipType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService


class _FakeResult:
    def __init__(self, record: Dict[str, Any]) -> None:
        self._record = record

    def single(self) -> Dict[str, Any]:
        return self._record


class _FakeSession:
    """Captures the Cypher and returns a record whose edge id differs from the
    generated uuid, so the test can prove the id is read back from the result."""

    def __init__(self, record: Dict[str, Any], captured: Dict[str, Any]) -> None:
        self._record = record
        self._captured = captured

    def run(self, query: str, params: Dict[str, Any]) -> _FakeResult:
        self._captured["query"] = query
        self._captured["params"] = params
        return _FakeResult(self._record)


def _service_with_session(
    record: Dict[str, Any], captured: Dict[str, Any]
) -> KnowledgeGraphService:
    svc = KnowledgeGraphService()

    @contextlib.contextmanager
    def _fake_get_session(database: str = "neo4j") -> Iterator[_FakeSession]:
        yield _FakeSession(record, captured)

    svc.get_session = _fake_get_session  # type: ignore[method-assign]
    return svc


def _request() -> CreateRelationshipRequest:
    return CreateRelationshipRequest(
        source_entity_id="entity-a",
        target_entity_id="entity-b",
        relationship_type=RelationshipType.RELATED_TO,
        strength=0.7,
        source_document_id="doc-1",
    )


def _assert_merge_upsert_shape(query: str) -> None:
    """Both paths must MERGE (idempotent) keyed on type + source_document_id,
    set props via ON CREATE/ON MATCH, and never blindly CREATE the edge."""
    assert "MERGE (source)-[r:RELATED_TO {" in query
    assert "type: $relationship_type" in query
    # Provenance is part of the dedupe key, not overwritten on match.
    assert "source_document_id: $source_document_id" in query
    assert "ON CREATE SET" in query
    assert "ON MATCH SET" in query
    assert "CREATE (source)-[r:RELATED_TO" not in query
    # source_document_id is a key, so it must not be re-SET in the ON MATCH arm.
    on_match = query.split("ON MATCH SET", 1)[1]
    assert "source_document_id" not in on_match


@pytest.mark.unit
def test_create_relationship_merges_not_creates() -> None:
    captured: Dict[str, Any] = {}
    record = {"r": {"id": "pre-existing-edge"}, "source": {}, "target": {}}
    svc = _service_with_session(record, captured)

    # Scoped by org: create_relationship refuses unscoped requests (D2).
    resp = svc.create_relationship(
        _request().model_copy(update={"organization_id": "org-1"})
    )

    _assert_merge_upsert_shape(captured["query"])
    # The existing edge's id is read back, not the freshly generated uuid.
    assert resp.id == "pre-existing-edge"
    # source_document_id coalesced into the params for use as a MERGE key.
    assert captured["params"]["source_document_id"] == "doc-1"


@pytest.mark.unit
def test_create_relationship_in_transaction_merges_not_creates() -> None:
    captured: Dict[str, Any] = {}
    record = {"r": {"id": "pre-existing-edge"}}

    class _Tx:
        def run(self, query: str, params: Dict[str, Any]) -> _FakeResult:
            captured["query"] = query
            captured["params"] = params
            return _FakeResult(record)

    svc = KnowledgeGraphService()
    resp = svc._create_relationship_in_transaction(_Tx(), _request())

    _assert_merge_upsert_shape(captured["query"])
    assert resp.id == "pre-existing-edge"


@pytest.mark.unit
def test_null_source_document_id_coalesced_for_merge_key() -> None:
    captured: Dict[str, Any] = {}
    record = {"r": {"id": "e1"}, "source": {}, "target": {}}
    svc = _service_with_session(record, captured)

    req = CreateRelationshipRequest(
        source_entity_id="a",
        target_entity_id="b",
        relationship_type=RelationshipType.RELATED_TO,
        organization_id="org-1",
    )
    svc.create_relationship(req)

    # None would make Cypher reject the MERGE key; it must be coalesced to "".
    assert captured["params"]["source_document_id"] == ""
