"""Transport gates for the chat handoff record (Plan 06 slice 3)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_handoff import (
    HandoffConflictBody,
    HandoffCreate,
    HandoffDTO,
)
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.handoffs import HandoffConflict, HandoffInvalid

pytestmark = pytest.mark.unit
USER, ORG, PROJECT, THREAD, GRANT, VERSION = (uuid4() for _ in range(6))
# token -> (scopes, bound thread)
GRANTS: dict[str, tuple[set[str], UUID | None]] = {
    "rw": ({"handoff:read", "handoff:write"}, THREAD),
    "ro": ({"handoff:read"}, THREAD),
    "unbound": ({"handoff:read", "handoff:write"}, None),
    "tools": ({"tools:write"}, THREAD),
}
LATEST = HandoffDTO(
    id=uuid4(),
    thread_id=THREAD,
    project_id=PROJECT,
    version=3,
    handoff_id=uuid4(),
    goal="Finish review",
    decisions=["PRISMA"],
    remaining=["Screen"],
    results=[{"artifact_version_id": VERSION, "summary": "log"}],  # type: ignore[list-item]
    harness_name="codex",
    harness_session_id=None,
    created_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
)


def _body(**values: Any) -> dict[str, Any]:
    body: dict[str, Any] = dict(
        handoff_id=str(uuid4()),
        expected_parent_version=None,
        goal="g",
        decisions=[],
        remaining=[],
        results=[],
        harness_name="codex",
    )
    body.update(values)
    return body


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer cli", "X-NOUS-Integration-Grant": token}


@pytest.fixture
def state() -> dict[str, Any]:
    return {
        "latest": LATEST,
        "save": LATEST,
        "saved": [],
        "thread": _thread(ORG),
        "read_orgs": [],
    }


def _thread(org: UUID) -> Any:
    return SimpleNamespace(
        conversation=SimpleNamespace(workspace=SimpleNamespace(organization_id=org))
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> FastAPI:
    from src.api.integrations import router
    from src.api.threads.workspace_routes import threads as thread_routes
    from src.core.config import settings
    from src.services.integrations import handoffs

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", True)

    async def resolve(_db: Any, token: str, *, required_scope: str) -> Any:
        scopes, thread = GRANTS.get(token, (set(), None))
        if required_scope not in scopes:
            raise IntegrationAccessDenied()
        return IntegrationContext(
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            thread_id=thread,
            grant_id=GRANT,
        )

    monkeypatch.setattr(
        "src.api.integrations.auth.resolve_integration_context", resolve
    )

    async def read_latest(_db: Any, context: IntegrationContext) -> Any:
        if context.thread_id is None:
            raise IntegrationAccessDenied()
        return state["latest"]

    async def save(
        _db: Any, context: IntegrationContext, payload: HandoffCreate
    ) -> Any:
        if context.thread_id is None:
            raise IntegrationAccessDenied()
        state["saved"].append((context, payload))
        if isinstance(state["save"], Exception):
            raise state["save"]
        return state["save"]

    async def read_for_thread(
        _db: Any, *, organization_id: UUID, thread_id: UUID
    ) -> Any:
        state["read_orgs"].append(organization_id)
        return state["latest"]

    async def get_thread(_db: Any, thread_id: UUID, user_id: UUID, **_: Any) -> Any:
        return state["thread"]

    monkeypatch.setattr(handoffs, "read_latest", read_latest)
    monkeypatch.setattr(handoffs, "save", save)
    monkeypatch.setattr(handoffs, "read_latest_for_thread", read_for_thread)
    monkeypatch.setattr(thread_routes.workspace_access, "get_thread", get_thread)

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.include_router(thread_routes.standalone_router)
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


URL = "/api/v1/integrations/handoffs"


def test_read_and_write_with_scoped_grant(
    client: TestClient, state: dict[str, Any]
) -> None:
    got = client.get(f"{URL}/latest", headers=_headers("ro"))
    assert got.status_code == 200 and got.json()["version"] == 3
    saved = client.post(URL, json=_body(), headers=_headers("rw"))
    assert saved.status_code == 200, saved.text
    context, _payload = state["saved"][0]
    # The chat comes from the grant, never from the request.
    assert context.thread_id == THREAD and context.grant_id == GRANT


@pytest.mark.parametrize(
    "token,method,expected",
    [
        ("ro", "post", 403),
        ("tools", "get", 403),
        ("tools", "post", 403),
        ("unknown", "get", 403),
        ("unbound", "get", 403),
        ("unbound", "post", 403),
    ],
)
def test_scope_and_binding_matrix(
    client: TestClient, token: str, method: str, expected: int
) -> None:
    if method == "get":
        response = client.get(f"{URL}/latest", headers=_headers(token))
    else:
        response = client.post(URL, json=_body(), headers=_headers(token))
    assert response.status_code == expected
    assert response.json() == {"detail": "Integration access denied"}


def test_browser_token_cannot_use_integration_routes(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    with TestClient(app) as client:
        assert client.get(f"{URL}/latest", headers=_headers("rw")).status_code == 403


def test_flag_off_blocks_writes_not_reads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "NOUS_MCP_ENABLED", False)
    assert client.post(URL, json=_body(), headers=_headers("rw")).status_code == 503
    assert state["saved"] == []
    assert client.get(f"{URL}/latest", headers=_headers("rw")).status_code == 200
    state["latest"] = None
    assert client.get(f"{URL}/latest", headers=_headers("rw")).status_code == 404


def test_conflict_returns_latest_dto(client: TestClient, state: dict[str, Any]) -> None:
    state["save"] = HandoffConflict(LATEST)
    response = client.post(URL, json=_body(), headers=_headers("rw"))
    assert response.status_code == 409
    body = HandoffConflictBody.model_validate(response.json())
    assert body.latest == LATEST and "latest" in body.detail


def test_conflict_without_latest_uses_same_envelope(
    client: TestClient, state: dict[str, Any]
) -> None:
    state["save"] = HandoffConflict(None)
    response = client.post(
        URL, json=_body(expected_parent_version=1), headers=_headers("rw")
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Handoff version conflict", "latest": None}


def test_foreign_version_is_422(client: TestClient, state: dict[str, Any]) -> None:
    state["save"] = HandoffInvalid(
        "Result references an artifact version outside the project"
    )
    response = client.post(URL, json=_body(), headers=_headers("rw"))
    assert response.status_code == 422
    assert "outside the project" in response.json()["detail"]


@pytest.mark.parametrize(
    "bad",
    [
        {"remaining": ["r" * 501]},
        {"remaining": ["r"] * 51},
        {"results": [{"artifact_version_id": str(VERSION), "summary": "s" * 501}]},
        {"goal": "g" * 2001},
        {"thread_id": str(THREAD)},
    ],
)
def test_over_bound_payload_is_422(
    client: TestClient, state: dict[str, Any], bad: dict[str, Any]
) -> None:
    response = client.post(URL, json=_body(**bad), headers=_headers("rw"))
    assert response.status_code == 422
    assert state["saved"] == []


def test_browser_route_reads_latest(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=False
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v2/threads/{THREAD}/handoff")
    assert response.status_code == 200
    assert response.json()["version"] == 3


def test_browser_route_404_for_outsider_and_empty(
    client: TestClient, state: dict[str, Any]
) -> None:
    state["thread"] = None
    response = client.get(f"/api/v2/threads/{THREAD}/handoff")
    assert response.status_code == 404
    assert response.json() == {"detail": "Thread not found"}
    state["thread"] = _thread(ORG)
    state["latest"] = None
    response = client.get(f"/api/v2/threads/{THREAD}/handoff")
    assert response.status_code == 404
    assert response.json() == {"detail": "No handoff for this chat"}


def test_browser_route_scopes_by_the_chats_workspace_org(
    app: FastAPI, state: dict[str, Any]
) -> None:
    """A workspace member from another org reads the chat's handoff."""
    thread_org = uuid4()
    state["thread"] = _thread(thread_org)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=uuid4()
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v2/threads/{THREAD}/handoff")
    assert response.status_code == 200
    assert state["read_orgs"] == [thread_org]


# The legacy workspace owner's organization, distinct from the caller's ORG so
# a fallback to current_user.organization_id cannot pass for the owner's.
OWNER_ORG = uuid4()


class _OwnerOrgSession:
    """Answers the one query workspace_organization_id makes for a legacy
    workspace: the owner's organization, or None when the owner has none."""

    def __init__(self, owner_org: UUID | None) -> None:
        self.owner_org = owner_org

    async def scalar(self, _statement: Any) -> UUID | None:
        return self.owner_org


@pytest.mark.parametrize(
    ("owner_org", "status", "read_orgs"),
    [(OWNER_ORG, 200, [OWNER_ORG]), (None, 404, [])],
    ids=["owner-has-org", "owner-has-none"],
)
def test_browser_route_reads_a_legacy_workspace_under_its_owners_org(
    app: FastAPI,
    state: dict[str, Any],
    owner_org: UUID | None,
    status: int,
    read_orgs: list[UUID],
) -> None:
    """WG-2c: a workspace with no organization is its owner's, never NULL
    and never the caller's."""
    state["thread"] = SimpleNamespace(
        conversation=SimpleNamespace(
            workspace=SimpleNamespace(organization_id=None, owner_id=USER)
        )
    )

    async def owner_db() -> AsyncIterator[Any]:
        yield _OwnerOrgSession(owner_org)

    app.dependency_overrides[get_db] = owner_db
    with TestClient(app) as client:
        response = client.get(f"/api/v2/threads/{THREAD}/handoff")
    assert response.status_code == status
    if status == 404:
        assert response.json() == {"detail": "No handoff for this chat"}
    assert state["read_orgs"] == read_orgs
