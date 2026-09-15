"""Discovery must retain evidence, identify duplicates, and report coverage."""

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.step_executor import StepExecutor


@pytest.mark.asyncio
async def test_search_merges_doi_and_preserves_provider_evidence_on_resume() -> None:
    a = SourceDocument(
        connector_type="crossref",
        external_id="10.1234/ABC",
        title="A paper",
        metadata={"doi": "https://doi.org/10.1234/ABC"},
    )
    b = SourceDocument(
        connector_type="pubmed",
        external_id="123",
        title="A paper",
        abstract="The measured outcome was 42%.",
        metadata={"doi": "10.1234/abc", "pmid": "123"},
    )
    executor = StepExecutor(
        {
            "crossref": AsyncMock(search=AsyncMock(return_value=[a])),
            "pubmed": AsyncMock(search=AsyncMock(return_value=[b])),
        },
        {},
    )
    events = [
        event
        async for event in WorkflowEngine(executor).run(
            {
                "steps": [
                    {"type": "search", "params": {"sources": ["crossref", "pubmed"]}}
                ]
            },
            uuid4(),
        )
    ]
    output = next(e["output"] for e in events if e["event"] == "step_complete")
    records = output["source_records"]
    assert len(records) == 1
    assert records[0]["abstract"] == b.abstract
    assert records[0]["metadata"]["identifiers"]["doi"] == "10.1234/abc"
    assert {p["connector_type"] for p in records[0]["metadata"]["provenance"]} == {
        "crossref",
        "pubmed",
    }
    assert records[0]["evidence_level"] == "abstract"
    resumed = [
        event
        async for event in WorkflowEngine(executor).run(
            {"steps": [{"type": "export"}]},
            uuid4(),
            initial_context=json.loads(json.dumps(output)),
        )
    ]
    exported = next(
        e["output"]["exported"] for e in resumed if e["event"] == "step_complete"
    )
    assert exported["source_records"] == records


@pytest.mark.asyncio
async def test_partial_failure_is_visible_without_exposing_exception_secrets() -> None:
    executor = StepExecutor(
        {
            "pubmed": AsyncMock(
                search=AsyncMock(side_effect=RuntimeError("api_key=secret"))
            ),
            "crossref": AsyncMock(search=AsyncMock(return_value=[])),
        },
        {},
    )
    result = await executor.execute(
        {"type": "search", "params": {"sources": ["pubmed", "crossref"]}}, {}
    )
    assert result.output["coverage"]["partial"] is True
    assert result.output["coverage"]["providers"]["pubmed"]["status"] == "failed"
    assert "secret" not in json.dumps(result.output)


@pytest.mark.asyncio
async def test_unknown_provider_does_not_silently_complete() -> None:
    with pytest.raises(ValueError, match="Unknown research source"):
        await StepExecutor({}, {}).execute(
            {"type": "search", "params": {"sources": ["typo"]}}, {}
        )


def test_distinct_arxiv_versions_and_equal_titles_are_not_merged() -> None:
    from src.services.research_engine.discovery import prepare_sources

    docs = [
        SourceDocument(
            connector_type="arxiv",
            external_id=f"https://arxiv.org/abs/2401.12345v{v}",
            title="Same",
        )
        for v in (1, 2)
    ]
    docs.append(SourceDocument(connector_type="crossref", title="Same"))
    assert len(prepare_sources(docs)) == 3


def test_identifier_bridge_merges_three_providers_but_not_conflicting_dois() -> None:
    from src.services.research_engine.discovery import prepare_sources

    docs = [
        SourceDocument(
            connector_type="crossref", title="A", metadata={"doi": "10.1234/abc"}
        ),
        SourceDocument(connector_type="pubmed", external_id="123", title="B"),
        SourceDocument(
            connector_type="openalex",
            external_id="W1",
            title="C",
            metadata={"doi": "10.1234/abc", "pmid": "123"},
        ),
        SourceDocument(
            connector_type="semantic_scholar",
            external_id="s2",
            title="D",
            metadata={"doi": "10.1234/different", "pmid": "123"},
        ),
    ]
    results = prepare_sources(docs)
    assert len(results) == 2
    assert len(results[0].metadata["provenance"]) == 3
    assert results[1].metadata["identifiers"]["doi"] == "10.1234/different"


def test_local_search_snippets_do_not_claim_full_text_access() -> None:
    from src.services.research_engine.discovery import source_records

    records = source_records(
        [
            SourceDocument(
                connector_type="rag_store",
                title="Local document",
                full_text="A retrieved snippet",
            )
        ]
    )
    assert records[0]["evidence_level"] == "excerpt"


@pytest.mark.asyncio
async def test_timeout_is_partial_and_cancelled_by_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.services.research_engine import discovery

    monkeypatch.setattr(discovery, "SEARCH_TIMEOUT_SECONDS", 0.01)
    stopped = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> list[SourceDocument]:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return []

    connectors: dict[str, SourceConnector] = {
        "slow": AsyncMock(search=hang),
        "fast": AsyncMock(search=AsyncMock(return_value=[])),
    }
    _, coverage = await discovery.search_sources(
        connectors, ["slow", "fast"], "test", 1
    )
    assert stopped.is_set()
    assert coverage["providers"]["slow"]["error_type"] == "TimeoutError"
    monkeypatch.setattr(discovery, "SEARCH_TIMEOUT_SECONDS", 10)
    task = asyncio.create_task(
        discovery.search_sources(connectors, ["slow"], "test", 1)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_openalex_reconstructs_abstract_and_uses_header_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors.openalex_connector import (
        OpenAlexConnector,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert "api_key" not in request.url.params
        assert int(request.url.params["per_page"]) <= 100
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W123",
                        "display_name": "A paper",
                        "doi": "https://doi.org/10.1234/abc",
                        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123"},
                        "abstract_inverted_index": {"result": [1], "The": [0]},
                        "authorships": [{"author": {"display_name": "A. Author"}}],
                        "primary_location": {
                            "landing_page_url": "https://doi.org/10.1234/abc"
                        },
                        "open_access": {"is_oa": False},
                        "publication_date": "2025-01-01",
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    docs = await OpenAlexConnector(api_key="test-key").search("test", max_results=200)
    assert docs[0].abstract == "The result"
    assert docs[0].full_text is None
    assert docs[0].metadata["pmid"] == "https://pubmed.ncbi.nlm.nih.gov/123"


def test_http_registry_uses_actual_pubmed_and_openalex() -> None:
    from src.api.research_engine.runs import _build_connectors

    connectors = _build_connectors(organization_id="org")
    assert type(connectors["pubmed"]).__name__ == "PubMedConnector"
    assert type(connectors["openalex"]).__name__ == "OpenAlexConnector"
    assert "crossref" in connectors


@pytest.mark.asyncio
async def test_http_local_search_fails_closed_without_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.research_engine.runs import _search_rag_store
    from src.services.search.hybrid_search_service import hybrid_search_service

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Missing organization must not call document search")

    monkeypatch.setattr(hybrid_search_service, "search", forbidden)

    assert await _search_rag_store("private", organization_id=None) == {"results": []}


@pytest.mark.asyncio
async def test_global_source_selection_survives_multiple_search_steps() -> None:
    connector = AsyncMock(
        search=AsyncMock(
            return_value=[SourceDocument(connector_type="pubmed", title="A title")]
        )
    )
    executor = StepExecutor({"pubmed": connector}, {})
    events = [
        e
        async for e in WorkflowEngine(executor).run(
            {
                "parameters": {"sources": ["pubmed"]},
                "steps": [{"type": "search"}, {"type": "search"}],
            },
            uuid4(),
        )
    ]
    assert events[-1]["event"] == "run_complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_step_override_does_not_replace_global_sources(resume: bool) -> None:
    connectors: dict[str, SourceConnector] = {
        name: AsyncMock(
            search=AsyncMock(
                return_value=[SourceDocument(connector_type=name, title=name)]
            )
        )
        for name in ("pubmed", "openalex")
    }
    engine = WorkflowEngine(StepExecutor(connectors, {}))
    blueprint: dict[str, Any] = {
        "parameters": {"sources": ["openalex"]},
        "steps": [
            {"type": "search", "parameters": {"sources": ["pubmed"]}},
            {"type": "search"},
        ],
    }
    if resume:
        first = await engine.step_executor.execute(
            blueprint["steps"][0], blueprint["parameters"]
        )
        events = [
            e
            async for e in engine.run(
                blueprint,
                uuid4(),
                start_from_step=1,
                initial_context=json.loads(json.dumps(first.output)),
            )
        ]
    else:
        events = [e async for e in engine.run(blueprint, uuid4())]
    completed = [e["output"] for e in events if e["event"] == "step_complete"]
    assert events[-1]["event"] == "run_complete"
    assert completed[-1]["selected_sources"] == ["openalex"]
    assert completed[-1]["source_records"][0]["connector_type"] == "openalex"


@pytest.mark.asyncio
async def test_all_failed_providers_fail_the_run() -> None:
    executor = StepExecutor(
        {"pubmed": AsyncMock(search=AsyncMock(side_effect=RuntimeError("secret")))}, {}
    )
    events = [
        e
        async for e in WorkflowEngine(executor).run(
            {
                "steps": [{"type": "search", "params": {"sources": ["pubmed"]}}],
            },
            uuid4(),
        )
    ]
    assert events[-1]["event"] == "run_failed"
    assert "secret" not in json.dumps(events)


@pytest.mark.asyncio
async def test_semantic_scholar_follows_next_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http
    from src.services.research_engine.connectors.semantic_scholar_connector import (
        SemanticScholarConnector,
    )

    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        if offset == 0:
            return httpx.Response(
                200, json={"data": [{"paperId": "first", "title": "First"}], "next": 1}
            )
        assert offset == 1
        return httpx.Response(
            200, json={"data": [{"paperId": "second", "title": "Second"}]}
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    result = await SemanticScholarConnector().search("test", max_results=2)
    assert [doc.external_id for doc in result] == ["first", "second"]
