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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.api_key_auth import (
    APIKey,
    _endpoint_allowed,
    _parse_allowed_endpoints,
    generate_api_key,
    get_api_key_data,
)

pytestmark = pytest.mark.unit


def _make_key(allowed_endpoints=None) -> tuple[APIKey, str]:
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


def _make_env(allowed_endpoints=None):
    """Return (record, raw_key, request, credentials, db, result) wired so
    the DB lookup resolves the key record."""
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


async def _validate(env, path=None):
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


def test_endpoint_allowed_none_allows_everything():
    assert _endpoint_allowed(None, "/any/path") is True


def test_endpoint_allowed_empty_list_denies_everything():
    assert _endpoint_allowed([], "/any/path") is False


def test_endpoint_allowed_exact_match():
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/search") is True


def test_endpoint_allowed_prefix_entry_permits_subpaths():
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/search/authenticated/hybrid") is True


def test_endpoint_allowed_prefix_entry_does_not_match_similar_prefix():
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/searchx") is False
    assert _endpoint_allowed(["/api/v1/search"], "/api/v1/other") is False


def test_endpoint_allowed_trailing_slash_entry_normalized():
    assert _endpoint_allowed(["/api/v1/search/"], "/api/v1/search/hybrid") is True


def test_endpoint_allowed_trailing_slash_entry_allows_exact_path():
    assert _endpoint_allowed(["/api/v1/search/"], "/api/v1/search") is True


# --- _parse_allowed_endpoints blank-entry handling ----------------------


@pytest.mark.parametrize("raw", [json.dumps([""]), json.dumps(["  "])])
async def test_blank_entries_deny_all_and_log_error(raw, caplog):
    assert _parse_allowed_endpoints(raw) == []
    assert any(
        rec.levelname == "ERROR" and "allowed_endpoints" in rec.message
        for rec in caplog.records
    )


async def test_blank_entry_config_denies_request():
    env = _make_env(json.dumps(["  "]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


# --- enforcement inside get_api_key_data --------------------------------


async def test_scoped_key_exact_match_allowed():
    env = _make_env(json.dumps(["/api/v1/search/authenticated/hybrid"]))
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"


async def test_scoped_key_prefix_allows_subpath_and_denies_other():
    env = _make_env(json.dumps(["/api/v1/search"]))
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"

    other = _make_env(json.dumps(["/api/v1/search"]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(other, path="/api/v1/other")
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "API key not permitted for this endpoint"


async def test_unscoped_key_none_allows_all():
    env = _make_env(None)
    _, endpoint, _ = await _validate(env)
    assert endpoint == "/api/v1/search/authenticated/hybrid"


async def test_empty_list_denies_all():
    env = _make_env(json.dumps([]))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


async def test_malformed_json_denies_all_and_logs_error(caplog):
    env = _make_env("not-json{{")
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert any(
        rec.levelname == "ERROR" and "allowed_endpoints" in rec.message
        for rec in caplog.records
    )


async def test_non_list_json_denies_all():
    env = _make_env(json.dumps({"endpoints": "/api/v1/search"}))
    with pytest.raises(HTTPException) as excinfo:
        await _validate(env)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


async def test_denied_request_does_not_consume_rate_limit_quota(caplog):
    """Enforcement sits before the rate-limit check: denied requests must
    not touch the limiter (no quota burn, no usage tracking, no DB write)."""
    env = _make_env(json.dumps(["/api/v1/search"]))
    record, raw_key, _, _, _ = env
    with caplog.at_level("WARNING"):
        with pytest.raises(HTTPException):
            await _validate(env, path="/api/v1/other")

    # The denial warning must reference the redacted prefix, never the raw key.
    denial_logs = [
        rec.message
        for rec in caplog.records
        if "not permitted" in rec.message
    ]
    assert denial_logs, "expected a denial warning log"
    assert all(raw_key not in msg for msg in denial_logs)
    assert any(record.key_prefix in msg for msg in denial_logs)
