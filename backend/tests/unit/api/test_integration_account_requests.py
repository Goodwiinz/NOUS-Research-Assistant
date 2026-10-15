"""Transport for account approvals: the rollout flag answers 503."""

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
from src.services.integrations.context import AccountConnectionsDisabled

pytestmark = pytest.mark.unit
USER, ORG, DEVICE = uuid4(), uuid4(), uuid4()


def test_a_disabled_account_request_is_503_with_a_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.integrations import grants
    from src.api.integrations.grants import router

    async def create_request(_db: Any, _user: Any, data: Any) -> Any:
        assert data.account is True
        raise AccountConnectionsDisabled()

    monkeypatch.setattr(grants.service, "create_request", create_request)

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/integrations")
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=True
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    response = TestClient(app).post(
        "/api/v1/integrations/grant-requests",
        json={"account": True, "device_id": str(DEVICE), "scopes": ["tools:read"]},
        headers={"Authorization": "Bearer cli-jwt"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Account connections are disabled"}
