"""Provider backoff must respect server cooldowns and cancellation."""

from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.asyncio
async def test_retries_transient_response_then_returns_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http

    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())
    monkeypatch.setattr(provider_http.asyncio, "sleep", AsyncMock())
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(503 if count == 1 else 200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await provider_http.get(
            client, "https://api.openalex.org/works", provider="openalex"
        )
    assert response.json() == {"ok": True}
    assert count == 2


@pytest.mark.asyncio
async def test_long_retry_after_does_not_retry_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http

    monkeypatch.setattr(provider_http, "wait_for_slot", AsyncMock())
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await provider_http.get(
                client, "https://api.openalex.org/works", provider="openalex"
            )
    assert count == 1


def test_local_rate_reservations_are_spaced_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.research_engine.connectors import provider_http

    monkeypatch.setattr(provider_http.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(provider_http, "_next_slot", {})
    assert provider_http.local_slot("pubmed") == 0
    assert provider_http.local_slot("pubmed") == pytest.approx(0.34)
    with pytest.raises(TimeoutError):
        for _ in range(100):
            provider_http.local_slot("pubmed")
