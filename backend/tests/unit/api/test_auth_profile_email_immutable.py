"""GOO-405: ``PUT /api/v1/auth/me`` must not let the client set ``users.email``.

Before the fix, ``ProfileUpdate.email`` flowed straight into
``AuthService.update_user_profile``, whose only check was "no other row holds
this address". Any signed-in user could therefore claim a victim's address
before the victim's first login. ``users.email`` is UNIQUE, so the victim's JIT
provisioning INSERT then hit an ``IntegrityError``, the re-select by id found
nothing, and every request from the victim returned 401
"User not found or inactive". The attacker could also show the victim's
address to collaborators.

``users.email`` is now owned by the identity provider. It is initialized at JIT
provisioning and reconciled from the provider's current confirmed account when
it changes. The ``email`` field stays in the request schema so the OpenAPI
contract does not break, but only an echo of the caller's current address
(compared case-insensitively) is accepted. Any other value rejects the WHOLE
request with 400 before anything is written.

GOO-405 reconciliation backoff mutation evidence (run from ``backend/``):

* Guard at ``src/services/security/auth_service.py:343``: a provider-disproved
  JWT email claim is remembered. Removing that call makes
  ``python -m pytest -q tests/unit/api/test_auth_profile_email_immutable.py::test_disproved_stale_claim_does_not_repeat_provider_lookup`` fail
  because it performs a second provider lookup.
* Guard at ``src/services/security/auth_service.py:353``: a confirmed email
  collision is remembered after rollback. Removing that call makes
  ``python -m pytest -q tests/unit/api/test_auth_profile_email_immutable.py::test_provider_email_conflict_backs_off_repeated_sync`` fail
  because it repeats the lookup and commit.
* Guard at ``src/services/security/auth_service.py:336``: provider lookup
  failures receive a short backoff. Removing it makes
  ``python -m pytest -q tests/unit/api/test_auth_profile_email_immutable.py::test_provider_lookup_failure_leaves_email_unchanged`` fail
  because every request retries the failed lookup.
* Guard at ``src/services/security/auth_service.py:97``: the lock key is the
  subject, so different JWT claims for one account are serialized. Making the
  key unique per request makes
  ``python -m pytest -q tests/unit/api/test_auth_profile_email_immutable.py::test_different_claims_for_same_subject_serialize_without_holding_connections``
  fail because the stale provider result can overwrite the newer result.
* Guard at ``src/services/security/auth_service.py:316``: the initial read
  transaction is ended before waiting on the per-subject lock. Removing this
  rollback makes
  ``python -m pytest -q tests/unit/api/test_auth_profile_email_immutable.py::test_different_claims_for_same_subject_serialize_without_holding_connections``
  fail because the waiting request has not released its session connection.
* Guard at ``src/services/security/auth_service.py:328``: the refreshed read
  transaction is ended before the provider request. Removing this rollback
  makes the same focused test fail because the first lookup starts while its
  session still owns a connection.

Each proof disables one guard, observes its focused failure, then restores the
source byte-for-byte before running the focused test on the restored source.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Iterator
from threading import Event as ThreadingEvent
from time import sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.api.auth.auth import router as auth_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verified_supabase_email_change_syncs_by_subject_id(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current provider email is reconciled before /auth/me sees the user."""
    setattr(user, "id", uuid.uuid4())
    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email",
        lambda _user_id: "new-owner@example.com",
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    token_data = TokenData(user_id=str(user.id), email="new-owner@example.com")

    current_user = await get_current_user(token_data, db)

    assert current_user is user
    assert user.email == "new-owner@example.com"
    db.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_supabase_claim_cannot_roll_email_back(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JWT A must not overwrite the provider's current address B."""
    setattr(user, "id", uuid.uuid4())
    setattr(user, "email", "intermediate@example.com")
    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email",
        lambda _user_id: "current@example.com",
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    token_data = TokenData(user_id=str(user.id), email=OWNER_EMAIL)

    current_user = await get_current_user(token_data, db)

    assert current_user is user
    assert user.email == "current@example.com"
    db.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disproved_stale_claim_does_not_repeat_provider_lookup(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider-disproved claim is cached by subject and claim, briefly."""
    setattr(user, "id", uuid.uuid4())
    setattr(user, "email", "current@example.com")
    lookups = 0

    def current_email(_user_id: str) -> str:
        nonlocal lookups
        lookups += 1
        return "current@example.com"

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", current_email
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    service = AuthService(db)

    await service.sync_user_email_from_provider(user, OWNER_EMAIL)
    await service.sync_user_email_from_provider(user, OWNER_EMAIL)

    assert lookups == 1
    assert user.email == "current@example.com"
    db.commit.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_reconciliation_uses_one_provider_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent requests for the same claim share one provider round trip."""
    subject_id = str(uuid.uuid4())
    first_user = User(
        id=subject_id,
        email="intermediate@example.com",
        password_hash="unused-jit-secret",
        first_name="Ada",
        last_name="Lovelace",
        role=UserRole.USER,
        is_active=True,
        is_deleted=False,
        organization_id=None,
    )
    stale_loaded_user = User(
        id=subject_id,
        email="intermediate@example.com",
        password_hash="unused-jit-secret",
        first_name="Ada",
        last_name="Lovelace",
        role=UserRole.USER,
        is_active=True,
        is_deleted=False,
        organization_id=None,
    )
    refreshed_user = SimpleNamespace(id=subject_id, email="current@example.com")
    first_db = AsyncMock()
    second_db = AsyncMock()
    first_result = MagicMock()
    first_result.scalars.return_value.first.return_value = first_user
    first_db.execute.return_value = first_result
    refreshed_result = MagicMock()
    refreshed_result.scalars.return_value.first.return_value = refreshed_user
    second_db.execute.return_value = refreshed_result
    lookups = 0

    def current_email(_user_id: str) -> str:
        nonlocal lookups
        lookups += 1
        sleep(0.05)
        return "current@example.com"

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", current_email
    )
    first_service = AuthService(first_db)
    second_service = AuthService(second_db)

    reconciled_users = await asyncio.gather(
        first_service.sync_user_email_from_provider(first_user, OWNER_EMAIL),
        second_service.sync_user_email_from_provider(stale_loaded_user, OWNER_EMAIL),
    )
    first_user_result = reconciled_users[0]
    second_user_result = reconciled_users[1]

    assert lookups == 1
    assert first_user_result is not None
    assert first_user_result.email == "current@example.com"
    assert second_user_result is refreshed_user
    first_db.commit.assert_awaited_once()
    second_db.commit.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_different_claims_for_same_subject_serialize_without_holding_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different JWT snapshots cannot race writes or hold DB connections."""
    subject_id = str(uuid.uuid4())
    user = User(
        id=subject_id,
        email="stored@example.com",
        password_hash="unused-jit-secret",
        first_name="Ada",
        last_name="Lovelace",
        role=UserRole.USER,
        is_active=True,
        is_deleted=False,
        organization_id=None,
    )
    first_db = AsyncMock()
    second_db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    first_db.execute.return_value = result
    second_db.execute.return_value = result
    first_lookup_started = ThreadingEvent()
    release_first_lookup = ThreadingEvent()
    provider_calls: list[str] = []
    first_session_rollbacks_at_lookup: list[int] = []

    def current_email(_user_id: str) -> str:
        provider_calls.append(_user_id)
        if len(provider_calls) == 1:
            first_session_rollbacks_at_lookup.append(first_db.rollback.await_count)
            first_lookup_started.set()
            assert release_first_lookup.wait(timeout=2)
            return "middle@example.com"
        return "latest@example.com"

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", current_email
    )
    first_service = AuthService(first_db)
    second_service = AuthService(second_db)
    first_request = asyncio.create_task(
        first_service.sync_user_email_from_provider(user, "claim-b@example.com")
    )
    assert await asyncio.to_thread(first_lookup_started.wait, 2)
    second_request = asyncio.create_task(
        second_service.sync_user_email_from_provider(user, "claim-c@example.com")
    )
    await asyncio.sleep(0.05)
    provider_calls_while_waiting = len(provider_calls)
    second_rollbacks_while_waiting = second_db.rollback.await_count
    release_first_lookup.set()
    results = await asyncio.gather(first_request, second_request)
    first_result = results[0]
    second_result = results[1]

    assert len(provider_calls) == 2
    assert provider_calls_while_waiting == 1
    assert second_rollbacks_while_waiting == 1
    assert first_session_rollbacks_at_lookup == [2]
    assert first_result is user
    assert second_result is user
    assert user.email == "latest@example.com"
    first_db.commit.assert_awaited_once()
    second_db.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_email_conflict_backs_off_repeated_sync(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider-confirmed local collision is retried only after backoff."""
    setattr(user, "id", uuid.uuid4())
    restored_user = SimpleNamespace(id=user.id, email=OWNER_EMAIL)
    current_result = MagicMock()
    current_result.scalars.return_value.first.return_value = user
    restored_result = MagicMock()
    restored_result.scalars.return_value.first.return_value = restored_user
    retried_user = SimpleNamespace(id=user.id, email=OWNER_EMAIL)
    retried_result = MagicMock()
    retried_result.scalars.return_value.first.return_value = retried_user
    db.execute = AsyncMock(
        side_effect=[
            current_result,
            current_result,
            restored_result,
            restored_result,
            restored_result,
            retried_result,
        ]
    )
    db.commit.side_effect = IntegrityError("UPDATE users", {}, Exception("duplicate"))
    lookups = 0

    def current_email(_user_id: str) -> str:
        nonlocal lookups
        lookups += 1
        return VICTIM_EMAIL

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", current_email
    )
    service = AuthService(db)

    current_user = await service.sync_user_email_from_provider(user, VICTIM_EMAIL)
    assert current_user is not None
    second_user = await service.sync_user_email_from_provider(
        current_user, VICTIM_EMAIL
    )

    assert lookups == 1
    db.commit.assert_awaited_once()
    assert db.execute.await_count == 4
    assert db.rollback.await_count == 4
    assert current_user is restored_user
    assert second_user is restored_user


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_lookup_failure_leaves_email_unchanged(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token claim alone is never enough to change the stored address."""
    setattr(user, "id", uuid.uuid4())
    lookups = 0

    def unavailable(_user_id: str) -> None:
        nonlocal lookups
        lookups += 1
        return None

    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email", unavailable
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    token_data = TokenData(user_id=str(user.id), email=VICTIM_EMAIL)

    current_user = await get_current_user(token_data, db)

    assert current_user is user
    assert user.email == OWNER_EMAIL
    await AuthService(db).sync_user_email_from_provider(user, VICTIM_EMAIL)
    assert lookups == 1
    db.commit.assert_not_awaited()


@pytest.mark.unit
def test_profile_accepts_the_email_from_the_current_provider_record(
    user: User, db: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth dependency reconciles current provider state before route checks."""
    setattr(user, "id", uuid.uuid4())
    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email",
        lambda _user_id: "new-owner@example.com",
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    token_data = TokenData(user_id=str(user.id), email="new-owner@example.com")

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user_token] = lambda: token_data
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_auth_service] = lambda: AuthService(db)
    try:
        with TestClient(app) as test_client:
            response = test_client.put(
                PROFILE_URL, json={"email": "new-owner@example.com"}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "new-owner@example.com"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_cli_email_cannot_overwrite_provider_email(
    user: User, db: AsyncMock
) -> None:
    """CLI tokens contain a minted snapshot, so they are not sync authority."""
    setattr(user, "email", "current@example.com")
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db.execute.return_value = result
    token_data = TokenData(user_id=str(user.id), email=OWNER_EMAIL, is_cli=True)

    current_user = await get_current_user(token_data, db)

    assert current_user is user
    assert user.email == "current@example.com"
    db.commit.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verified_email_conflict_keeps_user_bound_to_subject(
    user: User,
    db: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unique-email collision fails closed without looking up by email."""
    setattr(user, "id", uuid.uuid4())
    monkeypatch.setattr(
        "src.core.user_provisioning.get_verified_supabase_email",
        lambda _user_id: VICTIM_EMAIL,
    )
    current_result = MagicMock()
    current_result.scalars.return_value.first.return_value = user
    restored_user = SimpleNamespace(id=user.id, email=OWNER_EMAIL)
    restored_result = MagicMock()
    restored_result.scalars.return_value.first.return_value = restored_user
    db.execute = AsyncMock(
        side_effect=[current_result, current_result, current_result, restored_result]
    )
    db.commit.side_effect = IntegrityError("UPDATE users", {}, Exception("duplicate"))
    token_data = TokenData(user_id=str(user.id), email=VICTIM_EMAIL)

    with caplog.at_level("WARNING", logger="src.services.security.auth_service"):
        current_user = await get_current_user(token_data, db)

    assert current_user is restored_user
    assert current_user.email == OWNER_EMAIL
    assert db.rollback.await_count == 3
    assert db.execute.await_count == 4
    refetch_where = str(db.execute.await_args_list[-1].args[0]).split("WHERE", 1)[1]
    assert "users.id" in refetch_where
    assert "users.email" not in refetch_where
    warning = next(record.getMessage() for record in caplog.records)
    assert str(user.id) in warning
    assert VICTIM_EMAIL not in warning
