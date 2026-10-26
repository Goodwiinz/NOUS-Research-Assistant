"""Phase 1a: every in-service entity write stamps organization_id.

create_entity and _batch_merge_entities already stamped org; the batch-fallback
_create_entity_in_transaction and the create_entity_node adapter did not, so a
node created via those paths was org-less and invisible to tenant-scoped reads.
"""

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.models.graph import CreateEntityRequest, EntityType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService


class _Result:
    def __init__(self, record: Dict[str, Any]) -> None:
        self._record = record

    def single(self) -> Dict[str, Any]:
        return self._record


@pytest.mark.unit
def test_create_entity_in_transaction_stamps_org() -> None:
    captured: Dict[str, Any] = {}

    class _Tx:
        def run(self, query: str, params: Dict[str, Any]) -> _Result:
            captured["query"] = query
            captured["params"] = params
            return _Result({"resolved_id": "e1"})

    svc = KnowledgeGraphService()
    req = CreateEntityRequest(
        name="Insulin",
        entity_type=EntityType.CONCEPT,
        organization_id="org-A",
    )
    resp = svc._create_entity_in_transaction(_Tx(), req)

    query = captured["query"]
    # org is part of the MERGE identity (key), not a re-settable property.
    assert "organization_id: $org_key" in query
    assert captured["params"]["org_key"] == "org-A"
    assert resp is not None


@pytest.mark.unit
def test_create_entity_node_adapter_threads_org() -> None:
    svc = KnowledgeGraphService()
    captured: Dict[str, Any] = {}

    def _fake_create_entity(request: CreateEntityRequest, **kwargs: Any) -> Any:
        captured["request"] = request
        return MagicMock(id="e2")

    svc.create_entity = _fake_create_entity  # type: ignore[method-assign]

    svc.create_entity_node(
        entity_text="Transformer",
        entity_type="CONCEPT",
        document_id="doc-1",
        organization_id="org-B",
    )

    assert captured["request"].organization_id == "org-B"
