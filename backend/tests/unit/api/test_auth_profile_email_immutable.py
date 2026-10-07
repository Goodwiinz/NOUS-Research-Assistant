"""GOO-405: ``PUT /api/v1/auth/me`` must not let the client set ``users.email``.

Before the fix, ``ProfileUpdate.email`` flowed straight into
``AuthService.update_user_profile``, whose only check was "no other row holds
this address". Any signed-in user could therefore claim a victim's address
before the victim's first login. ``users.email`` is UNIQUE, so the victim's JIT
provisioning INSERT then hit an ``IntegrityError``, the re-select by id found
nothing, and every request from the victim returned 401
"User not found or inactive". The attacker could also show the victim's
address to collaborators.

``users.email`` is now owned by the identity provider. It is written once, at
JIT provisioning, from the verified token. The ``email`` field stays in the
request schema so the OpenAPI contract does not break, but only an echo of the
caller's current address (compared case-insensitively) is accepted. Any other
value rejects the WHOLE request with 400 before anything is written.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth.auth import router as auth_router
from src.core.dependencies import get_current_user
from src.models.user import User, UserRole
from src.services.security.auth_service import AuthService, get_auth_service

OWNER_EMAIL = "owner@example.com"
VICTIM_EMAIL = "victim@example.com"
PROFILE_URL = "/api/v1/auth/me"


@pytest.fixture
def user() -> User:
    return User(
        id="00000000-0000-0000-0000-000000000405",
        email=OWNER_EMAIL,
        password_hash="unused-jit-secret",
        first_name="Ada",
        last_name="Lovelace",
        role=UserRole.USER,
        is_active=True,
        is_deleted=False,
        organization_id=None,
    )


@pytest.fixture
def db() -> AsyncMock:
    # A "nobody else holds that address" lookup result, so that pre-fix the
    # squat went through (200) instead of tripping on a mock artifact.
    no_conflict = MagicMock()
    no_conflict.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=no_conflict)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def client(user: User, db: AsyncMock) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_auth_service] = lambda: AuthService(db)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.unit
def test_foreign_email_is_rejected_and_nothing_is_written(
    client: TestClient, user: User, db: AsyncMock
) -> None:
    response = client.put(PROFILE_URL, json={"email": VICTIM_EMAIL})

    assert response.status_code == 400
    assert "email" in response.json()["detail"].lower()
    assert user.email == OWNER_EMAIL
    # Rejected before the service runs: no uniqueness probe, no commit.
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.unit
def test_foreign_email_rejects_the_whole_request_including_names(
    client: TestClient, user: User, db: AsyncMock
) -> None:
    response = client.put(
        PROFILE_URL, json={"first_name": "Mallory", "email": VICTIM_EMAIL}
    )

    assert response.status_code == 400
    assert user.first_name == "Ada"
    assert user.email == OWNER_EMAIL
    db.commit.assert_not_awaited()


@pytest.mark.unit
def test_echoing_own_email_is_accepted_as_a_no_op(
    client: TestClient, user: User, db: AsyncMock
) -> None:
    response = client.put(PROFILE_URL, json={"email": OWNER_EMAIL})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == OWNER_EMAIL
    assert user.email == OWNER_EMAIL
    db.execute.assert_not_awaited()  # no "already in use" lookup any more


@pytest.mark.unit
def test_echoing_own_email_with_different_case_is_accepted(
    client: TestClient, user: User
) -> None:
    response = client.put(PROFILE_URL, json={"email": "OWNER@Example.COM"})

    assert response.status_code == 200
    assert user.email == OWNER_EMAIL


@pytest.mark.unit
def test_request_without_email_leaves_email_unchanged(
    client: TestClient, user: User, db: AsyncMock
) -> None:
    response = client.put(PROFILE_URL, json={})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == OWNER_EMAIL
    assert user.email == OWNER_EMAIL
    db.commit.assert_awaited_once()


@pytest.mark.unit
def test_name_update_still_works(client: TestClient, user: User) -> None:
    response = client.put(
        PROFILE_URL, json={"first_name": "Grace", "last_name": "Hopper"}
    )

    assert response.status_code == 200
    body = response.json()["user"]
    assert body["first_name"] == "Grace"
    assert body["last_name"] == "Hopper"
    assert body["full_name"] == "Grace Hopper"
    assert body["email"] == OWNER_EMAIL
    assert user.full_name == "Grace Hopper"


@pytest.mark.unit
def test_service_no_longer_accepts_an_email_argument() -> None:
    """Defense in depth: no future route can reuse the service as a writer of
    ``users.email``."""
    params = inspect.signature(AuthService.update_user_profile).parameters
    assert "email" not in params
