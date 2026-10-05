"""Transport gates for the integration read gateway."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolResult
from src.services.integrations.context import IntegrationAccessDenied

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, GRANT = (uuid4() for _ in range(4))
HEADERS = {
    "Authorization": "Bearer cli-jwt",
    "X-NOUS-Integration-Grant": "opaque-grant",
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from src.api.integrations import tools
    from src.api.integrations.tools import router
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", True)

    async def resolve(_db: Any, token: str, *, required_scope: str) -> Any:
        if token != "opaque-grant" or required_scope != "tools:read":
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=GRANT
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve
    )
    monkeypatch.setattr(
        tools,
        "invoke_read",
        lambda *_a, **_k: _completed(
            ToolResult(content=[{"ok": True}], is_error=False, source_refs=[])
        ),
    )

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    application = FastAPI()
    application.include_router(router, prefix="/api/v1/integrations")
    application.dependency_overrides[get_db] = fake_db
    application.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=True
    )
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    return application


async def _completed(value: Any) -> Any:
    return value


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _read(client: TestClient, **overrides: Any) -> Any:
    body = {
        "tool_name": "list_project_documents",
        "arguments": {},
        "invocation_id": str(uuid4()),
    }
    body.update(overrides)
    return client.post("/api/v1/integrations/tools/read", json=body, headers=HEADERS)


def test_catalog_and_read_succeed_with_matching_credentials(client: TestClient) -> None:
    catalog = client.get("/api/v1/integrations/tools", headers=HEADERS)
    assert catalog.status_code == 200
    assert {tool["name"] for tool in catalog.json()} >= {"search_documents"}
    assert _read(client).status_code == 200


def test_jwt_actor_must_match_grant_actor(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), organization_id=ORG
    )
    assert _read(client).status_code == 403


def test_browser_token_or_missing_grant_is_denied(
    app: FastAPI, client: TestClient
) -> None:
    assert _read(client).headers is not None
    response = client.post(
        "/api/v1/integrations/tools/read",
        json={
            "tool_name": "list_project_documents",
            "arguments": {},
            "invocation_id": str(uuid4()),
        },
        headers={"Authorization": "Bearer cli-jwt"},
    )
    assert response.status_code == 403
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    assert _read(client).status_code == 403


def test_argument_error_maps_to_422(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.integrations import tools
    from src.services.integrations.read_tools import ToolArgumentError

    async def reject(*_a: Any, **_k: Any) -> Any:
        raise ToolArgumentError("unknown arguments")

    monkeypatch.setattr(tools, "invoke_read", reject)
    assert _read(client, arguments={"project_id": str(PROJECT)}).status_code == 422


def test_disabled_flag_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", False)
    assert client.get("/api/v1/integrations/tools", headers=HEADERS).status_code == 503
    assert _read(client).status_code == 503


def test_catalog_lists_transient_arxiv_reader() -> None:
    from src.services.integrations.read_tools import list_read_tools

    by_name = {tool.name: tool for tool in list_read_tools()}
    assert "get_arxiv_paper_content" in by_name
    assert by_name["get_arxiv_paper_content"].input_schema["required"] == ["arxiv_id"]
