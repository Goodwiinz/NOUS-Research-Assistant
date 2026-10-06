"""GOO-331: a Daily Brief search may never widen its confirmed scope.

The search step resolves ``{providers}`` and ``{limit_per_provider}`` from the
engine context. These tests drift that context away from the server-owned
``scope_confirmation`` and require the step itself to fail closed before any
connector is called.

Mutation check (docs/engineering/testing.md), guard in
``StepExecutor._execute_search`` (backend/src/services/research_engine/
step_executor.py, the ``_is_daily_brief_context`` block): removing the provider
subset check fails ``test_undeclared_provider_is_refused_before_any_search``;
removing the limit check fails ``test_widened_limit_is_refused_before_any_search``.
Command: ``pytest -q backend/tests/unit/services/test_daily_brief_search_scope.py``
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.services.research_engine.step_executor import StepExecutor

# Mirrors the search step in blueprints/templates/daily_research_brief.yaml.
SEARCH_STEP = {
    "type": "search",
    "parameters": {
        "contract_version": 1,
        "sources": "{providers}",
        "query_template": "{research_question}",
        "max_results_per_source": "{limit_per_provider}",
    },
}


def _context(*, providers: list[str], limit: int) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "research_question": "sleep and memory",
        "providers": providers,
        "limit_per_provider": limit,
        "scope_confirmation": {
            "contract_version": 1,
            "research_question": "sleep and memory",
            "providers": ["openalex"],
            "limit_per_provider": 10,
            "confirmed": True,
        },
        "provider_manifest": [{"id": "openalex"}],
    }


def _connectors() -> dict[str, Any]:
    return {
        name: AsyncMock(search=AsyncMock(return_value=[]))
        for name in ("openalex", "pubmed")
    }


@pytest.mark.asyncio
async def test_undeclared_provider_is_refused_before_any_search() -> None:
    connectors = _connectors()
    with pytest.raises(ValueError, match="confirmed Daily Brief scope"):
        await StepExecutor(connectors, {}).execute(
            SEARCH_STEP, _context(providers=["openalex", "pubmed"], limit=10)
        )
    for connector in connectors.values():
        connector.search.assert_not_called()


@pytest.mark.asyncio
async def test_widened_limit_is_refused_before_any_search() -> None:
    connectors = _connectors()
    with pytest.raises(ValueError, match="confirmed Daily Brief scope"):
        await StepExecutor(connectors, {}).execute(
            SEARCH_STEP, _context(providers=["openalex"], limit=11)
        )
    for connector in connectors.values():
        connector.search.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_scope_searches_only_confirmed_providers() -> None:
    connectors = _connectors()
    result = await StepExecutor(connectors, {}).execute(
        SEARCH_STEP, _context(providers=["openalex"], limit=10)
    )
    assert result.output["stage_type"] == "search"
    connectors["openalex"].search.assert_awaited()
    connectors["pubmed"].search.assert_not_called()
