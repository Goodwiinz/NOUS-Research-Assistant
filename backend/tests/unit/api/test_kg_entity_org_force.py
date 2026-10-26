"""create_entity / batch_create_entities must stamp the CALLER's org, never the
client-supplied organization_id.

CreateEntityRequest / CreateRelationshipRequest expose an organization_id field.
The KG write endpoints previously passed create_entity's request through without
overwriting it, so a caller could omit source_document_id and set
organization_id=<other org> — the node was persisted under that tenant and
surfaced in its org-scoped entity list / search / analytics (cross-tenant graph
poisoning). Both endpoints now force request.organization_id = caller's org.
These tests assert the request object handed to the service carries the caller's
org, regardless of what the client sent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from src.api.search import knowledge_graph as kg


def _caller(org="org-A"):
    return SimpleNamespace(id="u1", organization_id=org)


def test_create_entity_overwrites_client_org(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "create_entity",
        lambda req: captured.setdefault("req", req) or SimpleNamespace(),
    )
    # no source_document_id -> skips that guard; hostile client org
    req = SimpleNamespace(organization_id="org-B", source_document_id=None)
    kg.create_entity(request=req, current_user=_caller("org-A"), db=MagicMock())
    assert captured["req"].organization_id == "org-A"


def test_batch_overwrites_client_org_on_all_items(monkeypatch):
    monkeypatch.setattr(kg, "_get_org_document_ids", lambda db, org: [])
    captured = {}
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "create_entities_batch",
        lambda req, source_document_ids: captured.setdefault("req", req)
        or SimpleNamespace(),
    )
    ents = [
        SimpleNamespace(organization_id="org-B", source_document_id=None),
        SimpleNamespace(organization_id=None, source_document_id=None),
    ]
    rels = [SimpleNamespace(organization_id="org-EVIL", source_document_id=None)]
    req = SimpleNamespace(entities=ents, relationships=rels)
    kg.batch_create_entities(request=req, current_user=_caller("org-A"), db=MagicMock())
    assert all(e.organization_id == "org-A" for e in captured["req"].entities)
    assert all(r.organization_id == "org-A" for r in captured["req"].relationships)


def test_create_entity_tenantless_caller_is_rejected(monkeypatch):
    # A tenantless caller is refused (403) before any write; it can neither
    # keep a client-supplied org nor create an unowned (org=None) node.
    from fastapi import HTTPException

    def _must_not_create(req):
        raise AssertionError("create_entity must not run for a tenantless caller")

    monkeypatch.setattr(kg.knowledge_graph_service, "create_entity", _must_not_create)
    req = SimpleNamespace(organization_id="org-B", source_document_id=None)
    with pytest.raises(HTTPException) as exc:
        kg.create_entity(request=req, current_user=_caller(None), db=MagicMock())
    assert exc.value.status_code == 403


def test_create_entity_with_real_model_instance(monkeypatch):
    # Uses a real CreateEntityRequest (not SimpleNamespace) so the test also
    # guards against a future frozen/validate_assignment config that would make
    # the in-place org stamp raise at runtime.
    from src.models.graph import CreateEntityRequest

    captured = {}
    monkeypatch.setattr(
        kg.knowledge_graph_service,
        "create_entity",
        lambda req: captured.setdefault("req", req) or SimpleNamespace(),
    )
    req = CreateEntityRequest(
        name="INJECTED", entity_type="PERSON", organization_id="org-B"
    )
    kg.create_entity(request=req, current_user=_caller("org-A"), db=MagicMock())
    assert captured["req"].organization_id == "org-A"
