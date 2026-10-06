"""Bearer tokens without an ``exp`` claim must be rejected (audit I21).

``python-jose`` only checks ``exp`` when the claim is present, and
``TokenData.exp`` was ``None`` for an exp-less token, so the conditional
expiry check in ``get_current_user_token`` skipped it: a validly-signed token
with no ``exp`` never expired. ``verify_token`` is the single decode step that
every live bearer path routes through (``get_current_user_token``,
``WebSocketAuthenticator``, the multi-tenancy middleware), so the guard is
``options={"require_exp": True}`` on its ``jwt.decode`` calls.

Mutation check (docs/engineering/testing.md "Mutation verification"):
  guard: ``backend/src/core/security.py`` ``_REQUIRE_EXP`` (passed as
  ``options=`` to every ``jwt.decode`` in ``verify_token``).
  mutation: ``_REQUIRE_EXP = {"require_exp": False}``
  command: ``pytest -q backend/tests/unit/core/test_token_requires_exp.py``
  expected: every ``*_without_exp_*`` test fails (``verify_token`` returns
  TokenData / no HTTPException / no WebSocketAuthError); the with-exp and
  expired tests keep passing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwk, jwt

import src.core.security as security
from src.core.config import settings
from src.core.security import (
    _CLI_TOKEN_ISSUER,
    _CLI_TOKEN_SCOPE,
    get_current_user_token,
    verify_token,
)
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError

pytestmark = pytest.mark.unit

_SUPABASE_SECRET = "supabase-test-shared-secret-32chars!!"


@pytest.fixture(autouse=True)
def _supabase_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", _SUPABASE_SECRET)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")


def _exp(minutes: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp())


def _supabase_token(exp: Optional[int]) -> str:
    payload: Dict[str, Any] = {
        "sub": "sb-user",
        "email": "sb@example.com",
        "app_metadata": {"role": "USER"},
        "aud": "authenticated",
    }
    if exp is not None:
        payload["exp"] = exp
    return str(jwt.encode(payload, _SUPABASE_SECRET, algorithm="HS256"))


def _cli_token(exp: Optional[int]) -> str:
    payload: Dict[str, Any] = {
        "sub": "cli-user",
        "email": "cli@example.com",
        "app_metadata": {"organization_id": "o", "role": "USER"},
        "scope": _CLI_TOKEN_SCOPE,
        "iss": _CLI_TOKEN_ISSUER,
    }
    if exp is not None:
        payload["exp"] = exp
    return str(
        jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    )


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _DummyWebSocket:
    def __init__(self, token: str) -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.cookies: Dict[str, str] = {}


def test_supabase_token_without_exp_rejected_by_verify_token() -> None:
    assert verify_token(_supabase_token(exp=None)) is None


def test_supabase_token_without_exp_gets_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user_token(_bearer(_supabase_token(exp=None)))
    assert exc.value.status_code == 401


def test_cli_token_without_exp_rejected_by_verify_token() -> None:
    assert verify_token(_cli_token(exp=None)) is None


def test_cli_token_without_exp_gets_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user_token(_bearer(_cli_token(exp=None)))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("with_exp", [True, False])
def test_es256_jwks_token_requires_exp(
    monkeypatch: pytest.MonkeyPatch, with_exp: bool
) -> None:
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    pub_jwk = jwk.construct(pub_pem, algorithm="ES256").to_dict()
    pub_jwk["kid"] = "kid"
    monkeypatch.setattr(security, "_get_supabase_jwks", lambda: {"keys": [pub_jwk]})
    payload: Dict[str, Any] = {"sub": "es-user", "aud": "authenticated"}
    if with_exp:
        payload["exp"] = _exp(60)
    token = jwt.encode(payload, priv_pem, algorithm="ES256", headers={"kid": "kid"})

    data = verify_token(token)

    assert (data is not None) is with_exp


@pytest.mark.asyncio
async def test_websocket_token_without_exp_rejected() -> None:
    with pytest.raises(WebSocketAuthError) as exc:
        await WebSocketAuthenticator.authenticate(
            _DummyWebSocket(_supabase_token(exp=None))  # type: ignore[arg-type]
        )
    assert exc.value.code == 4003


def test_supabase_token_with_future_exp_still_accepted() -> None:
    data = get_current_user_token(_bearer(_supabase_token(exp=_exp(60))))
    assert data.user_id == "sb-user"


def test_cli_token_with_future_exp_still_accepted() -> None:
    data = get_current_user_token(_bearer(_cli_token(exp=_exp(60))))
    assert data.user_id == "cli-user"
    assert data.is_cli is True


@pytest.mark.parametrize("make", [_supabase_token, _cli_token])
def test_expired_token_still_rejected(make: Any) -> None:
    token = make(exp=_exp(-5))
    assert verify_token(token) is None
    with pytest.raises(HTTPException) as exc:
        get_current_user_token(_bearer(token))
    assert exc.value.status_code == 401
