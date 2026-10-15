"""Authenticated sharing transport does not accept CLI publication authority."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import artifacts
from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user, get_current_user_token
from src.core.security import TokenData
from src.schemas.artifact import ArtifactNotFound

pytestmark = pytest.mark.unit
USER, ORG, VERSION, SHARE, RECIPIENT, ARTIFACT = (uuid4() for _ in range(6))


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)
    monkeypatch.setattr(settings, "ARTIFACT_SHARING_ENABLED", True)
    application = FastAPI()
    application.include_router(artifacts.router, prefix="/api/v1")
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    application.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )

    async def fake_db() -> Any:
        yield None

    application.dependency_overrides[get_db] = fake_db
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _body(**changes: Any) -> dict[str, Any]:
    return {
        "version_id": str(VERSION),
        "audience_user_ids": [str(RECIPIENT)],
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        **changes,
    }


def test_sharing_capabilities_follow_write_flags(client: TestClient) -> None:
    response = client.get("/api/v1/artifacts/capabilities")
    assert response.status_code == 200
    assert response.json().get("sharing_enabled") is True


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize("authority", ["cli", "integration_header"])
def test_share_writes_require_browser_authority(
    client: TestClient, app: FastAPI, method: str, authority: str
) -> None:
    headers = {}
    if authority == "cli":
        app.dependency_overrides[get_current_user_token] = lambda: TokenData(
            user_id=str(USER), organization_id=str(ORG), is_cli=True
        )
    else:
        headers = {"X-NOUS-Integration-Grant": "opaque"}
    response = (
        client.post("/api/v1/artifacts/shares", json=_body(), headers=headers)
        if method == "post"
        else client.delete(f"/api/v1/artifacts/shares/{SHARE}", headers=headers)
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "flags",
    [
        ("ARTIFACTS_ENABLED",),
        ("ARTIFACT_SHARING_ENABLED",),
        ("ARTIFACTS_ENABLED", "ARTIFACT_SHARING_ENABLED"),
    ],
)
def test_disabled_flags_block_creation_but_allow_revocation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    revoked = False

    async def revoke(_db: Any, **values: Any) -> None:
        nonlocal revoked
        assert values == {"user_id": USER, "share_id": SHARE}
        revoked = True

    async def read(_db: Any, **values: Any) -> Any:
        assert revoked
        raise ArtifactNotFound()

    monkeypatch.setattr(artifacts, "revoke_share", revoke)
    monkeypatch.setattr(artifacts, "read_share", read)
    monkeypatch.setattr(artifacts, "read_share_content", read)
    for flag in flags:
        monkeypatch.setattr(settings, flag, False)
    assert client.post("/api/v1/artifacts/shares", json=_body()).status_code == 503
    response = client.delete(f"/api/v1/artifacts/shares/{SHARE}")
    assert response.status_code == 204
    assert response.headers["cache-control"] == "private, no-store"
    for suffix in ("", "/content"):
        assert (
            client.get(f"/api/v1/artifacts/shares/{SHARE}{suffix}").status_code == 404
        )
    assert (
        client.get("/api/v1/artifacts/capabilities").json()["sharing_enabled"] is False
    )


def test_reads_survive_flags_and_download_names_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def content(_db: Any, **values: Any) -> tuple[bytes, str, str]:
        assert values == {"user_id": USER, "share_id": SHARE}
        return b"old bytes", "text/plain", 'quoted "name"\r\n报告.txt'

    monkeypatch.setattr(artifacts, "read_share_content", content, raising=False)
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    monkeypatch.setattr(settings, "ARTIFACT_SHARING_ENABLED", False)
    response = client.get(f"/api/v1/artifacts/shares/{SHARE}/content")
    assert response.status_code == 200
    assert response.content == b"old bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="quoted__name_____.txt"')
    assert "filename*=UTF-8''quoted%20%22name%22%0D%0A" in disposition
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.parametrize("path", ["", "/content"])
def test_inaccessible_share_errors_disclose_no_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async def missing(_db: Any, **values: Any) -> Any:
        raise ArtifactNotFound("secret storage key or internal pathname")

    monkeypatch.setattr(artifacts, "read_share", missing, raising=False)
    monkeypatch.setattr(artifacts, "read_share_content", missing, raising=False)
    response = client.get(f"/api/v1/artifacts/shares/{SHARE}{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact not found"}
    assert response.headers.get("cache-control") == "private, no-store"


@pytest.mark.parametrize(
    "change",
    [
        {"audience_user_ids": []},
        {"audience_user_ids": [str(RECIPIENT)] * 101},
        {"audience_user_ids": [str(RECIPIENT)] * 2},
        {"expires_at": "2026-10-11T12:00:00"},
        {"organization_id": str(uuid4())},
    ],
)
def test_invalid_create_input_is_rejected(
    client: TestClient, change: dict[str, Any]
) -> None:
    response = client.post("/api/v1/artifacts/shares", json=_body(**change))
    assert response.status_code == 422


def test_browser_create_and_revoke_use_current_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create(_db: Any, **values: Any) -> Any:
        from src.schemas.artifact import ArtifactShareDTO

        assert values["user_id"] == USER and values["organization_id"] == ORG
        assert values["version_id"] == VERSION
        assert values["audience_user_ids"] == [RECIPIENT]
        return ArtifactShareDTO(
            id=SHARE,
            version_id=VERSION,
            expires_at=values["expires_at"],
            revoked_at=None,
        )

    async def revoke(_db: Any, **values: Any) -> None:
        assert values == {"user_id": USER, "share_id": SHARE}

    monkeypatch.setattr(artifacts, "create_share", create, raising=False)
    monkeypatch.setattr(artifacts, "revoke_share", revoke, raising=False)
    response = client.post("/api/v1/artifacts/shares", json=_body())
    assert response.status_code == 200
    assert set(response.json()) == {"id", "version_id", "expires_at", "revoked_at"}
    assert response.headers["cache-control"] == "private, no-store"
    assert client.delete(f"/api/v1/artifacts/shares/{SHARE}").status_code == 204


def test_owner_share_list_reads_survive_flags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def own_shares(_db: Any, **values: Any) -> Any:
        from src.schemas.artifact import ArtifactShareDTO

        assert values == {
            "user_id": USER,
            "organization_id": ORG,
            "artifact_id": ARTIFACT,
        }
        return [
            ArtifactShareDTO(
                id=SHARE,
                version_id=VERSION,
                expires_at=datetime.now(timezone.utc),
                revoked_at=None,
            )
        ]

    monkeypatch.setattr(artifacts, "list_artifact_shares", own_shares, raising=False)
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    monkeypatch.setattr(settings, "ARTIFACT_SHARING_ENABLED", False)
    response = client.get(f"/api/v1/artifacts/{ARTIFACT}/shares")
    assert response.status_code == 200
    assert response.json()[0]["id"] == str(SHARE)
    assert response.headers["cache-control"] == "private, no-store"
