"""Artifact router transport: flag gating, size refusal, error mapping, downloads."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
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
USER, ORG, PROJECT, GRANT, UPLOAD, VERSION, WORKSPACE = (uuid4() for _ in range(7))
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
def app(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> FastAPI:
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
            return b"report\n", "text/markdown", 'weird "name" 报告.md'
        raise ArtifactStorageUnavailable()

    monkeypatch.setattr(artifacts, "reserve_upload", reserve)
    monkeypatch.setattr(artifacts, "store_upload", store)
    monkeypatch.setattr(artifacts, "publish_version", publish)
    monkeypatch.setattr(artifacts, "read_version_content", content)

    async def fake_db() -> Any:
        yield None

    application = FastAPI()
    if getattr(request, "param", False):
        from src.main import http_exception_handler

        application.add_exception_handler(HTTPException, http_exception_handler)
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


def test_workspace_grant_is_refused_by_every_publication_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve_workspace(
        _db: Any, token: str, *, required_scope: str
    ) -> IntegrationContext:
        if token != "opaque-grant" or required_scope != "artifacts:publish":
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            workspace_id=WORKSPACE,
            grant_id=GRANT,
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve_workspace
    )
    responses = [
        client.post("/api/v1/artifacts/uploads", json=_reserve_body(), headers=HEADERS),
        client.put(
            f"/api/v1/artifacts/uploads/{UPLOAD}/content",
            content=b"report\n",
            headers={**HEADERS, "Content-Type": "application/octet-stream"},
        ),
        client.post(
            "/api/v1/artifacts/versions",
            json={
                "publication_id": str(uuid4()),
                "upload_id": str(UPLOAD),
                "title": "report.md",
                "provenance": {"producer": "harness"},
            },
            headers=HEADERS,
        ),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403]
    assert {response.json()["detail"] for response in responses} == {
        "Integration access denied"
    }
    # Artifacts stay bound to one Collection: no service ran.
    assert CALLS == []


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
    assert download.headers["content-disposition"] == (
        'attachment; filename="weird__name____.md"; '
        "filename*=UTF-8''weird%20%22name%22%20%E6%8A%A5%E5%91%8A.md"
    )
    assert CALLS[-1] == ("content", (USER, ORG, VERSION))
    missing = client.get(
        f"/api/v1/artifacts/versions/{uuid4()}/content", headers=HEADERS
    )
    assert missing.status_code == 503


@pytest.mark.parametrize(
    ("is_cli", "headers", "enabled", "expected"),
    [
        (True, {}, True, 403),
        (False, HEADERS, True, 403),
        (False, {}, False, 503),
        (False, {}, True, 200),
    ],
)
def test_edit_requires_browser_and_flag(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    is_cli: bool,
    headers: dict[str, str],
    enabled: bool,
    expected: int,
) -> None:
    from src.api import artifacts
    from src.core.config import settings

    monkeypatch.setattr(settings, "ARTIFACT_EDITING_ENABLED", enabled)
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=is_cli
    )

    async def edit(_db: Any, **values: Any) -> ArtifactVersionDTO:
        CALLS.append(("edit", values))
        return _version()

    monkeypatch.setattr(artifacts, "edit_version", edit)
    response = client.post(
        f"/api/v1/artifacts/{uuid4()}/edits",
        json={
            "expected_parent_version_id": str(VERSION),
            "publication_id": str(uuid4()),
            "text": "changed",
        },
        headers=headers,
    )
    assert response.status_code == expected
    if expected == 200:
        assert CALLS[0][1]["user_id"] == USER
        assert CALLS[0][1]["organization_id"] == ORG
    else:
        assert CALLS == []


@pytest.mark.parametrize("enabled", [False, True])
def test_browser_capabilities_use_server_flags(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from src.core.config import settings

    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    monkeypatch.setattr(settings, "ARTIFACT_EDITING_ENABLED", enabled)
    monkeypatch.setattr(settings, "ARTIFACT_PREVIEW_ENABLED", enabled)
    response = client.get("/api/v1/artifacts/capabilities")
    assert response.status_code == 200
    assert response.json() == {"editing_enabled": enabled, "preview_enabled": enabled}
    assert response.headers["cache-control"] == "private, no-store"


def test_cli_cannot_read_browser_capabilities(client: TestClient) -> None:
    assert client.get("/api/v1/artifacts/capabilities").status_code == 403


def test_master_write_flag_disables_browser_edits_and_capability(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api import artifacts
    from src.core.config import settings

    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    monkeypatch.setattr(settings, "ARTIFACTS_ENABLED", False)
    monkeypatch.setattr(settings, "ARTIFACT_EDITING_ENABLED", True)
    monkeypatch.setattr(settings, "ARTIFACT_PREVIEW_ENABLED", True)

    async def edit(_db: Any, **values: Any) -> ArtifactVersionDTO:
        CALLS.append(("edit", values))
        return _version()

    monkeypatch.setattr(artifacts, "edit_version", edit)
    response = client.post(
        f"/api/v1/artifacts/{uuid4()}/edits",
        json={
            "expected_parent_version_id": str(VERSION),
            "publication_id": str(uuid4()),
            "text": "changed",
        },
    )
    assert response.status_code == 503
    assert CALLS == []
    capabilities = client.get("/api/v1/artifacts/capabilities")
    assert capabilities.json() == {"editing_enabled": False, "preview_enabled": True}
    assert capabilities.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "app", [False, True], indirect=True, ids=["router", "real-handler"]
)
def test_edit_conflict_returns_safe_current_version(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api import artifacts
    from src.core.config import settings

    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    monkeypatch.setattr(settings, "ARTIFACT_EDITING_ENABLED", True)

    async def conflict(_db: Any, **values: Any) -> ArtifactVersionDTO:
        error = ArtifactConflict()
        error.current_version_id = VERSION
        raise error

    monkeypatch.setattr(artifacts, "edit_version", conflict)
    response = client.post(
        f"/api/v1/artifacts/{uuid4()}/edits",
        json={
            "expected_parent_version_id": str(uuid4()),
            "publication_id": str(uuid4()),
            "text": "draft",
        },
    )
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "message": "Artifact publication conflict",
            "status_code": 409,
            "type": "http_error",
            "details": {"current_version_id": str(VERSION)},
        }
    }
    assert response.headers["cache-control"] == "private, no-store"
