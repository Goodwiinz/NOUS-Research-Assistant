"""Behavioral contracts joining PR #1722 auth with response-owned DB sessions."""

import asyncio
import time
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from jose import jwt
from sqlalchemy import select
from starlette.testclient import TestClient

from src.core.config import settings
from src.core.database import get_db
from src.core.security import TokenData, get_current_user_token
from src.core.user_provisioning import SupabaseEmailLookupError
from src.middleware import multi_tenancy
from src.models.organization import Organization
from src.models.user import User


@pytest.fixture
def signed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Use real offline JWT verification and downstream HTTPBearer dependency."""
    secret = "merge-contract-test-signing-key-not-for-production"
    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", secret)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")
    return str(
        jwt.encode(
            {
                "sub": "user-1",
                "aud": "authenticated",
                "app_metadata": {},
                # verify_token requires exp (audit I21).
                "exp": int(time.time()) + 3600,
            },
            secret,
            algorithm="HS256",
        )
    )


class _Result:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalars(self) -> "_Result":
        return self

    def first(self) -> Any:
        return self.row


class _Session:
    """Track the DB boundary while executing real middleware/dependency code."""

    def __init__(self, events: list[str], name: str, fail_entity: Any = None) -> None:
        self.events = events
        self.name = name
        self.fail_entity = fail_entity
        self.closed = False

    async def __aenter__(self) -> "_Session":
        self.events.append(f"{self.name}:open")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True
        self.events.append(f"{self.name}:close")

    async def execute(self, statement: Any) -> _Result:
        assert not self.closed, "response body queried a closed DB session"
        entity = statement.column_descriptions[0]["entity"]
        self.events.append(f"{self.name}:query:{entity.__name__}")
        await asyncio.sleep(0)
        if entity is self.fail_entity:
            raise RuntimeError("S3CR3T-INTERNAL-DB")
        if entity is User:
            return _Result(User(id="user-1", organization_id="org-1", role="user"))
        assert entity is Organization
        return _Result(Organization(id="org-1", is_active=True))


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_stream_uses_response_session_and_keeps_tenant_context(
    monkeypatch: pytest.MonkeyPatch, signed_token: str, scheme: str
) -> None:
    """Validation closes before route/dependencies; get_db lives through body.

    The real JWT, HTTPBearer, tenant resolver, org validator and get_db run.
    Only the external database boundary is replaced. Both streamed chunks
    query the route session and read the inherited tenant ContextVars.
    """
    from src.core import database

    events: list[str] = []
    validation_db = _Session(events, "validation")
    response_db = _Session(events, "response")
    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", lambda: validation_db)
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: response_db)
    app = FastAPI()
    app.add_middleware(multi_tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/research-engine/runs/r1/stream")
    async def stream(
        request: Request,
        db: Any = Depends(get_db),
        token: TokenData = Depends(get_current_user_token),
    ) -> StreamingResponse:
        assert token.user_id == "user-1"
        assert validation_db.closed, "validation session must close before call_next"
        assert not hasattr(request.state, "db"), "middleware session leaked to route"
        assert db is response_db and db is not validation_db
        assert request.state.tenant_id == "org-1"
        assert request.state.user_id == "user-1"
        assert request.state.user_role == "user"
        events.append("route")

        async def body() -> Any:
            for chunk in ("first", "last"):
                await db.execute(select(User))
                assert multi_tenancy.get_current_tenant_id() == "org-1"
                assert multi_tenancy.get_current_user_id() == "user-1"
                assert multi_tenancy.get_current_user_role() == "user"
                events.append(f"body:{chunk}")
                yield f"data: {chunk}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    response = TestClient(app).get(
        "/api/v1/research-engine/runs/r1/stream",
        headers={"Authorization": f"{scheme} {signed_token}"},
    )

    assert response.status_code == 200, response.text
    assert response.text == "data: first\n\ndata: last\n\n"
    assert events == [
        "validation:open",
        "validation:query:User",
        "validation:query:Organization",
        "validation:close",
        "response:open",
        "route",
        "response:query:User",
        "body:first",
        "response:query:User",
        "body:last",
        "response:close",
    ]
    assert validation_db.closed and response_db.closed
    assert multi_tenancy.get_current_tenant_id() is None
    assert multi_tenancy.get_current_user_id() is None
    assert multi_tenancy.get_current_user_role() is None


@pytest.mark.parametrize(
    "authorization", [None, "Basic abc", "Bearer", "Bearer ", "bearer bad-token"]
)
def test_invalid_credentials_rejected_before_session(
    monkeypatch: pytest.MonkeyPatch, authorization: str | None
) -> None:
    opened: list[bool] = []
    reached: list[bool] = []

    def forbidden_session() -> Any:
        opened.append(True)
        raise AssertionError("invalid credentials opened DB session")

    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", forbidden_session)
    app = FastAPI()
    app.add_middleware(multi_tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/threads")
    async def protected() -> dict[str, bool]:
        reached.append(True)
        return {"ok": True}

    headers = {} if authorization is None else {"Authorization": authorization}
    response = TestClient(app).get("/api/v1/threads", headers=headers)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "authentication_error"
    assert opened == [] and reached == []


@pytest.mark.parametrize("fail_entity", [User, Organization])
def test_lookup_outage_returns_sanitized_500_and_closes_validation_session(
    monkeypatch: pytest.MonkeyPatch, signed_token: str, fail_entity: Any
) -> None:
    events: list[str] = []
    validation_db = _Session(events, "validation", fail_entity)
    reached: list[bool] = []
    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", lambda: validation_db)
    app = FastAPI()
    app.add_middleware(multi_tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/threads")
    async def protected() -> dict[str, bool]:
        reached.append(True)
        return {"ok": True}

    response = TestClient(app).get(
        "/api/v1/threads", headers={"Authorization": f"Bearer {signed_token}"}
    )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["type"] == "internal_error"
    assert response.json()["error"]["message"] == (
        "Internal server error during tenant validation"
    )
    assert "S3CR3T" not in response.text
    assert reached == [] and validation_db.closed


def test_downstream_exception_is_not_relabelled_tenant_failure(
    monkeypatch: pytest.MonkeyPatch, signed_token: str
) -> None:
    validation_db = _Session([], "validation")
    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", lambda: validation_db)
    app = FastAPI()
    app.add_middleware(multi_tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/threads")
    async def protected() -> None:
        raise RuntimeError("route-specific-failure")

    with pytest.raises(RuntimeError, match="route-specific-failure"):
        TestClient(app).get(
            "/api/v1/threads", headers={"Authorization": f"Bearer {signed_token}"}
        )
    assert validation_db.closed


class _ProvisioningSession(_Session):
    """Database seam for the real JIT provisioner, including commit/rollback."""

    def __init__(self, rows: dict[Any, Any], events: list[str], name: str) -> None:
        super().__init__(events, name)
        self.rows = rows
        self.pending: list[Any] = []

    async def __aexit__(self, *exc: object) -> None:
        self.pending.clear()  # AsyncSession.close rolls back uncommitted work.
        await super().__aexit__(*exc)

    async def execute(self, statement: Any) -> _Result:
        assert not self.closed
        entity = statement.column_descriptions[0]["entity"]
        self.events.append(f"{self.name}:query:{entity.__name__}")
        return _Result(self.rows[entity])

    def add(self, row: Any) -> None:
        self.pending.append(row)

    async def flush(self) -> None:
        self.events.append(f"{self.name}:flush")
        if self.rows.get("provisioning_error") is not None:
            raise self.rows["provisioning_error"]

    async def commit(self) -> None:
        for row in self.pending:
            self.rows[type(row)] = row
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending.clear()


@pytest.fixture
def provisioning_db(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[Any, Any], list[_ProvisioningSession]]:
    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email",
        lambda _user_id: "user@example.com",
    )
    rows: dict[Any, Any] = {
        User: None,
        Organization: Organization(
            id="org-1", name="user-user-1 Organization", is_active=True
        ),
    }
    sessions: list[_ProvisioningSession] = []

    def factory() -> _ProvisioningSession:
        session = _ProvisioningSession(rows, [], f"session-{len(sessions)}")
        sessions.append(session)
        return session

    monkeypatch.setattr(multi_tenancy, "AsyncSessionLocal", factory)
    return rows, sessions


def _token_with_org(organization_id: str | None) -> str:
    return str(
        jwt.encode(
            {
                "sub": "user-1",
                "aud": "authenticated",
                "app_metadata": {"organization_id": organization_id},
                "exp": int(time.time()) + 3600,
            },
            settings.SUPABASE_JWT_SECRET,
            algorithm="HS256",
        )
    )


def _tenant_probe(reached: list[str | None]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(multi_tenancy.MultiTenancyMiddleware)

    @app.get("/api/v1/threads")
    async def protected(
        token: TokenData = Depends(get_current_user_token),
    ) -> dict[str, str | None]:
        assert token.user_id == "user-1"
        tenant_id = multi_tenancy.get_current_tenant_id()
        reached.append(tenant_id)
        return {"tenant_id": tenant_id}

    return app


@pytest.mark.parametrize(
    "embedded_org,db_org", [(None, None), ("org-old", None), ("org-old", "org-new")]
)
def test_existing_user_membership_comes_only_from_db(
    signed_token: str,
    provisioning_db: tuple[dict[Any, Any], list[_ProvisioningSession]],
    embedded_org: str | None,
    db_org: str | None,
) -> None:
    """A cleared membership cannot be resurrected from a stale signed claim."""
    rows, sessions = provisioning_db
    user = User(
        id="user-1",
        organization_id=db_org,
        role="user",
        is_active=True,
        is_deleted=False,
    )
    rows[User] = user
    rows[Organization] = Organization(id=db_org or embedded_org, is_active=True)
    reached: list[str | None] = []
    response = TestClient(_tenant_probe(reached)).get(
        "/api/v1/threads",
        headers={"Authorization": f"Bearer {_token_with_org(embedded_org)}"},
    )

    if db_org is None:
        assert response.status_code == 401, response.text
        assert response.headers["www-authenticate"] == "Bearer"
        assert reached == [], "cleared DB membership reached a tenant route"
    else:
        assert response.status_code == 200, response.text
        assert reached == [db_org]
    assert rows[User] is user and user.organization_id == db_org
    assert all(s.closed and not s.pending for s in sessions)
    assert not any(event.endswith(":flush") for s in sessions for event in s.events)


@pytest.mark.parametrize("embedded_org", [None, "org-old"])
def test_jit_provisioning_db_failure_is_sanitized_500(
    signed_token: str,
    provisioning_db: tuple[dict[Any, Any], list[_ProvisioningSession]],
    embedded_org: str | None,
) -> None:
    """Successful JWT verification/empty user lookup plus failed DB flush is 500."""
    rows, sessions = provisioning_db
    rows["provisioning_error"] = RuntimeError("S3CR3T-PROVISIONING-DB")
    rows[Organization].id = embedded_org or "org-1"
    reached: list[str | None] = []
    response = TestClient(_tenant_probe(reached)).get(
        "/api/v1/threads",
        headers={"Authorization": f"Bearer {_token_with_org(embedded_org)}"},
    )

    assert response.status_code == 500, response.text
    assert response.json()["error"] == {
        "message": "Internal server error during tenant validation",
        "status_code": 500,
        "type": "internal_error",
    }
    assert "S3CR3T" not in response.text
    assert reached == [] and rows[User] is None
    assert all(s.closed and not s.pending for s in sessions)
    assert any(event.endswith(":flush") for s in sessions for event in s.events)


@pytest.mark.parametrize("embedded_org", [None, "org-old"])
def test_jit_provider_outage_remains_retryable_through_tenant_middleware(
    monkeypatch: pytest.MonkeyPatch,
    signed_token: str,
    provisioning_db: tuple[dict[Any, Any], list[_ProvisioningSession]],
    embedded_org: str | None,
) -> None:
    rows, sessions = provisioning_db

    def unavailable(_user_id: str) -> None:
        raise SupabaseEmailLookupError("provider unavailable")

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", unavailable
    )
    rows[Organization].id = embedded_org or "org-1"
    reached: list[str | None] = []
    response = TestClient(_tenant_probe(reached)).get(
        "/api/v1/threads",
        headers={"Authorization": f"Bearer {_token_with_org(embedded_org)}"},
    )

    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"] == {
        "message": "Identity provider temporarily unavailable",
        "status_code": 503,
        "type": "service_unavailable",
    }
    assert reached == [] and rows[User] is None
    assert all(session.closed and not session.pending for session in sessions)


@pytest.mark.parametrize("embedded_org", [None, "org-old"])
def test_new_user_jit_keeps_authorized_token_org_metadata(
    signed_token: str,
    provisioning_db: tuple[dict[Any, Any], list[_ProvisioningSession]],
    embedded_org: str | None,
) -> None:
    rows, sessions = provisioning_db
    rows[Organization].id = embedded_org or "org-1"
    reached: list[str | None] = []
    response = TestClient(_tenant_probe(reached)).get(
        "/api/v1/threads",
        headers={"Authorization": f"Bearer {_token_with_org(embedded_org)}"},
    )

    assert response.status_code == 200, response.text
    assert rows[User].organization_id == (embedded_org or "org-1")
    assert reached == [rows[User].organization_id]
    assert all(s.closed and not s.pending for s in sessions)


@pytest.mark.parametrize("embedded_org", [None, "org-old"])
def test_jit_failure_does_not_override_resolved_active_db_user(
    monkeypatch: pytest.MonkeyPatch,
    signed_token: str,
    provisioning_db: tuple[dict[Any, Any], list[_ProvisioningSession]],
    embedded_org: str | None,
) -> None:
    """A transient provisioning failure is irrelevant once DB resolution succeeds."""
    from unittest.mock import AsyncMock

    rows, sessions = provisioning_db
    rows[User] = User(
        id="user-1",
        organization_id="org-1",
        role="user",
        is_active=True,
        is_deleted=False,
    )
    provisioner = AsyncMock(side_effect=RuntimeError("S3CR3T-PROVISIONING-DB"))
    monkeypatch.setattr(multi_tenancy, "ensure_user_and_org", provisioner)
    reached: list[str | None] = []
    response = TestClient(_tenant_probe(reached)).get(
        "/api/v1/threads",
        headers={"Authorization": f"Bearer {_token_with_org(embedded_org)}"},
    )

    assert response.status_code == 200, response.text
    assert reached == ["org-1"]
    assert provisioner.await_count == (1 if embedded_org else 0)
    assert all(s.closed for s in sessions)
