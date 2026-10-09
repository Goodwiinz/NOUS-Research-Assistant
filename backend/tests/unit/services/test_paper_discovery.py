"""Discovery must retain evidence, identify duplicates, and report coverage."""

import copy
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.step_executor import StepExecutor


class _FrozenDateTime:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 28, 6, 0, tzinfo=tz)


def _reference_prepare_sources(
    documents: list[SourceDocument],
) -> list[SourceDocument]:
    """Copy the original full-scan algorithm as an equivalence oracle."""
    from src.services.research_engine.discovery import _identifiers

    merged: list[SourceDocument] = []
    now = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc).isoformat()
    for original in documents:
        if not original.title.strip():
            continue
        source = copy.deepcopy(original)
        ids = _identifiers(source)
        provenance = {**asdict(source), "retrieved_at": now}
        source.metadata["identifiers"] = ids
        source.metadata["provenance"] = [provenance]
        for candidate in list(merged):
            if (candidate.connector_type == "rag_store") != (
                source.connector_type == "rag_store"
            ):
                continue
            other = candidate.metadata["identifiers"]
            shared = ids.keys() & other.keys()
            if not shared or any(ids[key] != other[key] for key in shared):
                continue
            ids = {**other, **ids}
            candidate.metadata["identifiers"] = ids
            candidate.metadata["provenance"].extend(source.metadata["provenance"])
            for attribute in ("abstract", "full_text", "url", "authors"):
                if not getattr(candidate, attribute):
                    setattr(candidate, attribute, getattr(source, attribute))
            source = candidate
            merged.remove(candidate)
        source.compute_hash()
        merged.append(source)
    return merged


def _unique_identifier_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            connector_type="openalex",
            external_id=f"https://openalex.org/W{index}",
            title=f"Unique paper {index}",
            abstract=f"Controlled abstract {index}",
        )
        for index in range(200)
    ]


def _three_provider_bridge_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            connector_type="crossref",
            external_id="10.1234/bridge",
            title="Crossref bridge",
        ),
        SourceDocument(
            connector_type="pubmed",
            external_id="12345",
            title="PubMed bridge",
        ),
        SourceDocument(
            connector_type="openalex",
            external_id="W-BRIDGE",
            title="OpenAlex bridge",
            metadata={"doi": "10.1234/bridge", "pmid": "12345"},
        ),
    ]


def _conflicting_identifier_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            connector_type="openalex",
            external_id="W1",
            title="First conflict",
            metadata={"doi": "10.1234/first", "pmid": "999"},
        ),
        SourceDocument(
            connector_type="semantic_scholar",
            external_id="S2",
            title="Second conflict",
            metadata={"doi": "10.1234/second", "pmid": "999"},
        ),
    ]


def _arxiv_revision_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            connector_type="arxiv",
            external_id=f"https://arxiv.org/abs/2401.12345v{revision}",
            title="Revision-sensitive paper",
        )
        for revision in (1, 2)
    ]


def _local_public_partition_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            connector_type="crossref",
            external_id="10.1234/shared",
            title="Public record",
        ),
        SourceDocument(
            connector_type="rag_store",
            external_id="workspace-1",
            title="Workspace copy one",
            metadata={"doi": "10.1234/shared"},
        ),
        SourceDocument(
            connector_type="rag_store",
            external_id="workspace-1",
            title="Workspace copy two",
            abstract="Workspace evidence",
            metadata={"doi": "10.1234/shared"},
        ),
    ]


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
async def test_search_step_without_id_checkpoints_under_the_event_step_id() -> None:
    """Template steps carry no ``id``. The journal checkpoint, the completed
    event and the pinned strategy must name the same step, or
    ``finalize_search_step`` rejects every template run."""
    checkpointed: set[str] = set()

    async def on_search_page(*, step_id, **_kwargs):
        checkpointed.add(step_id)

    executor = StepExecutor(
        {
            "crossref": AsyncMock(
                search=AsyncMock(
                    return_value=[
                        SourceDocument(
                            connector_type="crossref", external_id="10.1/x", title="X"
                        )
                    ]
                )
            )
        },
        {},
        on_search_page=on_search_page,
    )
    events = [
        event
        async for event in WorkflowEngine(executor).run(
            {
                "parameters": {"query": "q"},
                "steps": [{"type": "search", "params": {"sources": ["crossref"]}}],
            },
            uuid4(),
        )
    ]
    complete = next(e for e in events if e["event"] == "step_complete")
    strategy = complete["output"]["coverage"]["search_strategy"]

    assert checkpointed == {complete["step_id"]}
    assert strategy["step_id"] == complete["step_id"]


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


def test_source_record_ids_are_stable_for_a_run_step_strategy() -> None:
    from src.services.research_engine.discovery import prepare_sources, source_records

    documents = prepare_sources(
        [
            SourceDocument(
                connector_type="crossref",
                external_id="10.1234/abc",
                title="Stable paper",
            )
        ]
    )
    namespace = str(uuid4())
    first = source_records(documents, id_namespace=namespace)
    second = source_records(documents, id_namespace=namespace)
    assert first[0]["source_id"] == second[0]["source_id"]


@pytest.mark.asyncio
async def test_search_trace_checkpoints_requests_and_reuses_page_identity() -> None:
    from src.services.research_engine.connectors.base import SearchTrace

    updates = []

    async def checkpoint(provider, execution_id, page):
        updates.append((provider, execution_id, page.copy()))

    execution_id = str(uuid4())
    trace = SearchTrace(
        execution_id=execution_id,
        provider="openalex",
        requested_limit=1,
        on_page_update=checkpoint,
    )
    await trace.begin_request("https://api.openalex.org/works", {"query": "q"})
    await trace.record_response(httpx.Response(200))
    await trace.record_page(
        endpoint="https://api.openalex.org/works",
        params={"query": "q"},
        response=httpx.Response(200),
        documents=[
            SourceDocument(connector_type="openalex", external_id="W1", title="A paper")
        ],
        has_more=False,
    )

    retry = SearchTrace(
        execution_id=execution_id,
        provider="openalex",
        requested_limit=1,
        on_page_update=checkpoint,
    )
    await retry.begin_request("https://api.openalex.org/works", {"query": "q"})

    assert updates[0][2]["page_status"] == "requested"
    assert updates[2][2]["page_status"] == "completed"
    assert updates[0][2]["page_id"] == updates[-1][2]["page_id"]
    assert updates[0][2]["attempt_id"] != updates[-1][2]["attempt_id"]


@pytest.mark.asyncio
async def test_pubmed_empty_id_list_does_not_claim_more_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import pubmed_connector
    from src.services.research_engine.connectors.base import SearchTrace

    async def respond(*args, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", "https://eutils.ncbi.nlm.nih.gov/"),
            text="<eSearchResult><Count>120</Count><IdList /></eSearchResult>",
        )

    monkeypatch.setattr(pubmed_connector, "get", respond)
    trace = SearchTrace(
        execution_id=str(uuid4()), provider="pubmed", requested_limit=10
    )
    documents = await pubmed_connector.PubMedConnector().search(
        "approved query", max_results=10, search_trace=trace
    )

    assert documents == []
    receipt = trace.as_receipt(returned_count=0)
    assert receipt["completion"] == "exhausted"
    assert receipt["pages"][0]["response"]["has_more"] is False


@pytest.mark.asyncio
async def test_legacy_connector_gets_a_durable_single_request_receipt() -> None:
    from src.services.research_engine.discovery import search_sources

    updates = []

    class LegacyConnector:
        async def search(self, query: str, max_results: int) -> list[SourceDocument]:
            assert query == "approved query"
            assert max_results == 1
            return [
                SourceDocument(connector_type="legacy", external_id="P1", title="Paper")
            ]

    async def checkpoint(provider, execution_id, page):
        updates.append(page["page_status"])

    documents, coverage = await search_sources(
        {"legacy": LegacyConnector()},
        ["legacy"],
        "approved query",
        1,
        on_page_update=checkpoint,
    )

    assert len(documents) == 1
    assert coverage["providers"]["legacy"]["status"] == "ok"
    assert updates == ["requested", "completed"]


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


@pytest.mark.parametrize(
    ("case_name", "documents_factory", "expected_count"),
    [
        ("200 unique identifiers", _unique_identifier_documents, 200),
        ("three-provider bridge", _three_provider_bridge_documents, 1),
        ("conflicting shared identifiers", _conflicting_identifier_documents, 2),
        ("arxiv revisions", _arxiv_revision_documents, 2),
        ("rag_store/public isolation", _local_public_partition_documents, 2),
    ],
)
def test_prepare_sources_fast_path_matches_original_full_scan(
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    documents_factory: Callable[[], list[SourceDocument]],
    expected_count: int,
) -> None:
    from src.services.research_engine import discovery

    monkeypatch.setattr(discovery, "datetime", _FrozenDateTime)
    documents = documents_factory()

    expected = _reference_prepare_sources(documents)
    actual = discovery.prepare_sources(documents)

    assert len(actual) == expected_count, case_name
    assert [asdict(source) for source in actual] == [
        asdict(source) for source in expected
    ]
    assert all(source.content_hash for source in actual)


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
    assert records[0]["evidence_level"] == "workspace_document"


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
    from src.services.research_engine.connectors.base import SearchTrace
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
    checkpoints = []

    async def checkpoint(provider, execution_id, page):
        checkpoints.append(page.copy())

    trace = SearchTrace(
        execution_id=str(uuid4()),
        provider="openalex",
        requested_limit=200,
        on_page_update=checkpoint,
    )
    docs = await OpenAlexConnector(api_key="test-key").search(
        "test", max_results=200, search_trace=trace
    )
    assert docs[0].abstract == "The result"
    assert docs[0].full_text is None
    assert docs[0].metadata["pmid"] == "https://pubmed.ncbi.nlm.nih.gov/123"
    assert checkpoints[0]["page_status"] == "requested"
    assert checkpoints[-1]["page_status"] == "completed"
    assert checkpoints[-1]["request"]["params"]["search"] == "test"
    assert checkpoints[-1]["response"]["parsed_count"] == 1


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
async def test_all_failed_providers_fail_the_run_but_keep_receipts() -> None:
    pages: list[dict[str, Any]] = []

    async def persist(**kwargs: Any) -> None:
        pages.append(kwargs)

    executor = StepExecutor(
        {
            "pubmed": AsyncMock(search=AsyncMock(side_effect=RuntimeError("secret"))),
            "crossref": AsyncMock(
                search=AsyncMock(side_effect=RuntimeError("offline"))
            ),
        },
        {},
        strategy_context={"run_id": str(uuid4())},
        on_search_page=persist,
    )
    events = [
        e
        async for e in WorkflowEngine(executor).run(
            {
                "steps": [
                    {
                        "id": "discover",
                        "type": "search",
                        "params": {"sources": ["pubmed", "crossref"]},
                    },
                    {"id": "screen", "type": "export"},
                ],
            },
            uuid4(),
        )
    ]
    assert events[-1]["event"] == "run_failed"
    assert not any(event["event"] == "step_complete" for event in events)
    # Each provider checkpointed its failed request before the run failed.
    final_pages = {page["provider"]: page["page"] for page in pages}
    assert set(final_pages) == {"pubmed", "crossref"}
    assert all(page["page_status"] == "failed" for page in final_pages.values())
    assert "secret" not in json.dumps(events)
    assert "secret" not in json.dumps(pages)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("max_results", [0, 201])
async def test_semantic_scholar_rejects_result_limits_outside_provider_bounds(
    max_results: int,
) -> None:
    from src.services.research_engine.connectors.semantic_scholar_connector import (
        SemanticScholarConnector,
    )

    with pytest.raises(ValueError, match="between 1 and 200"):
        await SemanticScholarConnector().search("test", max_results=max_results)


@pytest.mark.asyncio
async def test_semantic_scholar_trace_captures_each_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http
    from src.services.research_engine.connectors.base import SearchTrace
    from src.services.research_engine.connectors.semantic_scholar_connector import (
        SemanticScholarConnector,
    )

    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())

    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        if offset == 0:
            return httpx.Response(200, json={"data": [{"paperId": "first"}], "next": 1})
        return httpx.Response(200, json={"data": [{"paperId": "second"}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    trace = SearchTrace(
        execution_id=str(uuid4()), provider="semantic_scholar", requested_limit=2
    )

    docs = await SemanticScholarConnector().search(
        "test", max_results=2, search_trace=trace
    )

    assert [doc.external_id for doc in docs] == ["first", "second"]
    assert len(trace.pages) == 2
    assert trace.pages[0]["response"]["has_more"] is True
    assert trace.pages[1]["response"]["has_more"] is False


@pytest.mark.asyncio
async def test_semantic_scholar_never_returns_more_than_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http
    from src.services.research_engine.connectors.semantic_scholar_connector import (
        SemanticScholarConnector,
    )

    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"paperId": "one", "title": "One"},
                    {"paperId": "two", "title": "Two"},
                    {"paperId": "three", "title": "Three"},
                ],
                "next": 3,
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    result = await SemanticScholarConnector().search("test", max_results=2)
    assert [doc.external_id for doc in result] == ["one", "two"]
