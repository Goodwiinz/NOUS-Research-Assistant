"""KG write endpoints must reject a client source_document_id outside the caller's org.

`/batch` and `POST /relationships` stamped whatever `source_document_id` the
client sent. Project- and document-scoped reads filter only by that id, so a
member of org A who knew one of org B's document UUIDs could inject entities
and edges into B's project knowledge tree (audit finding KN4). `create_entity`
already rejected a foreign id; these tests hold the batch and relationship
endpoints to the same rule, with a 403 rather than a swallowed 500.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.search import knowledge_graph as kg
from src.models.graph import (
    BatchEntityRequest,
    CreateEntityRequest,
    CreateRelationshipRequest,
)

pytestmark = pytest.mark.unit

OWN_DOC = "11111111-1111-1111-1111-111111111111"
FOREIGN_DOC = "22222222-2222-2222-2222-222222222222"


def _caller(org: Optional[str] = "org-A") -> SimpleNamespace:
    return SimpleNamespace(id="u1", organization_id=org)


def _entity(source_document_id: Optional[str]) -> CreateEntityRequest:
    return CreateEntityRequest(
        name="INJECTED",
        entity_type="PERSON",
        source_document_id=source_document_id,
    )


def _relationship(source_document_id: Optional[str]) -> CreateRelationshipRequest:
    return CreateRelationshipRequest(
        source_entity_id="e1",
        target_entity_id="e2",
        relationship_type="RELATED_TO",
        source_document_id=source_document_id,
    )


@pytest.fixture
def service_calls(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    calls: Dict[str, List[Any]] = {"batch": [], "relationship": []}
    monkeypatch.setattr(kg, "_get_org_document_ids", lambda db, org: [OWN_DOC])

    def fake_batch(req: Any, source_document_ids: Any = None) -> SimpleNamespace:
        calls["batch"].append(req)
        return SimpleNamespace()

    def fake_relationship(req: Any, source_document_ids: Any = None) -> Any:
        calls["relationship"].append(req)
        return SimpleNamespace()

    monkeypatch.setattr(kg.knowledge_graph_service, "create_entities_batch", fake_batch)
    monkeypatch.setattr(
        kg.knowledge_graph_service, "create_relationship", fake_relationship
    )
    return calls


def test_batch_rejects_entity_with_foreign_document(
    service_calls: Dict[str, List[Any]],
) -> None:
    request = BatchEntityRequest(entities=[_entity(OWN_DOC), _entity(FOREIGN_DOC)])
    with pytest.raises(HTTPException) as exc:
        kg.batch_create_entities(
            request=request, current_user=_caller(), db=MagicMock()
        )
    assert exc.value.status_code == 403
    assert service_calls["batch"] == []


def test_batch_rejects_relationship_with_foreign_document(
    service_calls: Dict[str, List[Any]],
) -> None:
    request = BatchEntityRequest(
        entities=[_entity(None)], relationships=[_relationship(FOREIGN_DOC)]
    )
    with pytest.raises(HTTPException) as exc:
        kg.batch_create_entities(
            request=request, current_user=_caller(), db=MagicMock()
        )
    assert exc.value.status_code == 403
    assert service_calls["batch"] == []


def test_batch_accepts_own_and_missing_documents(
    service_calls: Dict[str, List[Any]],
) -> None:
    request = BatchEntityRequest(
        entities=[_entity(OWN_DOC), _entity(None)],
        relationships=[_relationship(OWN_DOC), _relationship(None)],
    )
    kg.batch_create_entities(request=request, current_user=_caller(), db=MagicMock())
    assert service_calls["batch"] == [request]


def test_create_relationship_rejects_foreign_document(
    service_calls: Dict[str, List[Any]],
) -> None:
    with pytest.raises(HTTPException) as exc:
        kg.create_relationship(
            request=_relationship(FOREIGN_DOC), current_user=_caller(), db=MagicMock()
        )
    assert exc.value.status_code == 403
    assert service_calls["relationship"] == []


def test_create_relationship_accepts_own_document(
    service_calls: Dict[str, List[Any]],
) -> None:
    request = _relationship(OWN_DOC)
    kg.create_relationship(request=request, current_user=_caller(), db=MagicMock())
    assert service_calls["relationship"] == [request]
