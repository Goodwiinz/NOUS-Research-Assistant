"""Round-1 audit M7, M8, L7, L8, L9 — tool argument contracts.

Each of these was a tool quietly rewriting the model's request into something
else: dropping ids past a cap while reporting success, downgrading a rejected
connector name to "search everything", and sharing one stateful sandbox
between conversations.

The guards first lived in the ``tools.py`` ``@tool`` wrapper bodies, which the
production path (``execute_tool`` -> ``_dispatch_tool`` -> ``_tool_*``) never
ran (agent audit round 8). The wrappers are schema-only now, so these tests
pin the dispatcher and impls the graph actually calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.services.agent import tools_impl

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _user() -> Any:
    return SimpleNamespace(id=uuid4(), organization_id=uuid4())


async def test_ingest_refuses_over_cap_instead_of_truncating() -> None:
    """M8: 15 requested used to become 10 ingested, reported as '10 of 10'."""
    paper_ids = [f"2401.{i:05d}" for i in range(15)]

    result = await tools_impl._dispatch_tool(
        "ingest_arxiv_papers",
        {"paper_ids": paper_ids},
        "user-1",
        MagicMock(),
        _user(),
    )

    assert "error" in result
    assert "10" in result["error"]


async def test_compare_cap_matches_the_impl_limit() -> None:
    """L7: wrapper capped at 10, impl rejected >5 — 6-10 was an error loop."""
    result = await tools_impl._dispatch_tool(
        "compare_documents",
        {"document_ids": [str(uuid4()) for _ in range(6)]},
        db=MagicMock(),
        current_user=_user(),
    )

    assert "error" in result
    assert "5" in result["error"]


async def test_bibliography_export_is_bounded() -> None:
    """L8: document_ids went through uncapped, producing an unbounded IN()."""
    db = MagicMock()

    result = await tools_impl._dispatch_tool(
        "export_bibliography",
        {"document_ids": [str(uuid4()) for _ in range(51)]},
        db=db,
        current_user=_user(),
    )

    assert "error" in result
    # Must be the cap talking, not an incidental auth/context error.
    assert "50" in result["error"]
    assert "51" in result["error"]
    db.execute.assert_not_called()


async def test_invalid_connector_is_refused_not_fanned_out(monkeypatch) -> None:
    """M7: a rejected name became 'unspecified' → ~250-connector burst."""
    from src.services.connectors import connector_registry

    def _no_fan_out(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an invalid identifier fanned out")

    monkeypatch.setattr(connector_registry, "list_available", _no_fan_out)
    monkeypatch.setattr(connector_registry, "search_by_domain", _no_fan_out)

    result = await tools_impl._dispatch_tool(
        "search_external_database", {"query": "aspirin", "connector": "pub med!"}
    )

    assert "error" in result
    assert "connector" in result["error"].lower()

    domain_result = await tools_impl._dispatch_tool(
        "list_external_databases", {"domain": "../../etc"}
    )
    assert "error" in domain_result


async def test_valid_connector_still_reaches_the_impl(monkeypatch) -> None:
    """The M7 guard must not touch the legitimate path."""
    seen: Dict[str, Any] = {}

    async def _fake_impl(args: Dict[str, Any]) -> Dict[str, Any]:
        seen.update(args)
        return {"results": []}

    monkeypatch.setattr(tools_impl, "_tool_search_external_database", _fake_impl)

    await tools_impl.execute_tool(
        "search_external_database", {"query": "aspirin", "connector": "pubmed"}
    )

    assert seen["connector"] == "pubmed"


@pytest.mark.parametrize("has_user", [False, True])
async def test_sandbox_never_opens_without_a_thread(monkeypatch, has_user) -> None:
    """L9: no thread_id meant thread_id='default' — one sandbox for everyone."""
    from src.services.sandbox import e2b_sandbox_manager

    def _no_sandbox() -> Any:
        raise AssertionError("the sandbox was opened without a thread identity")

    monkeypatch.setattr(e2b_sandbox_manager, "get_sandbox_manager", _no_sandbox)

    result = await tools_impl._dispatch_tool(
        "execute_code",
        {"code": "1+1", "description": "smoke"},
        current_user=_user() if has_user else None,
        thread_id="",
    )

    assert "error" in result


# The wrapper clamped these to >= 1; the impls only applied ``min()``, so a
# negative value reached the provider on the production path.


async def test_search_documents_limit_is_at_least_one() -> None:
    seen: Dict[str, Any] = {}

    async def _execute(statement: Any) -> Any:
        seen["limit"] = statement._limit_clause.value
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.execute = _execute

    await tools_impl._dispatch_tool(
        "search_documents",
        {"query": "q", "max_results": 0},
        db=db,
        current_user=_user(),
    )

    assert seen["limit"] == 1


async def test_search_arxiv_max_results_is_at_least_one(monkeypatch) -> None:
    from src.services.arxiv import arxiv_service

    seen: Dict[str, Any] = {}

    class _Service:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def search_papers(self, **kwargs: Any) -> list[Any]:
            seen.update(kwargs)
            return []

    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(arxiv_service, "ArXivIngestionService", _Service)
    monkeypatch.setattr(tools_impl, "_arxiv_cache_get", lambda _key: None)
    monkeypatch.setattr(tools_impl, "_arxiv_cache_set", lambda *_args: None)
    monkeypatch.setattr(tools_impl, "_arxiv_redis_get", _none)
    monkeypatch.setattr(tools_impl, "_arxiv_redis_set", _none)

    await tools_impl._dispatch_tool("search_arxiv", {"query": "q", "max_results": -3})

    assert seen["max_results"] == 1


@pytest.mark.parametrize(
    "tool_name, args, method, bounded",
    [
        (
            "explore_entity_neighborhood",
            {"entity_id": "e1", "max_depth": -1, "limit": -5},
            "get_neighborhood",
            ("max_depth", "limit"),
        ),
        (
            "find_entity_paths",
            {"source_entity_id": "a", "target_entity_id": "b", "max_depth": -2},
            "find_paths",
            ("max_depth",),
        ),
    ],
)
async def test_graph_depth_and_limit_are_at_least_one(
    monkeypatch, tool_name: str, args: Dict[str, Any], method: str, bounded: Any
) -> None:
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    seen: Dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return [] if method == "find_paths" else {"entities": [], "relationships": []}

    monkeypatch.setattr(knowledge_graph_service, method, _capture)

    await tools_impl._dispatch_tool(tool_name, args, current_user=_user())

    assert {name: seen[name] for name in bounded} == {name: 1 for name in bounded}
