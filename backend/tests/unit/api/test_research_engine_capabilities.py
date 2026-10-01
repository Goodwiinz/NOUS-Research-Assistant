"""HTTP contract for the safe research connector capability projection."""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.config import settings


@pytest.fixture
def client(
    test_app: FastAPI, test_auth_headers: dict[str, str]
) -> Iterator[TestClient]:
    """Use the assembled application while suppressing external startup work."""

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(test_app, headers=test_auth_headers) as test_client:
            yield test_client
    finally:
        test_app.router.lifespan_context = original_lifespan


def test_capabilities_returns_only_safe_canonical_registry_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint mirrors the registry without exposing operational config."""
    sentinel = "never-expose-capability-secret"
    monkeypatch.setattr(settings, "SEMANTIC_SCHOLAR_API_KEY", sentinel)
    monkeypatch.setattr(settings, "CROSSREF_MAILTO", f"{sentinel}@example.test")

    response = client.get("/api/v1/research-engine/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert {item["id"] for item in body} == {
        "arxiv",
        "crossref",
        "openalex",
        "pubmed",
        "rag_store",
        "semantic_scholar",
    }
    for item in body:
        assert set(item) == {
            "id",
            "label",
            "daily_brief_eligible",
            "available",
            "features",
        }
        assert set(item["features"]) == {"full_text", "date_filter", "cursor"}

    serialized = json.dumps(body).lower()
    assert "web" not in serialized
    assert sentinel not in serialized
    for forbidden in ("aliases", "api_key", "mailto", "base_url", "config", "url"):
        assert forbidden not in serialized
