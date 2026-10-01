"""I9: Kaggle LLM bulk ingestion must not fall back to a literal Neo4j password."""

import pytest

from src.services.ingestion.kaggle_llm_bulk_ingestion import (
    KaggleLLMBulkIngestionService,
)

pytestmark = pytest.mark.unit

REQUIRED_MESSAGE = "NEO4J_PASSWORD must be set for Neo4j ingestion"


class TestKaggleLlmBulkNeo4jAuth:
    async def test_get_driver_raises_when_password_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        service = KaggleLLMBulkIngestionService()

        with pytest.raises(RuntimeError, match=REQUIRED_MESSAGE):
            await service._get_neo4j_driver()

    async def test_get_driver_receives_env_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        import neo4j

        monkeypatch.setenv("NEO4J_URI", "bolt://env-uri:7687")
        monkeypatch.setenv("NEO4J_USER", "env-user")
        monkeypatch.setenv("NEO4J_PASSWORD", "env-secret")
        ctor = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", ctor)
        service = KaggleLLMBulkIngestionService()

        driver = await service._get_neo4j_driver()

        assert driver is ctor.return_value
        assert ctor.call_args.args[0] == "bolt://env-uri:7687"
        assert ctor.call_args.kwargs.get("auth") == ("env-user", "env-secret")
