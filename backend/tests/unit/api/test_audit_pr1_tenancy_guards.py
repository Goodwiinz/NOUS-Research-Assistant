"""PR1 (audit R6-H2/H4/H5, R5-M7/M8, R6-L2/L10) regression guards.

Behavioral where cheap (config validators, graph service fail-closed),
source-guards where the code path needs live infra (Neo4j, route wiring).
"""

import ast
from pathlib import Path
from typing import Any

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# R6-H4 — ENVIRONMENT gates read Settings, not os.getenv
# ---------------------------------------------------------------------------


def test_config_prod_gates_fire_without_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates a deployment supplying ENVIRONMENT only via .env / init value.

    Before the fix the validators read os.getenv("ENVIRONMENT") and silently
    accepted weak secrets in production.
    """
    import os
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    from src.core.config import Settings

    # Hermetic production baseline: unrelated validators must not fire before
    # the secret gates we're asserting on (full-suite runs leak LOG_LEVEL /
    # DATABASE_URL-class vars into the process env; SUPABASE_DB_URL is pinned
    # empty because _override_database_url_from_supabase lets an env value
    # beat our explicit DATABASE_URL).
    base: dict[str, Any] = dict(
        ENVIRONMENT="production",
        DATABASE_URL="postgresql://u:p@db.example.com:5432/db",
        SUPABASE_DB_URL="",
        CORS_ORIGIN_REGEX="",
        NEO4J_URI="neo4j://neo4j.example.com:7687",
        NEO4J_PASSWORD="n" * 24,
    )

    with pytest.raises(Exception, match="JWT_SECRET_KEY"):
        Settings(SECRET_KEY="s" * 40, JWT_SECRET_KEY="", **base)

    with pytest.raises(Exception, match="SECRET_KEY"):
        Settings(SECRET_KEY="", JWT_SECRET_KEY="j" * 40, **base)

    # Dev still auto-generates instead of raising.
    s = Settings(
        ENVIRONMENT="development",
        JWT_SECRET_KEY="j" * 40,
        DATABASE_URL="postgresql://u:p@localhost/db",
        SUPABASE_DB_URL="",
    )
    assert s.SECRET_KEY


def test_config_has_no_os_getenv_environment_reads() -> None:
    source = (BACKEND_ROOT / "src/core/config.py").read_text()
    assert 'os.getenv("ENVIRONMENT"' not in source
    assert 'info.data.get("ENVIRONMENT"' in source


# ---------------------------------------------------------------------------
# R6-H2 — LLM cache keys are tenant- and system-prompt-scoped
# ---------------------------------------------------------------------------


def test_chat_cache_query_includes_tenant_and_system_prompt() -> None:
    source = (BACKEND_ROOT / "src/api/research/chat.py").read_text()
    assert "tenant_scope" in source
    assert "current_user.organization_id" in source
    assert "system_prompt_hash" in source
    # The scope must be part of cache_query before both cache.get and cache.set
    get_pos = source.find("llm_response_cache.get")
    set_pos = source.find("llm_response_cache.set")
    scope_pos = source.find('cache_query = f"{tenant_scope}')
    assert 0 < scope_pos < get_pos < set_pos


# ---------------------------------------------------------------------------
# R6-H5 — citation graph is org-partitioned end to end
# ---------------------------------------------------------------------------


def test_citation_graph_read_requires_organization_id() -> None:
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    from src.services.research.citation_graph_service import CitationGraphService

    svc = CitationGraphService()
    with pytest.raises(ValueError, match="organization_id"):
        # Must fail closed BEFORE any driver/connection work.
        import asyncio

        asyncio.run(svc.get_citation_graph(project_id=None, document_id=None))


def _service_source(rel: str) -> str:
    return (BACKEND_ROOT / rel).read_text()


def test_citation_graph_queries_carry_org_scope() -> None:
    source = _service_source("src/services/research/citation_graph_service.py")
    # Sync writes tenant properties onto the node...
    assert "c.organization_id = $organization_id" in source
    assert "c.project_id = $project_id" in source
    # ...reads anchor every branch inside the caller's partition...
    assert "start.organization_id = $organization_id" in source
    # ...and traversal results are post-filtered so pre-existing foreign
    # nodes can never reach the payload.
    assert 'r.get("organization_id") == organization_id' in source


def test_citation_graph_route_passes_org() -> None:
    source = (BACKEND_ROOT / "src/api/research/citations.py").read_text()
    get_call = source[source.find("graph_service.get_citation_graph") :]
    assert "organization_id=str(current_user.organization_id)" in get_call[:400]


# ---------------------------------------------------------------------------
# R5-M7 — relationship delete honors the same org partition as reads
# ---------------------------------------------------------------------------


def test_delete_relationship_signature_and_scope() -> None:
    import inspect
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    from src.services.knowledge_graph.knowledge_graph_service import (
        KnowledgeGraphService,
    )

    sig = inspect.signature(KnowledgeGraphService.delete_relationship)
    assert "organization_id" in sig.parameters

    source = _service_source("src/services/knowledge_graph/knowledge_graph_service.py")
    delete_block = source[
        source.find("def delete_relationship") : source.find(
            "def find_related_entities"
        )
    ]
    # Uses the shared two-endpoint scope helper (org OR doc-ids), not a bare
    # source_document_id IN-list that arXiv entities can never match.
    assert "_two_endpoint_scope(" in delete_block
    assert "$relationship_id" in delete_block


# ---------------------------------------------------------------------------
# R5-M8 — analytics endpoint passes the org partition, not just doc ids
# ---------------------------------------------------------------------------


def test_analytics_route_passes_organization_id() -> None:
    source = (BACKEND_ROOT / "src/api/search/knowledge_graph.py").read_text()
    route = source[source.find("def get_graph_analytics(") :]
    # The org is resolved by the fail-closed guard (403 for an org-less
    # caller) before any scoping/service work, then forwarded as-is.
    assert "org_id = _require_org_id(current_user)" in route[:600]
    assert route.find("_require_org_id(current_user)") < route.find("try:")
    call = route[route.find("knowledge_graph_service.get_graph_analytics") :]
    assert "organization_id=org_id" in call[:800]
    # F2: the doc-id list is only supplied for an explicit ?project_id= filter,
    # where it NARROWS the org counts instead of replacing them.
    assert (
        "source_document_ids=scope_doc_ids if project_id is not None else None"
        in call[:800]
    )


# ---------------------------------------------------------------------------
# R6-L10 — message-citation document fetch excludes soft-deleted rows
# ---------------------------------------------------------------------------


def test_message_citation_document_fetch_is_delete_aware() -> None:
    source = (
        BACKEND_ROOT / "src/services/research/message_citation_service.py"
    ).read_text()
    block = source[source.find("async def _get_document") :]
    assert "Document.is_deleted == False" in block[:900]
    assert "organization_id" in block[:900]


# ---------------------------------------------------------------------------
# R6-L2 — title enrichment cannot leak deleted/foreign documents' titles
# ---------------------------------------------------------------------------


def test_title_enrichment_query_is_scoped() -> None:
    source = (BACKEND_ROOT / "src/services/search/hybrid_search_service.py").read_text()
    block = source[source.find("def _enrich_titles_from_db") :]
    assert "NOT is_deleted" in block[:1600]
    assert "organization_id = :organization_id" in block[:1600]


# ---------------------------------------------------------------------------
# F2 — two-endpoint traversal scope ORs org with the legacy doc-id list
# ---------------------------------------------------------------------------


def test_two_endpoint_scope_ors_org_and_doc_ids() -> None:
    """Org-exclusive precedence made legacy NULL-org relationships listable
    (via the OR'ing _entity_scope_predicate) but undeletable/untraversable."""
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    from src.services.knowledge_graph.knowledge_graph_service import _two_endpoint_scope

    frag, params = _two_endpoint_scope("a", "b", ["d1", "d2"], "org-1")

    assert (
        "AND (a.organization_id = $organization_id "
        "OR a.source_document_id IN $source_document_ids)" in frag
    )
    assert (
        "AND (b.organization_id = $organization_id "
        "OR b.source_document_id IN $source_document_ids)" in frag
    )
    assert params == {
        "organization_id": "org-1",
        "source_document_ids": ["d1", "d2"],
    }

    # Single-scope callers keep a bare equality (no stray OR / unbound param).
    org_only, org_params = _two_endpoint_scope("a", "b", None, "org-1")
    assert " OR " not in org_only
    assert org_params == {"organization_id": "org-1"}


def test_graph_analytics_ands_org_with_project_doc_ids() -> None:
    """A ?project_id= doc-id list must narrow the org counts, not fall back.

    The old ``elif source_document_ids`` made the doc-id branch dead (the route
    always passes an org), so a project filter silently returned org-wide
    counts.
    """
    source = _service_source("src/services/knowledge_graph/knowledge_graph_service.py")
    block = source[
        source.find("def get_graph_analytics") : source.find("def get_health_status")
    ]
    assert "elif source_document_ids is not None:" not in block
    assert '" AND ".join(scope_preds)' in block
    assert "{alias}.organization_id = $organization_id" in block
    assert "{alias}.source_document_id IN $source_document_ids" in block


# ---------------------------------------------------------------------------
# N7/N8/N9 — citation graph counts, project scope and fail-closed writes
# ---------------------------------------------------------------------------


def test_citation_citedby_counts_are_org_scoped() -> None:
    """citedBy counted cross-org edges while the post-filter dropped those
    nodes — counts and returned nodes disagreed. Both the APOC query and the
    no-APOC fallback must constrain the citing node."""
    source = _service_source("src/services/research/citation_graph_service.py")
    graph_block = source[
        source.find("async def get_citation_graph") : source.find(
            "async def get_node_details"
        )
    ]
    assert "<-[incoming:CITES]-()" not in graph_block
    assert (
        graph_block.count(
            "OPTIONAL MATCH (node)<-[incoming:CITES]-(citer:Citation)\n"
            "        WHERE citer.organization_id = $organization_id"
        )
        == 2
    )


def test_citation_project_scope_is_strict() -> None:
    """No is_uploaded escape hatch: an uploaded citation elsewhere in the org
    must not enter the requested project's graph."""
    source = _service_source("src/services/research/citation_graph_service.py")
    block = source[source.find("elif project_id:") :]
    match_clause = block[block.find('match_clause = f"""') : block.find("params = {")]
    assert "AND start.project_id = $project_id" in match_clause
    assert "is_uploaded" not in match_clause


def test_sync_citation_requires_organization_id() -> None:
    """Root cause of the null-org nodes: the write accepted org=None."""
    import asyncio
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    from uuid import uuid4

    from src.services.research.citation_graph_service import CitationGraphService

    service = CitationGraphService()
    with pytest.raises(ValueError, match="organization_id"):
        asyncio.run(
            service.sync_citation_to_graph(
                citation_id=uuid4(),
                document_id=None,
                title="Legacy paper",
            )
        )


def test_sync_project_citations_is_gone() -> None:
    """Deleted: zero callers, and it fished a nonexistent "organization_id"
    key out of plain citation dicts, writing null-org nodes while reporting
    them as synced."""
    source = _service_source("src/services/research/citation_graph_service.py")
    assert "sync_project_citations" not in source


# ---------------------------------------------------------------------------
# F5 — the message-citation tenant predicate is actually reachable
# ---------------------------------------------------------------------------


def test_extraction_threads_org_into_document_lookup() -> None:
    """_get_document grew an organization_id guard but the only call site did
    not pass one, so the guard never ran."""
    import inspect
    import sys

    sys.path.insert(0, str(BACKEND_ROOT))
    from src.services.research.message_citation_service import MessageCitationService

    sig = inspect.signature(MessageCitationService.extract_citations_from_message)
    assert "organization_id" in sig.parameters

    source = _service_source("src/services/research/message_citation_service.py")
    assert "self._get_document(document_id, organization_id)" in source
