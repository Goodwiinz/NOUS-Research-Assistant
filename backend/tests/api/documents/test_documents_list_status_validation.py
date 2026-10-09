"""Document listing must preserve deliberate filter errors at the HTTP boundary."""

from __future__ import annotations

import uuid
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from src.api.documents import documents as documents_mod
from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user

pytestmark = pytest.mark.unit


@pytest.fixture()
def db() -> MagicMock:
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture()
def client(db: MagicMock) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(documents_mod.router, prefix="/api/v1")
    organization = MagicMock(id=uuid.uuid4())
    app.dependency_overrides[get_current_user] = lambda: MagicMock()
    app.dependency_overrides[get_current_organization] = lambda: organization
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/api/v1/documents", "/api/v1/documents/"])
def test_unknown_processing_status_returns_400(
    client: TestClient, db: MagicMock, path: str
) -> None:
    response = client.get(path, params={"processing_status": "not-a-status"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid processing_status: not-a-status"}
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "processing_status", ["pending", "queued", "completed", "indexed"]
)
def test_known_processing_status_still_lists_documents(
    client: TestClient, db: MagicMock, processing_status: str
) -> None:
    response = client.get(
        "/api/v1/documents", params={"processing_status": processing_status}
    )

    assert response.status_code == 200
    assert response.json()["documents"] == []
    assert response.json()["pagination"]["total"] == 0
    assert db.execute.await_count == 2


def test_database_failure_keeps_safe_500(client: TestClient, db: MagicMock) -> None:
    db.execute.side_effect = SQLAlchemyError("private database diagnostic")

    response = client.get("/api/v1/documents")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to list documents"}
