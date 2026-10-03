from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import redis as redis_lib
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel

from src.core.config import settings
from src.core.dependencies import get_current_user
from src.core.security import auth_rate_limiter, create_cli_token
from src.services.auth.cli_auth_sessions import (
    InMemoryCLIAuthSessionStore,
    RedisCLIAuthSessionStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cli-auth", tags=["cli-auth"])

_POLL_INTERVAL_SECONDS = 2
POLL_TOKEN_HEADER = "X-CLI-Poll-Token"


# Prefer Redis-backed store so sessions survive across Gunicorn workers.
# Falls back to in-memory if Redis is unavailable (e.g. local dev without Redis).
def _build_session_store() -> RedisCLIAuthSessionStore | InMemoryCLIAuthSessionStore:
    try:
        r: redis_lib.Redis = redis_lib.from_url(settings.REDIS_URL, socket_connect_timeout=2)  # type: ignore[type-arg]
        r.ping()
        logger.info("cli-auth: using Redis session store")
        return RedisCLIAuthSessionStore(r)
    except Exception as exc:
        logger.warning(
            "cli-auth: Redis unavailable (%s), falling back to in-memory store", exc
        )
        return InMemoryCLIAuthSessionStore()


_session_store = _build_session_store()


class CLIAuthApproveRequest(BaseModel):
    session_id: str
    verification_code: str


def get_cli_auth_session_store() -> (
    RedisCLIAuthSessionStore | InMemoryCLIAuthSessionStore
):
    return _session_store


def _frontend_base_url() -> str:
    # Prefer the dedicated frontend URL; fall back to the first CORS origin so
    # existing deployments keep working until FRONTEND_BASE_URL is set.
    base = settings.FRONTEND_BASE_URL or settings.cors_origins_list[0]
    return base.rstrip("/")


def _serialize_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post("/start")
async def start_cli_auth(
    request: Request,
    store: InMemoryCLIAuthSessionStore = Depends(get_cli_auth_session_store),
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else "unknown"
    if not await auth_rate_limiter.is_allowed(client_ip, prefix="cli_start"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many CLI auth requests. Try again later.",
        )
    session = store.create_session()
    # The code rides in the fragment, which browsers never send to a server,
    # proxy or Referer, so it stays out of request logs.
    browser_url = (
        f"{_frontend_base_url()}/cli-auth"
        f"?session_id={session.session_id}#code={session.verification_code}"
    )
    return {
        "session_id": session.session_id,
        "verification_code": session.verification_code,
        "browser_url": browser_url,
        "poll_token": session.poll_token,
        "expires_at": _serialize_datetime(session.expires_at),
        "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
    }


@router.get("/status/{session_id}")
async def get_cli_auth_status(
    session_id: str,
    response: Response,
    header_poll_token: str | None = Header(default=None, alias=POLL_TOKEN_HEADER),
    poll_token: str | None = Query(
        default=None,
        description=f"Deprecated: send the {POLL_TOKEN_HEADER} header instead.",
    ),
    store: InMemoryCLIAuthSessionStore = Depends(get_cli_auth_session_store),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    token = header_poll_token
    if token is None and poll_token is not None:
        # Compat window for CLIs installed before the header existed; the token
        # still reaches access logs here. Delete with the Query param (audit I22).
        logger.warning("cli-auth: poll token sent in query string; client must upgrade")
        token = poll_token
    session = store.get_session(session_id, token) if token else None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CLI auth session not found",
        )

    body: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status,
        "expires_at": _serialize_datetime(session.expires_at),
    }
    if session.credential_payload:
        body.update(session.credential_payload)
    return body


@router.post("/approve")
async def approve_cli_auth(
    request: CLIAuthApproveRequest,
    store: InMemoryCLIAuthSessionStore = Depends(get_cli_auth_session_store),
    current_user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    organization_id = str(current_user.organization_id or "")
    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authenticated user must belong to an organization",
        )

    # Mint a long-lived CLI token rather than passing the caller's short-lived
    # Supabase access token through. CLI users would otherwise have to
    # re-run device-flow login every time the Supabase token expires (~1h).
    role = getattr(current_user, "role", None)
    cli_token, expires_at = create_cli_token(
        user_id=str(current_user.id),
        email=str(current_user.email),
        organization_id=organization_id,
        role=(
            str(role.value)
            if hasattr(role, "value")
            else (str(role) if role else "USER")
        ),
    )

    credential_payload = {
        "token": cli_token,
        "organization_id": organization_id,
        "user_email": str(current_user.email),
        "expires_at": _serialize_datetime(expires_at),
    }
    session = store.approve_session(
        request.session_id,
        verification_code=request.verification_code,
        user_id=str(current_user.id),
        credential_payload=credential_payload,
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CLI auth session not found",
        )

    return {
        "session_id": session.session_id,
        "status": session.status,
        **credential_payload,
    }
