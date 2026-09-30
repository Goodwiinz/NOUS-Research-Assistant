"""Transport gates for listing and revoking connected devices."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.schemas.integration_connections import ConnectedDevice
from src.services.integrations.connections import ConnectionNotFound

pytestmark = pytest.mark.unit
USER, ORG, DEVICE, REQUEST = (uuid4() for _ in range(4))
BROWSER = {"Authorization": "Bearer browser"}
CLI_HEADERS = {
    "Authorization": "Bearer cli-jwt",
    "X-NOUS-Integration-Grant": "opaque-grant",
}


async def _completed(value: Any) -> Any:
    if isinstance(value, Exception):
        raise value
    return value


@pytest.fixture
def calls() -> dict[str, list[Any]]:
    return {"list": [], "consent": [], "device": []}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> FastAPI:
    from src.api.integrations import connections
    from src.api.integrations.connections import router

    def list_connections(_db: Any, user: Any) -> Any:
        calls["list"].append(user)
        return _completed(
            [
                ConnectedDevice(
                    device_id=DEVICE,
                    device_label="Laptop",
                    connected_at=datetime.now(timezone.utc),
                    consents=[],
                )
            ]
        )

    def revoke_consent(_db: Any, user: Any, request_id: Any) -> Any:
        calls["consent"].append(request_id)
        return _completed(None)

    def disconnect_device(_db: Any, user: Any, device_id: Any) -> Any:
        calls["device"].append(device_id)
        return _completed(None)

    monkeypatch.setattr(connections, "list_connections", list_connections)
    monkeypatch.setattr(connections, "revoke_consent", revoke_consent)
    monkeypatch.setattr(connections, "disconnect_device", disconnect_device)

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    application = FastAPI()
    application.include_router(router, prefix="/api/v1/integrations")
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_browser_lists_and_revokes(
    client: TestClient, calls: dict[str, list[Any]]
) -> None:
    listed = client.get("/api/v1/integrations/connections", headers=BROWSER)
    assert listed.status_code == 200 and listed.json()[0]["device_label"] == "Laptop"
    assert (
        client.post(
            f"/api/v1/integrations/grant-requests/{REQUEST}/revoke", headers=BROWSER
        ).status_code
        == 204
    )
    assert (
        client.post(
            f"/api/v1/integrations/devices/{DEVICE}/revoke", headers=BROWSER
        ).status_code
        == 204
    )
    assert calls["consent"] == [REQUEST] and calls["device"] == [DEVICE]


def test_a_connected_device_cannot_list_or_revoke(
    app: FastAPI, client: TestClient, calls: dict[str, list[Any]]
) -> None:
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=True
    )
    for headers in (CLI_HEADERS, {"Authorization": "Bearer cli-jwt"}):
        assert (
            client.get("/api/v1/integrations/connections", headers=headers).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/integrations/devices/{DEVICE}/revoke", headers=headers
            ).status_code
            == 403
        )
    assert calls["list"] == [] and calls["device"] == []


def test_unknown_or_foreign_targets_are_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.integrations import connections

    monkeypatch.setattr(
        connections,
        "revoke_consent",
        lambda *_a, **_k: _completed(ConnectionNotFound()),
    )
    monkeypatch.setattr(
        connections,
        "disconnect_device",
        lambda *_a, **_k: _completed(ConnectionNotFound()),
    )
    for path in (
        f"/api/v1/integrations/grant-requests/{REQUEST}/revoke",
        f"/api/v1/integrations/devices/{DEVICE}/revoke",
    ):
        missing = client.post(path, headers=BROWSER)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Connection not found"
