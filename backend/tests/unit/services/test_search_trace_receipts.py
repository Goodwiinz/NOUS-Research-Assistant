"""Focused coverage for durable provider-search receipt behavior."""

import asyncio
import hashlib
from typing import Any, List
from uuid import uuid4

import pytest

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
    redact_search_values,
)


def test_redact_search_values_handles_nested_secrets_and_other_objects() -> None:
    class OpaqueValue:
        def __str__(self) -> str:
            return "opaque"

    result = redact_search_values(
        {
            "query": "approved terms",
            "api_key": "private",
            "filters": ({"access_token": "private"}, OpaqueValue()),
            "count": 2,
        }
    )

    assert result == {
        "query": "approved terms",
        "api_key": "[REDACTED]",
        "filters": [{"access_token": "[REDACTED]"}, "opaque"],
        "count": 2,
    }


@pytest.mark.asyncio
async def test_receipt_noops_without_an_outstanding_page() -> None:
    trace = SearchTrace(execution_id=str(uuid4()), provider="test", requested_limit=1)

    await trace.record_response(object())
    await trace.mark_interrupted()

    assert trace.pages == []


@pytest.mark.asyncio
async def test_record_page_starts_request_and_hashes_long_cursor() -> None:
    trace = SearchTrace(execution_id=str(uuid4()), provider="test", requested_limit=1)
    cursor = "c" * 513

    await trace.record_page(
        endpoint=None,
        params={"query": "approved terms"},
        next_cursor=cursor,
    )

    page = trace.pages[0]
    assert page["request"]["endpoint"] == "unknown"
    assert (
        page["response"]["next_cursor"]
        == "sha256:" + hashlib.sha256(cursor.encode()).hexdigest()
    )
    assert page["page_status"] == "completed"


@pytest.mark.parametrize(
    ("has_more", "returned_count", "expected"),
    [
        (True, 5, "cap_reached"),
        (True, 3, "more_available"),
        (False, 1, "exhausted"),
        (None, 5, "cap_reached"),
        (None, 1, "unknown"),
    ],
)
def test_receipt_completion_reflects_provider_exhaustion_and_cap(
    has_more: bool | None, returned_count: int, expected: str
) -> None:
    trace = SearchTrace(execution_id=str(uuid4()), provider="test", requested_limit=5)
    trace.pages.append({"response": {"has_more": has_more}})

    assert trace.as_receipt(returned_count=returned_count)["completion"] == expected


@pytest.mark.asyncio
async def test_legacy_connector_gets_a_generic_receipt() -> None:
    class LegacyConnector(SourceConnector):
        endpoint = "legacy.search"

        async def search(
            self, query: str, max_results: int = 50, **kwargs: Any
        ) -> List[SourceDocument]:
            return [SourceDocument(connector_type="legacy", external_id="P1")]

    documents, receipt = await LegacyConnector().search_with_receipts(
        "approved terms", 1
    )

    assert len(documents) == 1
    assert receipt["pages"][0]["request"]["endpoint"] == "legacy.search"
    assert receipt["pages"][0]["record_keys"] == [
        {"provider": "legacy", "external_id": "P1"}
    ]


@pytest.mark.asyncio
async def test_uninspectable_connector_gets_a_generic_receipt() -> None:
    class OpaqueSearch:
        @property
        def __signature__(self) -> Any:
            raise ValueError("signature unavailable")

        async def __call__(self, query: str, max_results: int) -> List[SourceDocument]:
            return [SourceDocument(connector_type="opaque", external_id="P2")]

    class ConnectorWithOpaqueSearch(SourceConnector):
        endpoint = "opaque.search"

        async def search(
            self, query: str, max_results: int = 50, **kwargs: Any
        ) -> List[SourceDocument]:
            raise AssertionError("instance search override should be used")

    connector = ConnectorWithOpaqueSearch()
    setattr(connector, "search", OpaqueSearch())

    documents, receipt = await connector.search_with_receipts("terms", 1)

    assert documents[0].external_id == "P2"
    assert receipt["pages"][0]["request"]["endpoint"] == "opaque.search"


@pytest.mark.asyncio
async def test_trace_capable_connector_without_pages_gets_fallback_receipt() -> None:
    class TraceCapableConnector(SourceConnector):
        endpoint = "empty.search"

        async def search(
            self,
            query: str,
            max_results: int = 50,
            *,
            search_trace: SearchTrace | None = None,
            **kwargs: Any,
        ) -> List[SourceDocument]:
            return []

    documents, receipt = await TraceCapableConnector().search_with_receipts(
        "approved terms", 1
    )

    assert documents == []
    assert receipt["pages"][0]["request"]["endpoint"] == "empty.search"


@pytest.mark.asyncio
async def test_cancelled_connector_marks_outstanding_page_interrupted() -> None:
    class CancelledConnector(SourceConnector):
        async def search(
            self,
            query: str,
            max_results: int = 50,
            *,
            search_trace: SearchTrace | None = None,
            **kwargs: Any,
        ) -> List[SourceDocument]:
            assert search_trace is not None
            await search_trace.begin_request("cancelled.search", {"query": query})
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await CancelledConnector().search_with_receipts("terms", 1)
