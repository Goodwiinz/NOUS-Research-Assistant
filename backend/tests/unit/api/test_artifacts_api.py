"""Artifact router transport: flag gating, size refusal, error mapping, downloads."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user, get_current_user_token
from src.core.security import TokenData
from src.schemas.artifact import (
    ArtifactConflict,
    ArtifactProvenance,
    ArtifactQuotaExceeded,
    ArtifactStorageUnavailable,
    ArtifactUploadDTO,
    ArtifactVersionDTO,
)
from src.schemas.integration_context import IntegrationContext
from src.services.integrations.context import IntegrationAccessDenied

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, GRANT, UPLOAD, VERSION = (uuid4() for _ in range(6))
HEADERS = {
    "Authorization": "Bearer cli-jwt",
    "X-NOUS-Integration-Grant": "opaque-grant",
}
CALLS: list[tuple[str, Any]] = []


def _version() -> ArtifactVersionDTO:
    return ArtifactVersionDTO(
        artifact_id=uuid4(),
        version_id=VERSION,
        parent_version_id=None,
        title="report.md",
        mime_type="text/markdown",
        byte_size=7,
        sha256="a" * 64,
        created_at=datetime.now(timezone.utc),
        provenance=ArtifactProvenance(producer="harness"),
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from src.api import artifacts
    from src.core.config import settings

    CALLS.clear()
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", True)

    async def resolve(
        _db: Any, token: str, *, required_scope: str
    ) -> IntegrationContext:
        if token != "opaque-grant" or required_scope != "artifacts:publish":
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=GRANT
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve
    )

    async def reserve(
        _db: Any, context: IntegrationContext, request: Any
    ) -> ArtifactUploadDTO:
        CALLS.append(("reserve", context))
        if request.byte_size == 999:
            raise ArtifactQuotaExceeded()
        return ArtifactUploadDTO(
            upload_id=UPLOAD, expires_at=datetime.now(timezone.utc)
        )

    async def store(
        _db: Any, context: IntegrationContext, upload_id: UUID, content: bytes
    ) -> None:
        CALLS.append(("store", (upload_id, content)))

    async def publish(
        _db: Any, context: IntegrationContext, request: Any
    ) -> ArtifactVersionDTO:
        CALLS.append(("publish", request))
        if request.title == "conflict":
            raise ArtifactConflict()
        return _version()

    async def content(
        _db: Any, *, user_id: UUID, organization_id: UUID, version_id: UUID
    ) -> tuple[bytes, str, str]:
        CALLS.append(("content", (user_id, organization_id, version_id)))
        if version_id == VERSION:
            return b"report\n", "text/markdown", 'weird "name".md'
        raise ArtifactStorageUnavailable()

    monkeypatch.setattr(artifacts, "reserve_upload", reserve)
    monkeypatch.setattr(artifacts, "store_upload", store)
    monkeypatch.setattr(artifacts, "publish_version", publish)
    monkeypatch.setattr(artifacts, "read_version_content", content)

    async def fake_db() -> Any:
        yield None

    application = FastAPI()
    application.include_router(artifacts.router, prefix="/api/v1")
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=True
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


def _reserve_body(**overrides: Any) -> dict[str, Any]:
    return {
        "publication_id": str(uuid4()),
        "byte_size": 7,
        "mime_type": "text/markdown",
        "sha256": "a" * 64,
        **overrides,
    }


def test_publication_round_trip_uses_context_not_payload(client: TestClient) -> None:
    reserved = client.post(
        "/api/v1/artifacts/uploads", json=_reserve_body(), headers=HEADERS
    )
    assert reserved.status_code == 200 and reserved.json()["upload_id"] == str(UPLOAD)
    stored = client.put(
        f"/api/v1/artifacts/uploads/{UPLOAD}/content",
        content=b"report\n",
        headers={**HEADERS, "Content-Type": "application/octet-stream"},
    )
    assert stored.status_code == 204
    published = client.post(
        "/api/v1/artifacts/versions",
        json={
            "publication_id": str(uuid4()),
            "upload_id": str(UPLOAD),
            "title": "report.md",
            "provenance": {"producer": "harness"},
        },
        headers=HEADERS,
    )
    assert published.status_code == 200
    assert published.json()["version_id"] == str(VERSION)
    assert "storage_key" not in published.json()
    context = CALLS[0][1]
    assert (context.user_id, context.organization_id, context.project_id) == (
        USER,
        ORG,
        PROJECT,
    )
    assert CALLS[1][1] == (UPLOAD, b"report\n")


def test_identity_fields_in_payload_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/artifacts/uploads",
        json=_reserve_body(project_id=str(uuid4())),
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert CALLS == []


def test_oversized_body_is_refused_before_reading(client: TestClient) -> None:
    response = client.put(
        f"/api/v1/artifacts/uploads/{UPLOAD}/content",
        content=b"x",
        headers={**HEADERS, "Content-Length": str(10 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 413
    assert CALLS == []


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        ("/api/v1/artifacts/uploads", _reserve_body(byte_size=999), 413),
        (
            "/api/v1/artifacts/versions",
            {
                "publication_id": str(uuid4()),
                "upload_id": str(uuid4()),
                "title": "conflict",
                "provenance": {"producer": "harness"},
            },
            409,
        ),
    ],
)
def test_service_errors_map_to_stable_statuses(
    client: TestClient, path: str, body: dict[str, Any], expected: int
) -> None:
    response = client.post(path, json=body, headers=HEADERS)
    assert response.status_code == expected
    assert "Traceback" not in response.text


def test_missing_grant_or_browser_token_is_forbidden(
    client: TestClient, app: FastAPI
) -> None:
    assert (
        client.post(
            "/api/v1/artifacts/uploads",
            json=_reserve_body(),
            headers={"Authorization": "Bearer cli-jwt"},
        ).status_code
        == 403
    )
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    assert (
        client.post(
            "/api/v1/artifacts/uploads", json=_reserve_body(), headers=HEADERS
        ).status_code
        == 403
    )
    assert CALLS == []


def test_flag_off_blocks_writes_but_not_reads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    assert (
        client.post(
            "/api/v1/artifacts/uploads", json=_reserve_body(), headers=HEADERS
        ).status_code
        == 503
    )
    download = client.get(
        f"/api/v1/artifacts/versions/{VERSION}/content", headers=HEADERS
    )
    assert download.status_code == 200


def test_download_is_attachment_no_store_with_safe_filename(client: TestClient) -> None:
    download = client.get(
        f"/api/v1/artifacts/versions/{VERSION}/content", headers=HEADERS
    )
    assert download.status_code == 200
    assert download.content == b"report\n"
    assert download.headers["content-type"].startswith("text/markdown")
    assert download.headers["cache-control"] == "private, no-store"
    assert (
        download.headers["content-disposition"]
        == 'attachment; filename="weird__name_.md"'
    )
    assert CALLS[-1] == ("content", (USER, ORG, VERSION))
    missing = client.get(
        f"/api/v1/artifacts/versions/{uuid4()}/content", headers=HEADERS
    )
    assert missing.status_code == 503
