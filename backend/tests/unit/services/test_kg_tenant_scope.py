"""Regression tests: KG entity search must be tenant-scoped.

Deep-audit 2026-07-16 found call sites that reached KnowledgeGraphService's
entity search WITHOUT an organization_id, so `_entity_scope_predicate` fell
through to its unscoped branch and returned entities across ALL tenants into a
user's search results. These tests assert the scope predicate is present when
an org is supplied and pin the two callers' forwarding via source inspection.
"""

import inspect

import pytest


@pytest.mark.unit
def test_entity_scope_predicate_scopes_on_org_alone() -> None:
    """organization_id alone must produce an indexed equality predicate."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        _entity_scope_predicate,
    )

    params: dict = {}
    pred = _entity_scope_predicate("e", None, "org-123", params)

    assert pred is not None, "org-scoped call must not be unscoped"
    assert "e.organization_id = $organization_id" in pred
    assert params["organization_id"] == "org-123"

    # Both None → a dropped scope fails closed (D2): it matches nothing.
    assert _entity_scope_predicate("e", None, None, {}) == "false"


@pytest.mark.unit
def test_hybrid_kg_search_forwards_organization_id() -> None:
    """_execute_knowledge_graph_search must pass organization_id to the KG
    search — otherwise the hybrid KG leg leaks cross-tenant entities."""
    from src.services.search.hybrid_search_service import HybridSearchService

    src = inspect.getsource(HybridSearchService._execute_knowledge_graph_search)
    assert "organization_id=organization_id" in src


@pytest.mark.unit
def test_kg_search_adapter_forwards_organization_id() -> None:
    """The service-level search() adapter must forward organization_id directly,
    not rely solely on the derived source_document_ids (which is None on a doc
    lookup failure → unscoped)."""
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    src = inspect.getsource(KnowledgeGraphService.search)
    assert "organization_id=organization_id" in src
