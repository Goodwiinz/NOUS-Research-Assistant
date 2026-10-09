"""I9: arxiv_llm_bulk endpoints must not fall back to a literal Neo4j password."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

REQUIRED_MESSAGE = "NEO4J_PASSWORD must be set for Neo4j ingestion"


class _FakeResult:
    async def single(self) -> None:
        return None

    def __aiter__(self) -> "_FakeResult":
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration


def _make_driver_ctor() -> MagicMock:
    session = MagicMock()
    session.run = AsyncMock(return_value=_FakeResult())
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session_cm)
    driver.close = AsyncMock()
    return MagicMock(return_value=driver)


class TestArxivLlmBulkStatsNeo4jAuth:
    async def test_missing_password_returns_safe_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Credential resolution must sit inside the endpoint's error boundary:
        # a raw RuntimeError would reach the generic handler (which echoes
        # exception text in DEBUG) instead of the stable safe response.
        from src.api.arxiv.arxiv_llm_bulk import get_llm_ingestion_stats

        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        with pytest.raises(HTTPException) as exc_info:
            await get_llm_ingestion_stats()

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == "Internal server error"

    async def test_driver_receives_env_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import neo4j

        from src.api.arxiv.arxiv_llm_bulk import get_llm_ingestion_stats

        monkeypatch.setenv("NEO4J_URI", "bolt://env-uri:7687")
        monkeypatch.setenv("NEO4J_USER", "env-user")
        monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")
        ctor = _make_driver_ctor()
        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", ctor)

        await get_llm_ingestion_stats()

        ctor.assert_called_once()
        assert ctor.call_args.args[0] == "bolt://env-uri:7687"
        assert ctor.call_args.kwargs.get("auth") == ("env-user", "env-secret")


class TestArxivLlmBulkStartNeo4jAuth:
    async def test_start_rejects_missing_password_before_scheduling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # /start must not report "Started" and detach a task that dies on
        # the missing credential outside any failure boundary.
        from src.api.arxiv import arxiv_llm_bulk as arxiv_bulk

        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.setattr(arxiv_bulk, "_ingestion_task", None)
        monkeypatch.setattr(arxiv_bulk, "_current_ingestion", None)

        with pytest.raises(HTTPException) as exc_info:
            await arxiv_bulk.start_llm_bulk_ingestion(
                arxiv_bulk.LLMBulkIngestionRequest(), MagicMock()
            )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == arxiv_bulk.NEO4J_UNCONFIGURED_DETAIL
        assert arxiv_bulk._ingestion_task is None

    async def test_small_batch_missing_password_returns_safe_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.api.arxiv.arxiv_llm_bulk import test_llm_small_batch
        from src.services.ingestion.kaggle_llm_bulk_ingestion import (
            KaggleLLMBulkIngestionService,
        )

        # Skip the Kaggle download; go straight to credential resolution.
        async def _resolve_driver_only(
            self: KaggleLLMBulkIngestionService, **_: object
        ) -> None:
            await self._get_neo4j_driver()

        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.setattr(
            KaggleLLMBulkIngestionService, "run_ingestion", _resolve_driver_only
        )

        with pytest.raises(HTTPException) as exc_info:
            await test_llm_small_batch()

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == "Internal server error"
