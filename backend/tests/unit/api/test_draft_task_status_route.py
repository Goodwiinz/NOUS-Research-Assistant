"""GOO-297: the draft status route resolves a task from its retained row
when the Redis/in-memory cache has nothing (expiry, reload, process loss)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.research import drafts as drafts_api
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.draft_task_result import DraftTaskResult

pytestmark = pytest.mark.unit

PROJECT = UUID("11111111-1111-1111-1111-111111111111")
OWNER = UUID("22222222-2222-2222-2222-222222222222")
DRAFT = UUID("33333333-3333-3333-3333-333333333333")
HASH = hashlib.sha256(b"draft body").hexdigest()


def _completed_row() -> DraftTaskResult:
    now = datetime.now(timezone.utc)
    return DraftTaskResult(
        task_id="task-1",
        collection_id=PROJECT,
        actor_user_id=OWNER,
        state="completed",
        artifact_id=DRAFT,
        artifact_version=3,
        artifact_hash=HASH,
        request_fingerprint="f" * 64,
        started_at=now,
        heartbeat_at=now,
        terminal_at=now,
    )


RECONCILE = AsyncMock()


def _client(user_id: UUID) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(drafts_api.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[get_db] = lambda: MagicMock()
    RECONCILE.reset_mock()
    RECONCILE.return_value = _completed_row()
    with (
        patch.object(drafts_api, "_validate_project_ownership", new=AsyncMock()),
        patch.object(
            drafts_api.DraftGenerationService,
            "get_status_shared",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            drafts_api, "get_task_result", new=AsyncMock(return_value=_completed_row())
        ),
        patch.object(drafts_api, "reconcile_task", new=RECONCILE),
    ):
        yield TestClient(app)


@pytest.fixture
def owner_client() -> Iterator[TestClient]:
    yield from _client(OWNER)


@pytest.fixture
def foreign_client() -> Iterator[TestClient]:
    yield from _client(uuid4())


def test_status_resolves_from_database_when_cache_missing(
    owner_client: TestClient,
) -> None:
    response = owner_client.get(f"/api/v1/projects/{PROJECT}/drafts/status/task-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["draft_id"] == str(DRAFT)
    assert body["artifact_version"] == 3
    assert body["artifact_hash"] == HASH
    assert body["state_source"] == "database"


def test_status_hides_another_actors_task(foreign_client: TestClient) -> None:
    response = foreign_client.get(f"/api/v1/projects/{PROJECT}/drafts/status/task-1")

    assert response.status_code == 404
    # A foreign reader must not be able to commit another tenant's
    # interrupt flip: scope is checked before reconcile.
    RECONCILE.assert_not_awaited()
