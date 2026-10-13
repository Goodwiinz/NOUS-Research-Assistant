"""Transport gates for POST /integrations/grants/{grant_id}/resume."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.schemas.integration_context import IssuedGrant
from src.services.integrations.context import (
    IntegrationAccessDenied,
    IntegrationConflict,
)

pytestmark = pytest.mark.unit
USER, ORG, GRANT, SUCCESSOR = (uuid4() for _ in range(4))
CLI = {"Authorization": "Bearer cli-jwt"}


def _client(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], outcome: Any, *, is_cli: bool
) -> TestClient:
    from src.api.integrations import grants
    from src.api.integrations.grants import router

    async def resume_grant(_db: Any, user: Any, grant_id: Any, token: str) -> Any:
        calls.append((user.id, grant_id, token))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(grants.service, "resume_grant", resume_grant)

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/integrations")
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=is_cli
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    return TestClient(app)


def _resume(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(f"/api/v1/integrations/grants/{GRANT}/resume", headers=headers)


def test_cli_caller_resumes_with_the_stored_grant_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    issued = IssuedGrant(token="nous_ig_new", grant_id=SUCCESSOR)
    client = _client(monkeypatch, calls, issued, is_cli=True)
    response = _resume(client, {**CLI, "X-NOUS-Integration-Grant": "nous_ig_old"})
    assert response.status_code == 200
    assert response.json() == {"token": "nous_ig_new", "grant_id": str(SUCCESSOR)}
    assert calls == [(USER, GRANT, "nous_ig_old")]


def test_a_missing_grant_header_reaches_the_service_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    client = _client(monkeypatch, calls, IntegrationAccessDenied(), is_cli=True)
    assert _resume(client, CLI).status_code == 403
    assert calls == [(USER, GRANT, "")]


def test_a_browser_session_cannot_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    issued = IssuedGrant(token="nous_ig_new", grant_id=SUCCESSOR)
    client = _client(monkeypatch, calls, issued, is_cli=False)
    response = _resume(
        client, {"Authorization": "Bearer browser", "X-NOUS-Integration-Grant": "t"}
    )
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    "error,status", [(IntegrationAccessDenied(), 403), (IntegrationConflict(), 409)]
)
def test_service_refusals_map_to_403_and_409(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    client = _client(monkeypatch, [], error, is_cli=True)
    response = _resume(client, {**CLI, "X-NOUS-Integration-Grant": "nous_ig_old"})
    assert response.status_code == status
    # Opaque: the body never says which check refused.
    assert "token" not in response.text
