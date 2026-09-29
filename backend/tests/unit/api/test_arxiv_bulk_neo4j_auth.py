"""I9: arxiv_bulk stats endpoint must not fall back to a literal Neo4j password."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

REQUIRED_MESSAGE = "NEO4J_PASSWORD must be set for Neo4j ingestion"


class _FakeResult:
    async def single(self):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _make_driver_ctor():
    session = MagicMock()
    session.run = AsyncMock(return_value=_FakeResult())
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session_cm)
    driver.close = AsyncMock()
    return MagicMock(return_value=driver)


class TestArxivBulkStatsNeo4jAuth:
    async def test_raises_when_password_unset(self, monkeypatch):
        from src.api.arxiv.arxiv_bulk import get_ingestion_stats

        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        with pytest.raises(RuntimeError, match=REQUIRED_MESSAGE):
            await get_ingestion_stats()

    async def test_driver_receives_env_credentials(self, monkeypatch):
        import neo4j

        from src.api.arxiv.arxiv_bulk import get_ingestion_stats

        monkeypatch.setenv("NEO4J_URI", "bolt://env-uri:7687")
        monkeypatch.setenv("NEO4J_USER", "env-user")
        monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")
        ctor = _make_driver_ctor()
        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", ctor)

        await get_ingestion_stats()

        ctor.assert_called_once()
        assert ctor.call_args.args[0] == "bolt://env-uri:7687"
        assert ctor.call_args.kwargs.get("auth") == ("env-user", "env-secret")
