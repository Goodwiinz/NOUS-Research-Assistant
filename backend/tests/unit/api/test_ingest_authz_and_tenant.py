"""Security regressions for ingestion endpoints (found via ingestion bug-hunt).

1. The Kaggle bulk-ingest routers (arxiv_bulk, arxiv_llm_bulk) are global,
   process-wide and cost-intensive. Tenant ADMIN roles must not grant access;
   both routers carry a router-level platform-operator dependency.
2. The research-engine rag_store connector searched the hybrid index with no
   organization_id, so a run surfaced (and copied full_text from) every
   tenant's documents. The run owner's org is now threaded into the search.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _dep_funcs(router):
    return {getattr(d.dependency, "__name__", "") for d in router.dependencies}


def test_bulk_routers_require_platform_operator_at_router_level():
    from src.core import dependencies as dependency_module

    require_platform_operator = getattr(
        dependency_module, "require_platform_operator", None
    )
    assert (
        require_platform_operator is not None
    ), "bulk ingestion must use the platform operator boundary"
    from src.api.arxiv.arxiv_bulk import router as bulk
    from src.api.arxiv.arxiv_llm_bulk import router as llm_bulk

    assert require_platform_operator.__name__ in _dep_funcs(
        bulk
    ), "arxiv_bulk router must require a platform operator"
    assert require_platform_operator.__name__ in _dep_funcs(
        llm_bulk
    ), "arxiv_llm_bulk router must require a platform operator"


def test_bulk_routers_do_not_use_tenant_admin_dependency():
    from src.api.arxiv.arxiv_bulk import router as bulk
    from src.api.arxiv.arxiv_llm_bulk import router as llm_bulk
    from src.core.dependencies import require_admin

    assert require_admin.__name__ not in _dep_funcs(bulk)
    assert require_admin.__name__ not in _dep_funcs(llm_bulk)


def test_rag_store_search_passes_org_to_hybrid_search():
    import asyncio

    from src.api.research_engine import runs as runs_mod

    captured = {}

    class _Resp:
        results = []

    def _fake_search(search_request, user_id=None, organization_id=None):
        captured["organization_id"] = organization_id
        return _Resp()

    with patch(
        "src.services.search.hybrid_search_service.hybrid_search_service.search",
        new=_fake_search,
    ):
        asyncio.run(runs_mod._search_rag_store("q", organization_id="org-123"))

    assert captured["organization_id"] == "org-123", (
        "rag_store search must be scoped to the run owner's org, not run "
        "unfiltered across all tenants"
    )


def test_build_connectors_binds_org_into_rag_store():
    from src.api.research_engine import runs as runs_mod

    connectors = runs_mod._build_connectors(organization_id="org-abc")
    search_fn = connectors["rag_store"].search_fn
    # functools.partial binds organization_id as a keyword.
    assert getattr(search_fn, "keywords", {}).get("organization_id") == "org-abc"
