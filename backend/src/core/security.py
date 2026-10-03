"""
Security utilities for authentication and authorization.

The backend accepts two JWT shapes:

1. Supabase-issued JWTs (HS256 via shared secret or ES256 via JWKS) — used
   by the frontend via Supabase SSR with cookie-based sessions.
2. Long-lived CLI tokens (HS256 signed with ``JWT_SECRET_KEY``) — issued at
   ``/cli-auth/approve`` time so the device-flow CLI doesn't have to
   re-authenticate every Supabase access-token refresh (~1h). CLI tokens
   carry ``scope=cli`` so they can be revoked / rate-limited separately.
"""

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt  # Changed from passlib
import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwk, jwt
from pydantic import BaseModel
from starlette.datastructures import Address
from starlette.requests import HTTPConnection

from src.core.config import settings

logger = logging.getLogger(__name__)

# Cache for Supabase JWKS keys
_supabase_jwks_cache: Optional[Dict] = None
_supabase_jwks_cache_fetched_at: Optional[float] = None
_SUPABASE_JWKS_TTL_SECONDS = 3600

# JWT Bearer scheme
security = HTTPBearer()


class TokenData(BaseModel):
    """Token data model"""

    user_id: Optional[str] = None
    email: Optional[str] = None
    organization_id: Optional[str] = None
    role: Optional[str] = None
    exp: Optional[datetime] = None
    # CLI-token revocation support: issued-at + a marker so the auth chokepoint
    # can reject CLI tokens minted before a per-user "revoked before" cutoff.
    issued_at: Optional[datetime] = None
    is_cli: bool = False
    # Sign-up profile from Supabase ``user_metadata`` (see
    # ``_extract_signup_metadata``). DISPLAY TEXT ONLY — client-controlled, so
    # it must never influence role, permissions, org membership or tenant scope.
    signup_first_name: Optional[str] = None
    signup_last_name: Optional[str] = None
    signup_organization_name: Optional[str] = None


def _extract_forwarded_ip(headers: Any) -> Optional[str]:
    """Return the trusted client IP from X-Forwarded-For when present."""
    if headers is None:
        return None

    forwarded_for = headers.get("X-Forwarded-For")
    if not forwarded_for:
        return None

    ips = [ip.strip() for ip in forwarded_for.split(",") if ip.strip()]
    return ips[-1] if ips else None


def _install_proxy_aware_client_patch() -> None:
    """Make request.client.host reflect trusted X-Forwarded-For values."""
    if getattr(HTTPConnection, "_proxy_aware_client_installed", False):
        return

    original_client = HTTPConnection.client
    if not isinstance(original_client, property) or original_client.fget is None:
        return

    def _proxy_aware_client(self):
        forwarded_ip = _extract_forwarded_ip(getattr(self, "headers", None))
        if forwarded_ip:
            base_client = original_client.fget(self)
            port = getattr(base_client, "port", 0) if base_client else 0
            return Address(forwarded_ip, port)
        return original_client.fget(self)

    HTTPConnection.client = property(_proxy_aware_client)
    HTTPConnection._proxy_aware_client_installed = True


# R4-M13: this patch makes request.client.host (and get_client_ip below)
# trust X-Forwarded-For process-wide, which is only safe behind a proxy that
# itself sets/overwrites that header (our ingress does). Gate it behind
# TRUSTED_PROXY_ENABLED (default True — deployed dev sits behind ingress)
# so a deployment not behind a trusted proxy doesn't let a client spoof its
# own IP for rate-limiting / audit-log / abuse-detection purposes.
if settings.TRUSTED_PROXY_ENABLED:
    _install_proxy_aware_client_patch()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    try:
        # Truncate to 72 chars to avoid bcrypt limit and ensure compatibility
        plain_bytes = plain_password[:72].encode("utf-8")
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(plain_bytes, hash_bytes)
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    """Generate password hash"""
    # Truncate to 72 chars to avoid bcrypt limit
    pwd_bytes = password[:72].encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def generate_password_reset_token() -> str:
    """Generate a secure password reset token"""
    return secrets.token_urlsafe(32)


def generate_api_key() -> str:
    """Generate a secure API key"""
    return secrets.token_urlsafe(32)


def get_client_ip(request: Request) -> str:
    """
    Get client IP address, handling proxies.

    Checks X-Forwarded-For header first. If present, takes the last IP
    in the list (assuming trusted proxy appends client IP).
    Falls back to request.client.host if not behind a proxy.
    """
    forwarded_ip = _extract_forwarded_ip(request.headers)
    if forwarded_ip:
        return forwarded_ip

    return request.client.host if request.client else "unknown"


def _get_supabase_jwks() -> Optional[Dict]:
    """Fetch and cache JWKS from Supabase for ES256 verification.

    Cached for _SUPABASE_JWKS_TTL_SECONDS so a Supabase key rotation is
    picked up without a restart. If the refetch after expiry fails, the
    stale cache is returned (with a warning) rather than None'd out —
    working keys must never be dropped just because one refresh attempt
    failed.
    """
    global _supabase_jwks_cache, _supabase_jwks_cache_fetched_at
    now = time.time()
    if (
        _supabase_jwks_cache is not None
        and _supabase_jwks_cache_fetched_at is not None
        and now - _supabase_jwks_cache_fetched_at < _SUPABASE_JWKS_TTL_SECONDS
    ):
        return _supabase_jwks_cache
    try:
        url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        _supabase_jwks_cache = resp.json()
        _supabase_jwks_cache_fetched_at = now
        logger.info("Fetched Supabase JWKS successfully")
        return _supabase_jwks_cache
    except Exception as e:
        logger.warning(f"Failed to fetch Supabase JWKS: {e}")
        if _supabase_jwks_cache is not None:
            logger.warning("Using stale cached Supabase JWKS after refetch failure")
            return _supabase_jwks_cache
        return None


# Column limits the sign-up metadata has to fit (see src/models/user.py and
# src/models/organization.py): users.first_name / last_name are String(100),
# organizations.name is String(255).
_SIGNUP_NAME_MAX_LENGTH = 100
_SIGNUP_ORG_NAME_MAX_LENGTH = 255


def _clean_metadata_string(value: Any, max_length: int) -> Optional[str]:
    """Coerce one attacker-controlled metadata value into safe display text.

    Anything that isn't a non-blank string (``None``, numbers, dicts, lists)
    becomes ``None`` so the caller falls back to a derived value. A pydantic
    ``TokenData`` field is ``Optional[str]`` and pydantic v2 does NOT coerce,
    so letting a non-string through here would raise during token extraction
    and 401 the user.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:max_length] or None


def _extract_signup_metadata(payload: dict) -> Dict[str, Optional[str]]:
    """Whitelist the sign-up fields the frontend writes to ``user_metadata``.

    Supabase copies the client-supplied ``options.data`` of ``auth.signUp()``
    verbatim into the ``user_metadata`` JWT claim, so ANY key in there is
    attacker-controlled — a user can set ``role``/``organization_id`` at will.
    We therefore take only the three display fields, truncated to their column
    limits, and drop the rest at the boundary: nothing else can ride in.
    Authorization keeps coming exclusively from ``app_metadata``.
    """
    user_metadata = payload.get("user_metadata")
    if not isinstance(user_metadata, dict):
        return {}
    return {
        "signup_first_name": _clean_metadata_string(
            user_metadata.get("first_name"), _SIGNUP_NAME_MAX_LENGTH
        ),
        "signup_last_name": _clean_metadata_string(
            user_metadata.get("last_name"), _SIGNUP_NAME_MAX_LENGTH
        ),
        "signup_organization_name": _clean_metadata_string(
            user_metadata.get("organization_name"), _SIGNUP_ORG_NAME_MAX_LENGTH
        ),
    }


def _extract_supabase_token_data(payload: dict) -> Optional[TokenData]:
    """Extract TokenData from a decoded Supabase JWT payload."""
    user_id = payload.get("sub")
    email = payload.get("email")
    app_metadata = payload.get("app_metadata", {})
    role = app_metadata.get("role", "USER")
    exp = payload.get("exp")
    if user_id:
        return TokenData(
            user_id=user_id,
            email=email,
            # Honor an org claim if Supabase set one; otherwise JIT
            # provisioning (ensure_user_and_org) resolves/creates one.
            organization_id=app_metadata.get("organization_id"),
            role=role,
            exp=datetime.utcfromtimestamp(exp) if exp else None,
            **_extract_signup_metadata(payload),
        )
    return None


_CLI_TOKEN_SCOPE = "cli"
_CLI_TOKEN_ISSUER = "nous-backend"

# jose only checks `exp` when the claim is present; without this a validly
# signed token that omits `exp` never expires (audit I21). Every live bearer
# path (HTTP deps, WebSocketAuthenticator, multi-tenancy middleware) decodes
# via verify_token, so requiring it here covers them all.
_REQUIRE_EXP = {"require_exp": True}


def create_cli_token(
    user_id: str,
    email: str,
    organization_id: str,
    role: str = "USER",
) -> tuple[str, datetime]:
    """Mint a long-lived CLI access token.

    Returns ``(token, expires_at)`` where ``expires_at`` is the absolute UTC
    expiry. ``CLI_TOKEN_EXPIRE_DAYS`` (default 30) governs the lifetime —
    long enough that the CLI feels permanent without being non-expiring.

    Payload mirrors the Supabase JWT shape so ``_extract_supabase_token_data``
    can decode it unchanged. The ``scope=cli`` claim is the only marker
    distinguishing CLI tokens from a (real) Supabase token; combined with the
    HS256 signature using ``JWT_SECRET_KEY`` (not ``SUPABASE_JWT_SECRET``)
    this prevents cross-confusion.

    Raises ``RuntimeError`` if ``JWT_SECRET_KEY`` is unset — refusing to mint
    rather than silently issuing tokens with a default-empty signing key.
    """
    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY must be configured to mint CLI tokens")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.CLI_TOKEN_EXPIRE_DAYS)

    payload = {
        "sub": user_id,
        "email": email,
        "app_metadata": {
            "organization_id": organization_id,
            "role": role,
        },
        "scope": _CLI_TOKEN_SCOPE,
        "iss": _CLI_TOKEN_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    return token, expires_at


def _extract_cli_token_data(payload: dict) -> Optional[TokenData]:
    """Extract TokenData from a decoded CLI JWT payload."""
    user_id = payload.get("sub")
    if not user_id:
        return None
    app_metadata = payload.get("app_metadata", {}) or {}
    exp = payload.get("exp")
    iat = payload.get("iat")
    return TokenData(
        user_id=user_id,
        email=payload.get("email"),
        organization_id=app_metadata.get("organization_id"),
        role=app_metadata.get("role", "USER"),
        exp=datetime.utcfromtimestamp(exp) if exp else None,
        issued_at=datetime.utcfromtimestamp(iat) if iat else None,
        is_cli=True,
    )


def verify_token(token: str) -> Optional[TokenData]:
    """Verify a Supabase JWT (HS256 / ES256) or a long-lived CLI token (HS256).

    Tries each path in turn; the first match wins. Returns ``None`` when no
    path validates (caller should map to 401).
    """
    # Try CLI token first — cheap (no JWKS fetch) and lets us short-circuit
    # before falling through to the more expensive Supabase paths.
    if settings.JWT_SECRET_KEY:
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
                issuer=_CLI_TOKEN_ISSUER,
                options={"verify_aud": False, **_REQUIRE_EXP},
            )
            if payload.get("scope") == _CLI_TOKEN_SCOPE:
                result = _extract_cli_token_data(payload)
                if result:
                    return result
        except JWTError:
            pass  # Fall through to Supabase paths

    # Defense-in-depth (audit AU6): optionally pin the expected Supabase issuer.
    # The signature already binds the token to this project's secret/JWKS, so
    # this can't reject a *forged* token — it only guards against a same-secret
    # token minted for a different Supabase project being replayed here.
    # Opt-in via an EXACT SUPABASE_JWT_ISSUER (empty = skip). It is deliberately
    # NOT derived from SUPABASE_URL: the hosted stack issues `<url>/auth/v1`
    # while a bare GoTrue (CI/local) issues a different value, so a derived
    # guess would reject every login in those environments.
    supabase_issuer = settings.SUPABASE_JWT_ISSUER or None

    # Try Supabase JWT — HS256 with shared secret
    if settings.SUPABASE_JWT_SECRET:
        try:
            decode_kwargs: Dict[str, Any] = {
                "algorithms": ["HS256"],
                "audience": "authenticated",
                "options": _REQUIRE_EXP,
            }
            if supabase_issuer:
                decode_kwargs["issuer"] = supabase_issuer
            payload = jwt.decode(token, settings.SUPABASE_JWT_SECRET, **decode_kwargs)
            result = _extract_supabase_token_data(payload)
            if result:
                return result
        except JWTError:
            pass  # Fall through to JWKS/ES256

    # Try Supabase JWT — ES256 via JWKS
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") == "ES256":
            jwks_data = _get_supabase_jwks()
            if jwks_data and "keys" in jwks_data:
                kid = header.get("kid")
                for key_data in jwks_data["keys"]:
                    if key_data.get("kid") == kid or kid is None:
                        public_key = jwk.construct(key_data)
                        decode_kwargs = {
                            "algorithms": ["ES256"],
                            "audience": "authenticated",
                            "options": _REQUIRE_EXP,
                        }
                        if supabase_issuer:
                            decode_kwargs["issuer"] = supabase_issuer
                        payload = jwt.decode(token, public_key, **decode_kwargs)
                        result = _extract_supabase_token_data(payload)
                        if result:
                            return result
    except JWTError as e:
        logger.debug(f"JWT validation failed (expected for expired tokens): {e}")
    except Exception as e:
        logger.error(f"ES256 verification failed: {type(e).__name__}: {e}")

    return None


def get_current_user_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenData:
    """Get current user from JWT token"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token = credentials.credentials
        token_data = verify_token(token)

        if token_data is None:
            raise credentials_exception

        # Check if token is expired
        if token_data.exp and token_data.exp < datetime.utcnow():
            raise credentials_exception

        # Hard max-age cap on CLI tokens, independent of the token's own `exp`
        # claim. A CLI token minted under a previously larger
        # CLI_TOKEN_EXPIRE_DAYS is still clamped to the *current* configured
        # lifetime. This bounds exposure if the Redis revocation store loses a
        # revoked-before cutoff (e.g. a flush, which reads as a miss and so
        # allows): the token ages out within the cap instead of staying valid
        # to its original exp. (audit D7)
        if token_data.is_cli and token_data.issued_at is not None:
            issued_at = token_data.issued_at
            if issued_at.tzinfo is not None:
                issued_at = issued_at.astimezone(timezone.utc).replace(tzinfo=None)
            max_age = timedelta(days=settings.CLI_TOKEN_EXPIRE_DAYS)
            if datetime.utcnow() - issued_at > max_age:
                raise credentials_exception

        return token_data

    except Exception:
        raise credentials_exception


def check_password_strength(password: str) -> Dict[str, Any]:
    """Check password strength and return recommendations"""
    issues = []
    score = 0

    # Length check
    if len(password) < 8:
        issues.append("Password should be at least 8 characters long")
    else:
        score += 1

    # Uppercase check
    if not any(c.isupper() for c in password):
        issues.append("Password should contain at least one uppercase letter")
    else:
        score += 1

    # Lowercase check
    if not any(c.islower() for c in password):
        issues.append("Password should contain at least one lowercase letter")
    else:
        score += 1

    # Number check
    if not any(c.isdigit() for c in password):
        issues.append("Password should contain at least one number")
    else:
        score += 1

    # Special character check
    special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if not any(c in special_chars for c in password):
        issues.append("Password should contain at least one special character")
    else:
        score += 1

    # Common patterns check
    common_patterns = ["password", "123456", "qwerty", "admin"]
    password_lower = password.lower()
    if any(pattern in password_lower for pattern in common_patterns):
        issues.append("Password should not contain common patterns")
        score = max(0, score - 2)

    # Determine strength
    if score >= 5:
        strength = "strong"
    elif score >= 3:
        strength = "medium"
    else:
        strength = "weak"

    return {
        "strength": strength,
        "score": score,
        "issues": issues,
        "is_valid": len(issues) == 0,
    }


def generate_secure_random_string(length: int = 32) -> str:
    """Generate a cryptographically secure random string"""
    return secrets.token_urlsafe(length)


def hash_sensitive_data(data: str) -> str:
    """Hash sensitive data for storage"""
    import hashlib

    return hashlib.sha256(data.encode()).hexdigest()


def verify_sensitive_data_hash(data: str, hashed: str) -> bool:
    """Verify sensitive data against its hash"""
    import hashlib
    import secrets

    return secrets.compare_digest(hashlib.sha256(data.encode()).hexdigest(), hashed)


# Import RateLimiter implementations
from src.core.rate_limit import InMemoryRateLimiter as RateLimiter
from src.core.rate_limit import create_rate_limiter

# Global rate limiter instance (will be initialized after settings import)
# Use higher limits for development to avoid blocking during testing
auth_rate_limiter = create_rate_limiter(
    max_attempts=settings.AUTH_RATE_LIMIT_ATTEMPTS,
    window_minutes=settings.AUTH_RATE_LIMIT_WINDOW_MINUTES,
)
