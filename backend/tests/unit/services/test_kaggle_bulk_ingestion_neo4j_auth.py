"""I9: Kaggle bulk ingestion must not fall back to a literal Neo4j password."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.ingestion.kaggle_bulk_ingestion import KaggleBulkIngestionService

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


class TestKaggleBulkNeo4jAuth:
    def test_constructor_raises_when_password_unset(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        with pytest.raises(RuntimeError, match=REQUIRED_MESSAGE):
            KaggleBulkIngestionService()

    async def test_explicit_credentials_do_not_require_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        ctor = _make_driver_ctor()

        import neo4j

        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", ctor)
        service = KaggleBulkIngestionService(
            neo4j_uri="bolt://explicit:7687",
            neo4j_user="explicit-user",
            neo4j_password="explicit-secret",
        )

        await service.connect_neo4j()

        assert ctor.call_args.args[0] == "bolt://explicit:7687"
        assert ctor.call_args.kwargs.get("auth") == ("explicit-user", "explicit-secret")

    async def test_env_credentials_used_when_params_absent(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NEO4J_URI", "bolt://env-uri:7687")
        monkeypatch.setenv("NEO4J_USER", "env-user")
        monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")
        ctor = _make_driver_ctor()

        import neo4j

        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", ctor)
        service = KaggleBulkIngestionService()

        await service.connect_neo4j()

        assert ctor.call_args.args[0] == "bolt://env-uri:7687"
        assert ctor.call_args.kwargs.get("auth") == ("env-user", "env-secret")
