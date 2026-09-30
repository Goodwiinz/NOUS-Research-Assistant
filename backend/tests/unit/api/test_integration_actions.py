"""Transport gates for durable integration actions."""

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
from src.schemas.tool_actions import ActionStatus
from src.services.agent.tool_actions import ActionConflict, ActionNotFound
from src.services.integrations.context import IntegrationAccessDenied

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, GRANT, INVOCATION = (uuid4() for _ in range(5))
CLI_HEADERS = {
    "Authorization": "Bearer cli-jwt",
    "X-NOUS-Integration-Grant": "opaque-grant",
}
BODY = {
    "tool_name": "create_project_note",
    "arguments": {"title": "t", "content": "c"},
    "invocation_id": str(INVOCATION),
}


def _status(state: str = "awaiting_approval") -> ActionStatus:
    return ActionStatus(
        invocation_id=INVOCATION, state=state, tool_name="create_project_note"  # type: ignore[arg-type]
    )


async def _completed(value: Any) -> Any:
    if isinstance(value, Exception):
        raise value
    return value


@pytest.fixture
def calls() -> dict[str, list[Any]]:
    return {"request": [], "decide": [], "status": []}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> FastAPI:
    from src.api.integrations import actions
    from src.api.integrations.actions import router
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", True)

    async def resolve(_db: Any, token: str, *, required_scope: str) -> Any:
        if token != "opaque-grant" or required_scope != "tools:write":
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER, organization_id=ORG, project_id=PROJECT, grant_id=GRANT
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve
    )

    def request_action(_db: Any, actor: Any, invocation: Any) -> Any:
        calls["request"].append((actor, invocation))
        return _completed(_status())

    def decide_action(
        _db: Any, user: Any, invocation_id: Any, *, approved: bool
    ) -> Any:
        calls["decide"].append((user, invocation_id, approved))
        return _completed(_status("approved" if approved else "failed"))

    def get_action_status(_db: Any, actor: Any, invocation_id: Any) -> Any:
        calls["status"].append((actor, invocation_id))
        return _completed(_status("succeeded"))

    monkeypatch.setattr(actions, "request_action", request_action)
    monkeypatch.setattr(actions, "decide_action", decide_action)
    monkeypatch.setattr(actions, "get_action_status", get_action_status)

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


def test_request_and_status_bind_the_grant_actor(
    client: TestClient, calls: dict[str, list[Any]]
) -> None:
    created = client.post(
        "/api/v1/integrations/actions", json=BODY, headers=CLI_HEADERS
    )
    assert created.status_code == 200
    assert created.json()["state"] == "awaiting_approval"
    actor, invocation = calls["request"][0]
    assert (actor.user_id, actor.organization_id, actor.project_id, actor.grant_id) == (
        USER,
        ORG,
        PROJECT,
        GRANT,
    )
    assert invocation.invocation_id == INVOCATION
    read = client.get(f"/api/v1/integrations/actions/{INVOCATION}", headers=CLI_HEADERS)
    assert read.status_code == 200 and read.json()["state"] == "succeeded"
    assert calls["status"][0][0].grant_id == GRANT


def test_cli_token_cannot_decide_with_or_without_grant(
    client: TestClient, calls: dict[str, list[Any]]
) -> None:
    for headers in (CLI_HEADERS, {"Authorization": "Bearer cli-jwt"}):
        rejected = client.post(
            f"/api/v1/integrations/actions/{INVOCATION}/decision",
            json={"approved": True},
            headers=headers,
        )
        assert rejected.status_code == 403
    assert calls["decide"] == []


def test_browser_user_decides_once_without_a_grant_header(
    app: FastAPI, client: TestClient, calls: dict[str, list[Any]]
) -> None:
    _as_browser(app)
    decided = client.post(
        f"/api/v1/integrations/actions/{INVOCATION}/decision",
        json={"approved": False},
        headers={"Authorization": "Bearer browser"},
    )
    assert decided.status_code == 200 and decided.json()["state"] == "failed"
    assert calls["decide"][0][1:] == (INVOCATION, False)
    # A browser session that also presents a grant header is not interactive.
    with_grant = client.post(
        f"/api/v1/integrations/actions/{INVOCATION}/decision",
        json={"approved": True},
        headers={"Authorization": "Bearer browser", **CLI_HEADERS},
    )
    assert with_grant.status_code == 403


def test_browser_token_cannot_request_or_read_as_a_harness(
    app: FastAPI, client: TestClient
) -> None:
    _as_browser(app)
    assert (
        client.post(
            "/api/v1/integrations/actions", json=BODY, headers=CLI_HEADERS
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/integrations/actions/{INVOCATION}", headers=CLI_HEADERS
        ).status_code
        == 403
    )


def test_disabled_flag_blocks_new_requests_but_not_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", False)
    assert (
        client.post(
            "/api/v1/integrations/actions", json=BODY, headers=CLI_HEADERS
        ).status_code
        == 503
    )
    assert (
        client.get(
            f"/api/v1/integrations/actions/{INVOCATION}", headers=CLI_HEADERS
        ).status_code
        == 200
    )


def test_service_errors_map_to_stable_statuses(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.integrations import actions

    monkeypatch.setattr(
        actions, "request_action", lambda *_a, **_k: _completed(ActionConflict())
    )
    conflict = client.post(
        "/api/v1/integrations/actions", json=BODY, headers=CLI_HEADERS
    )
    assert conflict.status_code == 409
    monkeypatch.setattr(
        actions, "get_action_status", lambda *_a, **_k: _completed(ActionNotFound())
    )
    missing = client.get(
        f"/api/v1/integrations/actions/{INVOCATION}", headers=CLI_HEADERS
    )
    assert missing.status_code == 404
    _as_browser(app)
    monkeypatch.setattr(
        actions, "decide_action", lambda *_a, **_k: _completed(ActionConflict())
    )
    twice = client.post(
        f"/api/v1/integrations/actions/{INVOCATION}/decision",
        json={"approved": True},
        headers={"Authorization": "Bearer browser"},
    )
    assert twice.status_code == 409
    unknown_field = client.post(
        f"/api/v1/integrations/actions/{INVOCATION}/decision",
        json={"approved": True, "note": "x"},
        headers={"Authorization": "Bearer browser"},
    )
    assert unknown_field.status_code == 422
