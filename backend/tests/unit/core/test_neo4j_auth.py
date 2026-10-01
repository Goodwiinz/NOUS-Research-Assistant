"""Tests for src.core.neo4j_auth credential resolution."""

import pytest

from src.core.neo4j_auth import get_neo4j_auth

pytestmark = pytest.mark.unit

REQUIRED_MESSAGE = "NEO4J_PASSWORD must be set for Neo4j ingestion"


class TestGetNeo4jAuth:
    def test_returns_env_values_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEO4J_URI", "bolt://neo-test:7687")
        monkeypatch.setenv("NEO4J_USER", "admin")
        monkeypatch.setenv("NEO4J_PASSWORD", "s3cret-value")

        auth = get_neo4j_auth()

        assert auth.uri == "bolt://neo-test:7687"
        assert auth.user == "admin"
        assert auth.password == "s3cret-value"

    def test_uri_and_user_keep_defaults_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEO4J_PASSWORD", "s3cret-value")
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_USER", raising=False)

        auth = get_neo4j_auth()

        assert auth.uri == "bolt://localhost:7687"
        assert auth.user == "neo4j"

    def test_raises_when_password_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        with pytest.raises(RuntimeError, match=REQUIRED_MESSAGE):
            get_neo4j_auth()

    def test_raises_when_password_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEO4J_PASSWORD", "")

        with pytest.raises(RuntimeError, match=REQUIRED_MESSAGE):
            get_neo4j_auth()
