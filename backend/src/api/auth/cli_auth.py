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

from src.api.integrations.auth import require_interactive_user
from src.core.config import settings
from src.core.security import auth_rate_limiter, create_cli_token
from src.services.auth.cli_auth_sessions import (
    CLIAuthSessionStatus,
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


class CLIAuthSessionInfo(BaseModel):
    """What the approving browser may see about a pending CLI sign-in.

    Never carries the verification code or poll token. The user must read the
    code from their own terminal (RFC 8628 §5.4 remote-phishing defence).
    """

    session_id: str
    status: CLIAuthSessionStatus
    started_at: str
    expires_at: str
    requester_ip: str | None = None
    requester_user_agent: str | None = None


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
    # request.client.host honours X-Forwarded-For only when TRUSTED_PROXY_ENABLED
    # installed the proxy patch (core/security.py), so it is the IP the rate
    # limiter keys on and cannot be spoofed when that patch is off. Do not use
    # get_client_ip() here: it trusts X-Forwarded-For unconditionally.
    client_ip = request.client.host if request.client else "unknown"
    if not await auth_rate_limiter.is_allowed(client_ip, prefix="cli_start"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many CLI auth requests. Try again later.",
        )
    session = store.create_session(
        requester_ip=client_ip,
        requester_user_agent=request.headers.get("user-agent"),
    )
    # GOO-403 (RFC 8628 §5.4): the link carries the session only, never the
    # code. The user types the code their own terminal printed, so a link
    # someone else sends cannot be approved in one click. The code is returned
    # to the CLI caller below so it can print it.
    browser_url = f"{_frontend_base_url()}/cli-auth?session_id={session.session_id}"
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
            headers={"Cache-Control": "no-store"},
        )

    body: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status,
        "expires_at": _serialize_datetime(session.expires_at),
    }
    if session.credential_payload:
        body.update(session.credential_payload)
    return body


@router.get("/session/{session_id}", response_model=CLIAuthSessionInfo)
async def get_cli_auth_session_info(
    session_id: str,
    response: Response,
    store: InMemoryCLIAuthSessionStore = Depends(get_cli_auth_session_store),
    _user: Any = Depends(require_interactive_user),
) -> CLIAuthSessionInfo:
    """Requester details for the approval page. Never returns the code."""
    response.headers["Cache-Control"] = "no-store"
    session = store.get_session_for_approver(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CLI auth session not found",
            headers={"Cache-Control": "no-store"},
        )
    return CLIAuthSessionInfo(
        session_id=session.session_id,
        status=session.status,
        started_at=_serialize_datetime(session.created_at),
        expires_at=_serialize_datetime(session.expires_at),
        requester_ip=session.requester_ip,
        requester_user_agent=session.requester_user_agent,
    )


@router.post(
    "/approve",
    responses={
        400: {"description": "Verification code does not match"},
        403: {"description": "A browser session is required to approve"},
        404: {"description": "CLI auth session not found or no longer pending"},
    },
)
async def approve_cli_auth(
    request: CLIAuthApproveRequest,
    store: InMemoryCLIAuthSessionStore = Depends(get_cli_auth_session_store),
    # GOO-403: only an interactive browser login may approve. A CLI token (or an
    # integration grant) approving would let a stolen token renew itself forever.
    current_user: Any = Depends(require_interactive_user),
) -> dict[str, Any]:
    organization_id = str(current_user.organization_id or "")
    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authenticated user must belong to an organization",
        )

    pending = store.get_session_for_approver(request.session_id)
    if pending is None or pending.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CLI auth session not found",
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
        # After 5 wrong codes the store denies the session, even when this
        # attempt carried the right code. Report that as not pending (404) so
        # the user does not keep retyping into a dead session.
        current = store.get_session_for_approver(request.session_id)
        if current is None or current.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CLI auth session not found",
            )
        # Still pending, so the typed code is wrong (the store counted it).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification code does not match the one shown in your terminal",
        )

    logger.info(
        "cli-auth: session approved",
        extra={
            "cli_auth_session_id": session.session_id,
            "user_id": str(current_user.id),
            "requester_ip": session.requester_ip,
        },
    )
    return {
        "session_id": session.session_id,
        "status": session.status,
        **credential_payload,
    }
