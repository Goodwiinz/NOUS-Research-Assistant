"""Crossref ``update-to`` notices for GOO-319 corpus deltas.

The fixture is a real ``GET /works?filter=updates:10.1016/s0140-6736(20)31180-6``
response recorded on 2026-10-01 (items trimmed to the fields read): the
Lancet 2020 hydroxychloroquine registry paper, retracted, with an expression
of concern and errata. No test here reaches the network (``MockTransport``).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.services.research_engine.connectors import provider_http
from src.services.research_engine.connectors.crossref_connector import (
    NOTICE_BATCH,
    CrossrefConnector,
)

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "crossref"
    / "updates_lancet_2020_retraction.json"
)
TARGET = "10.1016/s0140-6736(20)31180-6"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())
    seen: list[httpx.Request] = []
    recorded = json.loads(FIXTURE.read_text())

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "fail" in request.url.params["filter"]:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=recorded)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    return seen


async def test_recorded_retraction_notice_is_returned(
    transport: list[httpx.Request],
) -> None:
    found = await CrossrefConnector().update_notices([TARGET.upper(), "10.1/none"])
    assert transport[0].url.params["filter"] == f"updates:10.1/none,updates:{TARGET}"
    assert found["10.1/none"] == []
    types = {(n["notice_doi"], n["type"]) for n in found[TARGET]}
    assert ("10.1016/s0140-6736(20)31324-6", "retraction") in types
    assert ("10.1016/s0140-6736(20)31290-3", "expression_of_concern") in types
    retraction = next(n for n in found[TARGET] if n["type"] == "retraction")
    assert retraction["date"].startswith("2020-")
    assert retraction["asserted_by"] in ("publisher", "retraction-watch")
    # The other paper's retraction (31174-0 -> 31528-2) is not attributed here.
    assert all(
        n["notice_doi"] != "10.1016/s0140-6736(20)31528-2" or (n["type"] == "erratum")
        for n in found[TARGET]
    )


async def test_failed_batch_is_absent_never_empty(
    transport: list[httpx.Request],
) -> None:
    dois = [f"10.1/ok{i:02d}" for i in range(NOTICE_BATCH)] + ["10.1/zfail"]
    found = await CrossrefConnector().update_notices(dois)
    assert len(transport) == 2  # batched; a failed request voids its batch only
    assert "10.1/zfail" not in found  # an outage is not "no notice"
    assert all(found[d] == [] for d in dois[:NOTICE_BATCH])
