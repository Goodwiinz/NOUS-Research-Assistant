"""DELETE /knowledge-graph/relationships/{id} must 404 on a miss (RC-2).

The handler raised HTTPException(404) inside a try whose only handler was
``except Exception``, which re-wrapped it as a 500.
"""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db_sync
from src.core.dependencies import get_current_user


@pytest.fixture
def mock_sync_db() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _override_dependencies(
    test_app: FastAPI, mock_user: Any, mock_sync_db: MagicMock
) -> Iterator[None]:
    def override_get_current_user() -> Any:
        return mock_user

    def override_get_db_sync() -> Iterator[MagicMock]:
        yield mock_sync_db

    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[get_db_sync] = override_get_db_sync
    try:
        yield
    finally:
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db_sync, None)


URL = "/api/v1/knowledge-graph/relationships/rel-1"


@patch("src.api.search.knowledge_graph._get_org_document_ids", return_value=["d1"])
@patch("src.api.search.knowledge_graph.knowledge_graph_service.delete_relationship")
def test_missing_relationship_returns_404(
    mock_delete: MagicMock, _mock_doc_ids: MagicMock, test_client: TestClient
) -> None:
    mock_delete.return_value = False

    response = test_client.delete(URL)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Relationship not found"
    mock_delete.assert_called_once()
    assert mock_delete.call_args.args == ("rel-1",)


@patch("src.api.search.knowledge_graph._get_org_document_ids", return_value=["d1"])
@patch("src.api.search.knowledge_graph.knowledge_graph_service.delete_relationship")
def test_deleted_relationship_returns_success(
    mock_delete: MagicMock, _mock_doc_ids: MagicMock, test_client: TestClient
) -> None:
    mock_delete.return_value = True

    response = test_client.delete(URL)

    assert response.status_code == 200
    assert response.json() == {"message": "Relationship deleted successfully"}


@patch("src.api.search.knowledge_graph._get_org_document_ids", return_value=["d1"])
@patch("src.api.search.knowledge_graph.knowledge_graph_service.delete_relationship")
def test_service_error_still_returns_500(
    mock_delete: MagicMock, _mock_doc_ids: MagicMock, test_client: TestClient
) -> None:
    mock_delete.side_effect = RuntimeError("neo4j exploded")

    response = test_client.delete(URL)

    assert response.status_code == 500
