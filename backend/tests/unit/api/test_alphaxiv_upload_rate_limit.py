"""The root-mounted alphaXiv upload alias enforces a per-user upload budget.

``ApiRateLimitMiddleware`` only counts ``/api/`` paths, so without a route-level
limiter ``/v1/assistant/upload-file`` had no budget at all.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.compat import alphaxiv_upload as route
from src.core.rate_limit import InMemoryRateLimiter
from src.schemas.alphaxiv_files import AlphaXivUploadedFile, AlphaXivUploadResponse
from src.shared.enums import ApiDocumentStatus

DOCUMENT = UUID("44444444-4444-4444-8444-444444444444")


def _client(monkeypatch: pytest.MonkeyPatch, user_ids: list[UUID]) -> TestClient:
    monkeypatch.setattr(
        route,
        "_upload_rate_limiter",
        InMemoryRateLimiter(
            route._UPLOAD_RATE_LIMIT_RPM, route._UPLOAD_RATE_WINDOW_MINUTES
        ),
    )
    upload = AsyncMock(
        return_value=AlphaXivUploadResponse(
            data=AlphaXivUploadedFile.model_validate(
                {
                    "url": f"api/v1/files/{DOCUMENT}/download",
                    "documentId": DOCUMENT,
                    "processingStatus": ApiDocumentStatus.QUEUED,
                }
            )
        )
    )
    monkeypatch.setattr(route, "upload_file", upload)
    app = FastAPI()
    app.include_router(route.router)

    async def user() -> SimpleNamespace:
        return SimpleNamespace(id=user_ids[0])

    async def session() -> AsyncMock:
        return AsyncMock()

    app.dependency_overrides[route.get_current_user] = user
    app.dependency_overrides[route.get_db] = session
    return TestClient(app)


def _post(client: TestClient) -> int:
    return client.post(
        "/v1/assistant/upload-file",
        content=b"notes",
        headers={"X-Filename": "notes.txt", "Content-Type": "text/plain"},
    ).status_code


def test_thirty_first_upload_in_window_is_rejected_with_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [uuid4()]
    client = _client(monkeypatch, users)
    statuses = [_post(client) for _ in range(route._UPLOAD_RATE_LIMIT_RPM)]
    assert statuses == [200] * 30
    assert _post(client) == 429
    assert route.upload_file.await_count == 30  # type: ignore[attr-defined]

    # The budget is per user, not global.
    users[0] = uuid4()
    assert _post(client) == 200
