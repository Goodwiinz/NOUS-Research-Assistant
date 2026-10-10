"""Transport gates for explicitly selected integration context."""

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
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_selected_context import ContextOptions, MemoryOption
from src.schemas.integration_tools import ToolResult
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.selected_context import (
    ContextNotFound,
    ContextSelectionInvalid,
)

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, GRANT, REQUEST, MEMORY = (uuid4() for _ in range(6))
CLI_HEADERS = {
    "Authorization": "Bearer cli-jwt",
    "X-NOUS-Integration-Grant": "opaque-grant",
}
BROWSER = {"Authorization": "Bearer browser"}


def _options() -> ContextOptions:
    return ContextOptions(
        request_id=REQUEST,
        project_id=PROJECT,
        project_label="Thesis",
        memories=[
            MemoryOption(
                id=MEMORY,
                content="Cite in APA",
                source="manual",
                created_at=datetime.now(timezone.utc),
            )
        ],
        selected_memory_ids=[],
    )


async def _completed(value: Any) -> Any:
    if isinstance(value, Exception):
        raise value
    return value


@pytest.fixture
def calls() -> dict[str, list[Any]]:
    return {"options": [], "save": [], "read": []}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> FastAPI:
    from src.api.integrations import selected_context
    from src.api.integrations.selected_context import router
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", True)

    async def resolve(_db: Any, token: str, *, required_scope: str) -> Any:
        if token != "opaque-grant" or required_scope != "context:read":
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=GRANT
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve
    )

    def context_options(_db: Any, user: Any, request_id: Any) -> Any:
        calls["options"].append((user, request_id))
        return _completed(_options())

    def save_selection(_db: Any, user: Any, request_id: Any, memory_ids: Any) -> Any:
        calls["save"].append((user, request_id, memory_ids))
        return _completed(_options())

    def read_selected_context(_db: Any, context: Any) -> Any:
        calls["read"].append(context)
        return _completed(
            ToolResult(content=[{"memories": []}], is_error=False, source_refs=[])
        )

    monkeypatch.setattr(selected_context, "context_options", context_options)
    monkeypatch.setattr(selected_context, "save_selection", save_selection)
    monkeypatch.setattr(
        selected_context, "read_selected_context", read_selected_context
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


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as_browser(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )


def test_harness_reads_with_a_context_grant_only(
    client: TestClient, calls: dict[str, list[Any]]
) -> None:
    ok = client.get("/api/v1/integrations/context", headers=CLI_HEADERS)
    assert ok.status_code == 200
    assert calls["read"][0].grant_id == GRANT
    assert (
        client.get(
            "/api/v1/integrations/context", headers={"Authorization": "Bearer cli-jwt"}
        ).status_code
        == 403
    )


def test_harness_cannot_list_or_change_the_selection(
    client: TestClient, calls: dict[str, list[Any]]
) -> None:
    for headers in (CLI_HEADERS, {"Authorization": "Bearer cli-jwt"}):
        assert (
            client.get(
                f"/api/v1/integrations/context/options?request_id={REQUEST}",
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/api/v1/integrations/context/selection",
                json={"request_id": str(REQUEST), "memory_ids": [str(MEMORY)]},
                headers=headers,
            ).status_code
            == 403
        )
    assert (
        client.put(
            "/api/v1/integrations/context/selection",
            json={
                "request_id": str(REQUEST),
                "memory_ids": [],
                "skill_version_ids": [str(uuid4())],
                "refresh_skills": True,
            },
            headers=CLI_HEADERS,
        ).status_code
        == 403
    )
    assert calls["options"] == [] and calls["save"] == []


def test_browser_owner_lists_and_saves(
    app: FastAPI, client: TestClient, calls: dict[str, list[Any]]
) -> None:
    _as_browser(app)
    listed = client.get(
        f"/api/v1/integrations/context/options?request_id={REQUEST}", headers=BROWSER
    )
    assert listed.status_code == 200 and listed.json()["project_label"] == "Thesis"
    saved = client.put(
        "/api/v1/integrations/context/selection",
        json={"request_id": str(REQUEST), "memory_ids": [str(MEMORY)]},
        headers=BROWSER,
    )
    assert saved.status_code == 200
    assert calls["save"][0][1:] == (REQUEST, [MEMORY])
    too_many = client.put(
        "/api/v1/integrations/context/selection",
        json={
            "request_id": str(REQUEST),
            "memory_ids": [str(uuid4()) for _ in range(26)],
        },
        headers=BROWSER,
    )
    assert too_many.status_code == 422
    extra = client.put(
        "/api/v1/integrations/context/selection",
        json={"request_id": str(REQUEST), "memory_ids": [], "user_id": str(USER)},
        headers=BROWSER,
    )
    assert extra.status_code == 422


def test_service_errors_map_to_stable_statuses(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.integrations import selected_context

    _as_browser(app)
    monkeypatch.setattr(
        selected_context,
        "context_options",
        lambda *_a, **_k: _completed(ContextNotFound()),
    )
    missing = client.get(
        f"/api/v1/integrations/context/options?request_id={REQUEST}", headers=BROWSER
    )
    assert missing.status_code == 404
    monkeypatch.setattr(
        selected_context,
        "save_selection",
        lambda *_a, **_k: _completed(ContextSelectionInvalid()),
    )
    invalid = client.put(
        "/api/v1/integrations/context/selection",
        json={"request_id": str(REQUEST), "memory_ids": [str(MEMORY)]},
        headers=BROWSER,
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "Selected memories must belong to this project"


def test_disabled_flag_blocks_the_harness_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", False)
    assert (
        client.get("/api/v1/integrations/context", headers=CLI_HEADERS).status_code
        == 503
    )
    assert (
        client.post(
            "/api/v1/integrations/context/skills/load",
            json={"skill_name": "review"},
            headers=CLI_HEADERS,
        ).status_code
        == 503
    )


def test_skill_load_uses_only_grant_context_and_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.integrations import selected_context

    seen: list[Any] = []

    async def load(_db: Any, context: Any, name: str) -> ToolResult:
        seen.append((context, name))
        return ToolResult(
            content=[{"name": name, "version_id": "frozen"}],
            is_error=False,
            source_refs=[{"version_id": "frozen"}],
        )

    monkeypatch.setattr(selected_context, "load_selected_skill", load, raising=False)
    response = client.post(
        "/api/v1/integrations/context/skills/load",
        json={"skill_name": "review"},
        headers=CLI_HEADERS,
    )
    assert response.status_code == 200
    assert seen[0][0].grant_id == GRANT and seen[0][1] == "review"
    assert (
        client.post(
            "/api/v1/integrations/context/skills/load",
            json={"skill_name": "review", "snapshot_id": str(uuid4())},
            headers=CLI_HEADERS,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/integrations/context/skills/load",
            json={"skill_name": "review"},
            headers={"Authorization": "Bearer cli-jwt"},
        ).status_code
        == 403
    )
