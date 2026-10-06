"""I5: API-key ``allowed_endpoints`` scope enforcement.

``get_api_key_data`` must honor the key's stored ``allowed_endpoints``
config (JSON array string column, ``api_keys.allowed_endpoints``):

- ``None`` (never configured)  -> all endpoints allowed (back-compat)
- ``[]`` (explicit empty list) -> deny all (admin scoped key to nothing)
- entries                      -> exact path or directory-prefix match
- malformed / non-list JSON    -> deny all (fail closed) + error log

Enforcement runs AFTER the hash lookup + expiry checks and BEFORE the
rate-limit check, so denied requests never consume rate-limit quota.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.api_keys import create_api_key
from src.core.api_key_auth import (
    APIKey,
    APIKeyCreate,
    APIKeyData,
    _endpoint_allowed,
    _parse_allowed_endpoints,
    generate_api_key,
    get_api_key_data,
)
from src.models.user import User

pytestmark = pytest.mark.unit

Env = tuple[APIKey, str, MagicMock, MagicMock, AsyncMock]


def _make_key(allowed_endpoints: Optional[str] = None) -> tuple[APIKey, str]:
    raw_key, key_hash = generate_api_key()
    record = APIKey(
        id="test-id",
        name="Scoped Key",
        key_hash=key_hash,
        key_prefix=raw_key[:8],
        is_active=True,
        rate_limit_per_hour=100,
        usage_count=0,
        organization_id="org-1",
        allowed_endpoints=allowed_endpoints,
        expires_at=None,
        created_at=datetime.utcnow(),
    )
    return record, raw_key


def _make_env(
    allowed_endpoints: Optional[str] = None,
    record: Optional[APIKey] = None,
    raw_key: Optional[str] = None,
) -> Env:
    """Return (record, raw_key, request, credentials, db) wired so
    the DB lookup resolves the key record."""
    if record is None or raw_key is None:
        record, raw_key = _make_key(allowed_endpoints)

    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    request.url.path = "/api/v1/search/authenticated/hybrid"

    credentials = MagicMock(spec=HTTPAuthorizationCredentials)
    credentials.credentials = raw_key

    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.first.return_value = record
    db.execute.return_value = result

    return record, raw_key, request, credentials, db


async def _validate(
    env: Env, path: Optional[str] = None
) -> tuple[APIKeyData, str, MagicMock]:
    record, raw_key, request, credentials, db = env
    if path is not None:
        request.url.path = path
    with patch("src.core.api_key_auth.api_key_auth") as mock_auth:
        mock_auth.check_rate_limit = AsyncMock(return_value=True)
        mock_auth.track_usage = AsyncMock()
        mock_auth.get_current_usage = AsyncMock(return_value=0)
        data, endpoint = await get_api_key_data(request, credentials, db)
    return data, endpoint, mock_auth


# --- _endpoint_allowed matching semantics -------------------------------


def test_endpoint_allowed_none_allows_everything() -> None:
    assert _endpoint_allowed(None, "/any/path") is True


def test_endpoint_allowed_empty_list_denies_everything() -> None:
    assert _endpoint_allowed([], "/any/path") is False


def test_endpoint_allowed_exact_match() -> None:
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/search") is True


def test_endpoint_allowed_prefix_entry_permits_subpaths() -> None:
    assert (
        _endpoint_allowed(["/api/v1/search"], "/api/v1/search/authenticated/hybrid")
        is True
    )


def test_endpoint_allowed_prefix_entry_does_not_match_similar_prefix() -> None:
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/searchx") is False
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/other") is False


def test_endpoint_allowed_trailing_slash_entry_normalized() -> None:
    assert _endpoint_allowed(["/api/v1/search/"], "/api/v1/search/hybrid") is True


def test_endpoint_allowed_trailing_slash_entry_allows_exact_path() -> None:
    assert _endpoint_allowed(["/api/v1/search/"], "/api/v1/search") is True


def test_endpoint_allowed_degenerate_root_entry_denies_everything() -> None:
    """A ``/`` entry normalizes to an empty prefix — it must never become
    an allow-all wildcard."""
    assert _endpoint_allowed(["/"], "/") is False
    assert _endpoint_allowed(["/"], "/api/v1/search") is False


def test_endpoint_allowed_blank_entry_denies_everything() -> None:
    assert _endpoint_allowed([""], "/api/v1/search") is False


# --- _parse_allowed_endpoints blank-entry handling ----------------------


@pytest.mark.parametrize("raw", [json.dumps([""]), json.dumps(["  "])])
async def test_blank_entries_deny_all_and_log_error(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert _parse_allowed_endpoints(raw, key_prefix="ragabcd12") == []
    assert any(
        rec.levelname == "ERROR"
        and "allowed_endpoints" in rec.message
        and "ragabcd12***" in rec.message
        for rec in caplog.records
    )


async def test_blank_entry_config_denies_request() -> None:
    env = _make_env(json.dumps(["  "]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


# --- enforcement inside get_api_key_data --------------------------------


async def test_scoped_key_exact_match_allowed() -> None:
    env = _make_env(json.dumps(["/api/v1/search/authenticated/hybrid"]))
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"


async def test_scoped_key_prefix_allows_subpath_and_denies_other() -> None:
    env = _make_env(json.dumps(["/api/v1/search"]))
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"

    other = _make_env(json.dumps(["/api/v1/search"]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(other, path="/api/v1/other")
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "API key not permitted for this endpoint"


async def test_unscoped_key_none_allows_all() -> None:
    env = _make_env(None)
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"


async def test_empty_list_denies_all() -> None:
    env = _make_env(json.dumps([]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


async def test_malformed_json_denies_all_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _make_env("not-json{{")
    record, raw_key, _, _, _ = env
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert any(
        rec.levelname == "ERROR"
        and "allowed_endpoints" in rec.message
        and f"{record.key_prefix}***" in rec.message
        for rec in caplog.records
    )


async def test_non_list_json_denies_all() -> None:
    env = _make_env(json.dumps({"endpoints": "/api/v1/search"}))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


async def test_denied_request_does_not_consume_rate_limit_quota(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enforcement sits before the rate-limit check: denied requests must
    not touch the limiter (no quota burn, no usage tracking, no DB write)."""
    env = _make_env(json.dumps(["/api/v1/search"]))
    record, raw_key, request, credentials, db = env
    request.url.path = "/api/v1/other"

    with patch("src.core.api_key_auth.api_key_auth") as mock_auth:
        mock_auth.check_rate_limit = AsyncMock(return_value=True)
        mock_auth.track_usage = AsyncMock()
        mock_auth.get_current_usage = AsyncMock(return_value=0)
        with caplog.at_level("WARNING"):
            with pytest.raises(HTTPException):
                await get_api_key_data(request, credentials, db)

        # 403-before-429 ordering is pinned: the limiter and usage tracking
        # must never be reached on a scope denial.
        mock_auth.check_rate_limit.assert_not_called()
        mock_auth.track_usage.assert_not_called()
        mock_auth.get_current_usage.assert_not_called()

    # The denial warning must reference the redacted prefix, never the raw key.
    denial_logs = [
        rec.message for rec in caplog.records if "not permitted" in rec.message
    ]
    assert denial_logs, "expected a denial warning log"
    assert all(raw_key not in msg for msg in denial_logs)
    assert any(str(record.key_prefix) in msg for msg in denial_logs)


# --- end-to-end: router-created scoped key is usable ---------------------


async def test_router_created_scoped_key_enforced_end_to_end() -> None:
    """Create a key through the admin router with scope
    ``["/api/v1/search"]``, read the persisted column back, parse it, and
    verify the enforcement path: allowed path permitted, other path 403.

    Regression guard: the scope must be stored as real JSON
    (``json.dumps``), not the Python ``str()`` repr, which the parser
    rejects as malformed (deny-all)."""
    admin = MagicMock(spec=User)
    admin.email = "admin@example.com"
    admin.id = "admin-1"
    admin.organization_id = "org-1"

    db = AsyncMock(spec=AsyncSession)
    added: dict[str, APIKey] = {}

    def _capture_add(obj: APIKey) -> None:
        added["record"] = obj

    db.add.side_effect = _capture_add

    async def _fake_refresh(obj: APIKey) -> None:
        # Stand in for the DB-side defaults (PK, activity flag, counters).
        if obj.id is None:
            obj.id = "generated-key-id"
        if obj.is_active is None:
            obj.is_active = True
        if obj.usage_count is None:
            obj.usage_count = 0

    db.refresh.side_effect = _fake_refresh

    payload = APIKeyCreate(name="Scoped Key", allowed_endpoints=["/api/v1/search"])
    response = await create_api_key(payload, current_user=admin, db=db)
    stored_record = added["record"]

    # 1. Read back from DB: stored column is valid JSON with the scope.
    assert stored_record.allowed_endpoints == json.dumps(["/api/v1/search"])

    # 2. Parse the persisted value exactly as the auth path does.
    parsed = _parse_allowed_endpoints(
        cast(Optional[str], stored_record.allowed_endpoints),
        key_prefix=str(stored_record.key_prefix),
    )
    assert parsed == ["/api/v1/search"]

    # 3. Requests with the issued raw key honor the scope end-to-end.
    raw_key = response.api_key
    allowed_env = _make_env(record=stored_record, raw_key=raw_key)
    _, endpoint, mock_auth = await _validate(
        allowed_env, path="/api/v1/search/authenticated/hybrid"
    )
    assert endpoint == "/api/v1/search/authenticated/hybrid"
    mock_auth.track_usage.assert_called_once_with(stored_record.id, endpoint)

    denied_env = _make_env(record=stored_record, raw_key=raw_key)
    with pytest.raises(HTTPException) as excinfo:
        await _validate(denied_env, path="/api/v1/other")
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


# --- legacy str(list) storage (pre-I5 create path) -------------------------


def test_legacy_python_repr_scope_parses_to_same_allowlist() -> None:
    """Keys created before I5 stored ``str(list)`` (``"['/api/v1/search']"``).
    They must keep their intended scope, not collapse to deny-all."""
    legacy = str(["/api/v1/search"])
    assert _parse_allowed_endpoints(legacy) == ["/api/v1/search"]


async def test_legacy_python_repr_scope_enforced() -> None:
    env = _make_env(str(["/api/v1/search"]))
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"

    with pytest.raises(HTTPException) as excinfo:
        await _validate(_make_env(str(["/api/v1/search"])), path="/api/v1/other")
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(
    "raw", ["{'endpoints': '/api/v1/search'}", "[1, 2]", "__import__('os')", "['']"]
)
def test_legacy_python_repr_malformed_still_denies_all(raw: str) -> None:
    assert _parse_allowed_endpoints(raw) == []


# --- creation-time validation (422 instead of an unusable key) ------------


@pytest.mark.parametrize(
    "entries",
    [[""], ["  "], ["/"], [123], [None], ["api/v1/search"], [" /api/v1/search"]],
)
def test_create_rejects_invalid_scope_entries(entries: list[object]) -> None:
    with pytest.raises(ValidationError):
        APIKeyCreate(name="k", allowed_endpoints=entries)


@pytest.mark.parametrize("entries", [None, [], ["/api/v1/search", "/api/v1/x/"]])
def test_create_accepts_valid_scope(entries: Optional[list[str]]) -> None:
    assert APIKeyCreate(name="k", allowed_endpoints=entries).allowed_endpoints == (
        entries
    )
