"""OpenAlex citation chasing: bounded metadata requests, recorded as receipts (GOO-300).

Every HTTP call goes through ``httpx.MockTransport``; nothing touches the network.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_source import ResearchSource
from src.schemas.research_engine import CitationChaseRequest
from src.services.research_engine import corpus_service, search_import
from src.services.research_engine.connectors import openalex_connector, provider_http
from src.services.research_engine.connectors.base import SearchTrace
from src.services.research_engine.connectors.openalex_connector import OpenAlexConnector
from src.services.research_engine.step_executor import MAX_CONNECTOR_RESULTS


def _work(work_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "display_name": f"Work {work_id}",
        "doi": f"https://doi.org/10.1000/{work_id.lower()}",
        "publication_date": "2020-01-02",
        "best_oa_location": {"pdf_url": "https://publisher.example/paper.pdf"},
        **extra,
    }


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())
    monkeypatch.setattr(provider_http.asyncio, "sleep", AsyncMock())


def _connector(
    respond: Callable[[httpx.Request], httpx.Response],
) -> tuple[OpenAlexConnector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # No PDF, landing page or other host is ever fetched: metadata only.
        assert request.url.host == "api.openalex.org"
        seen.append(request)
        return respond(request)

    return OpenAlexConnector(transport=httpx.MockTransport(handler)), seen


def _trace(limit: int) -> SearchTrace:
    return SearchTrace(
        execution_id=str(uuid4()), provider="openalex", requested_limit=limit
    )


@pytest.mark.asyncio
async def test_backward_fetches_referenced_ids_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openalex_connector, "ID_BATCH_SIZE", 3)
    refs = [f"W{n}" for n in range(10, 17)]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works/W1":
            return httpx.Response(
                200,
                json={
                    "id": "https://openalex.org/W1",
                    "referenced_works": [f"https://openalex.org/{r}" for r in refs],
                },
            )
        ids = request.url.params["filter"].removeprefix("openalex_id:").split("|")
        return httpx.Response(200, json={"results": [_work(i) for i in ids]})

    connector, seen = _connector(respond)
    trace = _trace(5)
    works = await connector.citations("W1", "backward", 5, search_trace=trace)

    assert [w["id"].rsplit("/", 1)[-1] for w in works] == refs[:5]
    assert [r.url.params.get("filter") for r in seen] == [
        None,
        "openalex_id:W10|W11|W12",
        "openalex_id:W13|W14",
    ]
    assert len(trace.pages) == 3
    assert trace.pages[1]["request"]["params"]["filter"] == "openalex_id:W10|W11|W12"
    assert trace.as_receipt(returned_count=5)["completion"] == "cap_reached"


@pytest.mark.asyncio
async def test_forward_uses_cites_filter_and_stops_at_limit() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/works/doi:"):
            return httpx.Response(200, json={"id": "https://openalex.org/W00"})
        page = int(request.url.params["cursor"] != "*")
        return httpx.Response(
            200,
            json={
                "results": [_work(f"W{page}{n}") for n in range(3)],
                "meta": {"count": 99, "next_cursor": f"c{page + 1}"},
            },
        )

    connector, seen = _connector(respond)
    works = await connector.citations(
        "doi:10.1000/seed", "forward", 4, search_trace=_trace(4)
    )

    assert len(works) == 4
    # A DOI seed is resolved to its OpenAlex id first, then cites: pages.
    assert seen[0].url.path == "/works/doi:10.1000/seed"
    assert [r.url.params.get("filter") for r in seen[1:]] == [
        "cites:W00",
        "cites:W00",
    ]
    assert [r.url.params.get("per_page") for r in seen[1:]] == ["4", "1"]


@pytest.mark.asyncio
async def test_no_pdf_url_is_fetched() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works/W1":
            return httpx.Response(
                200,
                json={
                    "id": "https://openalex.org/W1",
                    "referenced_works": ["https://openalex.org/W2"],
                },
            )
        return httpx.Response(200, json={"results": [_work("W2")], "meta": {}})

    connector, seen = _connector(respond)
    for direction in ("backward", "forward"):
        works = await connector.citations(
            "W1", direction, 5, search_trace=_trace(5)  # type: ignore[arg-type]
        )
        assert works and works[0]["best_oa_location"]["pdf_url"]
    assert {r.url.host for r in seen} == {"api.openalex.org"}


def test_chase_limit_mirrors_connector_cap() -> None:
    field = CitationChaseRequest.model_fields["max_results"]
    assert field.default == MAX_CONNECTOR_RESULTS
    with pytest.raises(ValueError):
        CitationChaseRequest(
            seed_report_id=uuid4(),
            direction="backward",
            max_results=MAX_CONNECTOR_RESULTS + 1,
            idempotency_key="k",
        )


# --- service: three-phase chase receipts -------------------------------------


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [
        model.__table__  # type: ignore[attr-defined]
        for model in (
            ResearchProtocol,
            ResearchProtocolVersion,
            ResearchDecisionStream,
            ResearchDecisionEvent,
            ResearchStudy,
            ResearchReport,
            ResearchReportIdentifier,
            ResearchSource,
            ResearchReportObservation,
            ResearchImportReceipt,
            ResearchImportRecord,
        )
    ]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        ),
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_report(
    factory: async_sessionmaker[AsyncSession], **identifiers: str
) -> tuple[UUID, UUID]:
    collection_id, report_id = uuid4(), uuid4()
    async with factory() as db:
        db.add(
            ResearchReport(
                id=report_id, collection_id=collection_id, title_snapshot="S"
            )
        )
        await db.flush()
        for kind, value in identifiers.items():
            db.add(
                ResearchReportIdentifier(
                    collection_id=collection_id,
                    report_id=report_id,
                    kind=kind,
                    value=value,
                )
            )
        await db.commit()
    return collection_id, report_id


def _resolve(
    monkeypatch: pytest.MonkeyPatch, collection_id: UUID
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def resolve(*args: Any, **_kwargs: Any) -> Any:
        calls.append(args)
        return SimpleNamespace(collection=SimpleNamespace(id=collection_id))

    monkeypatch.setattr(corpus_service, "resolve_project", resolve)
    return calls


def _request(report_id: UUID, **overrides: Any) -> CitationChaseRequest:
    return CitationChaseRequest(
        **{
            "seed_report_id": report_id,
            "direction": "backward",
            "max_results": 5,
            "idempotency_key": "chase-1",
            **overrides,
        }
    )


class _FakeConnector:
    def __init__(
        self,
        works: list[dict[str, Any]] | None = None,
        before: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.works = works
        self.before = before
        self.calls: list[tuple[str, str, int]] = []

    async def citations(
        self,
        work_id: str,
        direction: str,
        max_results: int,
        *,
        search_trace: Any,
        into: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((work_id, direction, max_results))
        if self.before is not None:
            await self.before()
        await search_trace.begin_request(
            OpenAlexConnector.endpoint, {"filter": f"cites:{work_id}"}
        )
        if self.works is None:
            raise httpx.ConnectError("provider down")
        await search_trace.record_page(
            endpoint=OpenAlexConnector.endpoint,
            params={"filter": f"cites:{work_id}"},
            documents=[],
            has_more=False,
        )
        if into is not None:
            into.extend(self.works)
        return self.works


async def _count(db: AsyncSession, model: Any) -> int:
    return cast(
        int, (await db.execute(select(func.count()).select_from(model))).scalar_one()
    )


@pytest.mark.asyncio
async def test_chase_stores_accepted_records_and_replays(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, openalex="W1", doi="10.1/x")
    calls = _resolve(monkeypatch, collection_id)
    connector = _FakeConnector([_work("W2"), _work("W3")])

    async with factory() as db:
        receipt, created = await corpus_service.chase_citations(
            db,
            project_id=collection_id,
            user_id=uuid4(),
            data=_request(seed),
            connector=connector,
        )
        await db.commit()
        again, created_again = await corpus_service.chase_citations(
            db,
            project_id=collection_id,
            user_id=uuid4(),
            data=_request(seed),
            connector=connector,
        )
        records = (await db.execute(select(ResearchImportRecord))).scalars().all()

    assert (created, created_again, again.id, again.replayed) == (
        True,
        False,
        receipt.id,
        True,
    )
    assert connector.calls == [("W1", "backward", 5)]
    assert len(calls) == 3  # EDIT before the network, again after it; replay once
    assert receipt.kind == "citation_chase"
    assert receipt.declared.model_dump(mode="json") == {
        "seed_report_id": str(seed),
        "direction": "backward",
        "requested_limit": 5,
        "redistribution": "allowed",
    }
    assert receipt.observed["provider"] == "openalex"
    assert receipt.observed["completion"] == "exhausted"
    assert receipt.observed["trace"]["pages"][0]["request"]["params"] == {
        "filter": "cites:W1"
    }
    assert (receipt.accepted_count, receipt.rejected_count) == (2, 0)
    assert all(r.status == "accepted" and r.report_id for r in records)
    assert {r.parsed["identifiers"]["openalex"] for r in records} == {"W2", "W3"}

    with pytest.raises(HTTPException) as conflict:
        async with factory() as db:
            await corpus_service.chase_citations(
                db,
                project_id=collection_id,
                user_id=uuid4(),
                data=_request(seed, direction="forward"),
                connector=connector,
            )
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_seed_without_ids_is_422(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, pmid="123")
    _resolve(monkeypatch, collection_id)
    connector = _FakeConnector([])

    async with factory() as db:
        with pytest.raises(HTTPException) as unresolvable:
            await corpus_service.chase_citations(
                db,
                project_id=collection_id,
                user_id=uuid4(),
                data=_request(seed),
                connector=connector,
            )
        with pytest.raises(HTTPException) as foreign:
            await corpus_service.chase_citations(
                db,
                project_id=collection_id,
                user_id=uuid4(),
                data=_request(uuid4()),
                connector=connector,
            )

    assert unresolvable.value.status_code == 422
    assert cast(dict, unresolvable.value.detail)["code"] == "seed_not_resolvable"
    assert foreign.value.status_code == 404
    assert connector.calls == []


@pytest.mark.asyncio
async def test_failed_provider_still_records_receipt(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, doi="10.1000/seed")
    _resolve(monkeypatch, collection_id)
    connector = _FakeConnector(None)

    async with factory() as db:
        receipt, created = await corpus_service.chase_citations(
            db,
            project_id=collection_id,
            user_id=uuid4(),
            data=_request(seed, direction="forward"),
            connector=connector,
        )
        assert await _count(db, ResearchImportRecord) == 0

    assert created is True
    assert connector.calls == [("doi:10.1000/seed", "forward", 5)]
    assert receipt.parsed_count == 0
    assert receipt.observed["completion"] == "failed"
    assert receipt.observed["error_type"] == "ConnectError"
    assert receipt.observed["trace"]["pages"][0]["page_status"] == "failed"


@pytest.mark.asyncio
async def test_pages_fetched_before_a_failure_are_kept(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, openalex="W1")
    _resolve(monkeypatch, collection_id)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["cursor"] != "*":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "results": [_work("W2"), _work("W3")],
                "meta": {"next_cursor": "c1"},
            },
        )

    connector, _seen = _connector(respond)
    async with factory() as db:
        receipt, _ = await corpus_service.chase_citations(
            db,
            project_id=collection_id,
            user_id=uuid4(),
            data=_request(seed, direction="forward"),
            connector=connector,
        )

    assert receipt.accepted_count == 2
    assert receipt.observed["completion"] == "partial_failure"
    assert receipt.observed["trace"]["status"] == "partial"
    assert receipt.observed["trace"]["returned_count"] == 2
    assert receipt.observed["error_type"] == "HTTPStatusError"


@pytest.mark.asyncio
async def test_seed_merged_during_the_network_call_is_409(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, openalex="W1")
    _resolve(monkeypatch, collection_id)
    survivor = uuid4()

    async def merge_seed() -> None:
        async with factory() as other:
            other.add(
                ResearchReport(
                    id=survivor, collection_id=collection_id, title_snapshot="T"
                )
            )
            await other.flush()
            report = await other.get(ResearchReport, seed)
            cast(Any, report).merged_into_report_id = survivor
            await other.commit()

    async with factory() as db:
        with pytest.raises(HTTPException) as merged:
            await corpus_service.chase_citations(
                db,
                project_id=collection_id,
                user_id=uuid4(),
                data=_request(seed),
                connector=_FakeConnector([_work("W2")], before=merge_seed),
            )
        assert await _count(db, ResearchImportReceipt) == 0

    assert merged.value.status_code == 409


@pytest.mark.asyncio
async def test_oversized_chased_work_is_rejected_with_bounded_raw(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    collection_id, seed = await _seed_report(factory, openalex="W1")
    _resolve(monkeypatch, collection_id)
    monkeypatch.setattr(search_import, "MAX_RECORD_BYTES", 600)
    big = _work("W2", abstract_inverted_index={"x" * 900: [0]})

    async with factory() as db:
        receipt, _ = await corpus_service.chase_citations(
            db,
            project_id=collection_id,
            user_id=uuid4(),
            data=_request(seed),
            connector=_FakeConnector([big, _work("W3")]),
        )
        records = (
            (
                await db.execute(
                    select(ResearchImportRecord).order_by(
                        ResearchImportRecord.record_index
                    )
                )
            )
            .scalars()
            .all()
        )

    assert (receipt.accepted_count, receipt.rejected_count) == (1, 1)
    assert records[0].rejection_reason == "record_too_large"
    assert len(records[0].raw.encode()) <= 600
