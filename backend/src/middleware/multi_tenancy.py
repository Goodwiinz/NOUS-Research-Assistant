"""
Per-request tenant context for RAG Analytics.

What this module actually does:
- ``MultiTenancyMiddleware`` resolves the authenticated user + organization from
  the Bearer JWT (DB is the source of truth), validates the organization is
  active, and enters ``tenant_context_manager`` so the org/user/role are
  available for the duration of the request via ContextVars.
- The ``get_current_*`` accessors + ``check_tenant_permission`` /
  ``validate_tenant_access`` read that per-request context (used by the RBAC,
  audit, compliance and tenant-management layers).

What this module does NOT do: it does not enforce tenant isolation at the
database layer. There is no PostgreSQL Row Level Security and no automatic
query rewriting. Tenant isolation is a *per-query convention* — every
tenant-scoped query explicitly filters ``organization_id`` (~1,600+ sites
across the codebase). Earlier revisions carried an RLS/query-helper toolkit
(``add_row_level_security_filters``, ``setup_row_level_security``,
``TenantAwareQuery``, a no-op ``before_cursor_execute`` listener, …) that had
zero callers and falsely implied centralized enforcement; it was removed so the
module honestly reflects the real mechanism.
"""

import logging
import re
from contextvars import ContextVar
from typing import Any, Callable, Optional

import sentry_sdk
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.database import AsyncSessionLocal
from src.core.probes import PROBE_EXEMPT_PATHS
from src.core.security import verify_token
from src.core.user_provisioning import ensure_user_and_org
from src.exceptions.analytics_exceptions import PermissionDeniedException
from src.middleware.responses import error_response
from src.models.organization import Organization
from src.models.user import User

logger = logging.getLogger(__name__)

# Audit I7: the skip list used bare ``startswith``, so look-alike paths such as
# ``/docs<anything>`` or ``/api/v1/auth/refreshXYZ`` bypassed tenant validation.
# All patterns below are anchored. Two separate lists, two separate policies:
#
# ``_SKIP_PATH_REGEXES`` — auth-establishment routes. These run BEFORE a tenant
# context can exist (login/register/refresh, CLI device flow). Skipping tenant
# resolution for them is required for auth to work at all.
#
# ``PUBLIC_PATH_REGEXES`` — deliberately anonymous routes (verified by scanning
# ``src/api/`` for endpoints with no auth dependency and by reading each
# handler). Everything NOT listed here is fail-closed: no Authorization header
# => 401, unresolvable token => 401. Do not add entries without verifying the
# endpoint has no auth dependency and exposes no tenant data.
_SKIP_PATH_REGEXES = (
    # POST /api/v1/auth/login — establishes identity; no tenant context yet.
    re.compile(r"^/api/v1/auth/login$"),
    # POST /api/v1/auth/register — same as login.
    re.compile(r"^/api/v1/auth/register$"),
    # POST /api/v1/auth/refresh — exchanges a refresh token for a new JWT.
    re.compile(r"^/api/v1/auth/refresh$"),
    # /api/v1/cli-auth/* — CLI device-flow router (api/auth/cli_auth.py) is
    # pre-auth by design; every route under it starts an unauthenticated flow.
    re.compile(r"^/api/v1/cli-auth/"),
)

# Deliberately anonymous routes. Each entry: one route, one reason it is
# public. Anchored on purpose — a prefix match here would re-open the I7
# look-alike bypass for every sibling route.
PUBLIC_PATH_REGEXES = (
    # GET / — static welcome/version metadata served by main.py (no tenant data).
    re.compile(r"^/$"),
    # GET /docs — Swagger UI page served by FastAPI (static HTML).
    re.compile(r"^/docs$"),
    # GET /docs/oauth2-redirect — Swagger UI OAuth2 helper page (static HTML).
    re.compile(r"^/docs/oauth2-redirect$"),
    # GET /redoc — ReDoc page served by FastAPI (static HTML).
    re.compile(r"^/redoc$"),
    # GET /openapi.json — generated API schema (service metadata).
    re.compile(r"^/openapi\.json$"),
    # POST /api/v1/arxiv/search — anonymous arXiv literature discovery
    # (api/arxiv/core.py:145, no auth dependency by design; external data only).
    re.compile(r"^/api/v1/arxiv/search$"),
    # GET /api/v1/arxiv/tracking/stats — aggregate change-tracking counts;
    # docstring declares it public and deliberately omits per-tenant detail.
    re.compile(r"^/api/v1/arxiv/tracking/stats$"),
    # GET /api/v1/agent/health — agent service health (static ok payload).
    re.compile(r"^/api/v1/agent/health$"),
    # GET /api/v1/evaluation/health — evaluation service health (static).
    re.compile(r"^/api/v1/evaluation/health$"),
    # GET /api/v1/search-quality/health — search-quality service health.
    re.compile(r"^/api/v1/search-quality/health$"),
    # GET /api/v1/search-quality/metrics/types — static catalog of metric
    # enum types/descriptions (no user or tenant data).
    re.compile(r"^/api/v1/search-quality/metrics/types$"),
    # GET /api/v1/analytics/quality/health — quality-metrics service health.
    re.compile(r"^/api/v1/analytics/quality/health$"),
    # GET /api/v1/analytics/behavior/health — behavior-analytics service health.
    re.compile(r"^/api/v1/analytics/behavior/health$"),
    # GET /api/v1/analytics/performance/health — performance dashboard health.
    re.compile(r"^/api/v1/analytics/performance/health$"),
    # GET /api/v1/analytics/recommendations/health — recommendations health.
    re.compile(r"^/api/v1/analytics/recommendations/health$"),
)


def _role_to_str(role: Any) -> str:
    """Normalize a DB User.role (UserRole enum) to the string the tenancy
    permission checks compare against; default to 'user'."""
    if role is None:
        return "user"
    return getattr(role, "value", None) or str(role) or "user"


# Context variables for tenant information
tenant_context: ContextVar[Optional[str]] = ContextVar("tenant_id", default=None)
user_context: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
role_context: ContextVar[Optional[str]] = ContextVar("user_role", default=None)


class MultiTenancyMiddleware(BaseHTTPMiddleware):
    """Middleware to extract and validate tenant information from requests"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and set tenant context"""

        if self._should_skip_tenant_validation(request):
            return await call_next(request)

        # Audit I7 (fail-closed): a request with no Authorization header can
        # never carry tenant context. The old code fell through to
        # ``call_next`` here, so tenant isolation depended on every endpoint
        # separately raising 401. Return 401 immediately — BEFORE the
        # per-request DB session below is opened.
        if not request.headers.get("Authorization", ""):
            return error_response(
                401, "Not authenticated", "authentication_error"
            )

        try:
            async with AsyncSessionLocal() as db:
                tenant_info = await self._extract_tenant_info(request, db)

                if not tenant_info:
                    # Audit I7 (fail-closed): a token that is absent-credentials
                    # is handled above; here the token is present but fails
                    # verification or tenant resolution. The downstream auth
                    # dependencies (HTTPBearer(auto_error=True) +
                    # get_current_user_token in core/security.py, get_current_user
                    # in core/dependencies.py) would 401 on any route that
                    # declares them — but the middleware cannot guarantee every
                    # tenant-relevant route declares them (routes that only read
                    # the ContextVar tenant context would run with a None
                    # context). Tenant isolation must not be a per-endpoint
                    # convention, so the middleware 401s here itself. Details of
                    # the resolution failure stay server-side (logged below).
                    return error_response(
                        401,
                        "Could not validate credentials",
                        "authentication_error",
                    )
                await self._validate_tenant_access(tenant_info["organization_id"], db)

                request.state.db = db

                with tenant_context_manager(
                    organization_id=tenant_info["organization_id"],
                    user_id=tenant_info["user_id"],
                    user_role=tenant_info["role"],
                ):
                    request.state.tenant_id = tenant_info["organization_id"]
                    request.state.user_id = tenant_info["user_id"]
                    request.state.user_role = tenant_info["role"]
                    sentry_sdk.set_user({"id": str(tenant_info["user_id"])})
                    sentry_sdk.set_tag("tenant_id", str(tenant_info["organization_id"]))
                    return await call_next(request)

        except PermissionDeniedException as e:
            return error_response(403, str(e))
        except Exception as e:
            logger.error(f"Multi-tenancy middleware error: {e}")
            return error_response(
                500, "Internal server error during tenant validation", "internal_error"
            )

    def _should_skip_tenant_validation(self, request: Request) -> bool:
        """Check if tenant validation should be skipped for this endpoint.

        Paths must match the *mounted* route, not the bare router prefix.
        The auth router is ``APIRouter(prefix="/auth")`` (api/auth/auth.py)
        included with ``prefix="/api/v1"`` (main.py), so its real path is
        ``/api/v1/auth/...``. Matching ``/auth/login`` here never fired and
        forced every login/register/refresh request through tenant
        resolution (issue #1003).

        Audit I7: matching is done with anchored regexes (see
        ``_SKIP_PATH_REGEXES`` and ``PUBLIC_PATH_REGEXES``) instead of
        ``startswith`` so look-alike paths (``/docsX``,
        ``/api/v1/auth/refreshXYZ``, ...) can never bypass tenant validation.
        Probe paths stay an exact-match set lookup.
        """
        if request.url.path in PROBE_EXEMPT_PATHS:
            return True

        path = request.url.path
        if any(pattern.match(path) for pattern in _SKIP_PATH_REGEXES):
            return True
        return any(pattern.match(path) for pattern in PUBLIC_PATH_REGEXES)

    async def _extract_tenant_info(
        self, request: Request, db: Optional[AsyncSession] = None
    ) -> Optional[dict]:
        """Extract tenant information by verifying the Bearer JWT and resolving org via DB."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        try:
            token_data = verify_token(token)
        except Exception:
            return None
        if not token_data or not token_data.user_id:
            return None

        # The authenticated DB user record is the single source of truth for
        # role and account status — NOT the JWT claim. A long-lived CLI token
        # (up to 30 days) must not let a since-deactivated or since-demoted user
        # keep tenant access / an elevated role. Both paths below resolve and
        # validate the live User row.
        active_user = (
            User.id == token_data.user_id,
            User.is_active == True,  # noqa: E712 - SQLAlchemy needs == True
            User.is_deleted == False,  # noqa: E712
        )

        # Fast path: org_id already embedded in JWT (CLI tokens). JIT-provision,
        # then validate against the DB in the same session.
        if token_data.organization_id:
            try:
                async with AsyncSessionLocal() as prov_db:
                    provisioned = await ensure_user_and_org(prov_db, token_data)
                    if provisioned:
                        await prov_db.commit()
                    user = (
                        (await prov_db.execute(select(User).where(*active_user)))
                        .scalars()
                        .first()
                    )
            except Exception as e:
                logger.debug("Fast-path user resolve failed (non-fatal): %s", e)
                user = None
            if not user:
                return None  # inactive / deleted / unresolved -> no tenant context
            return {
                "organization_id": str(
                    user.organization_id or token_data.organization_id
                ),
                "user_id": str(token_data.user_id),
                "role": _role_to_str(user.role),
            }

        # Fallback: resolve org from DB (current Supabase JWTs don't embed org_id).
        # Reuses the caller's session — no extra connection needed.
        try:
            result = await db.execute(select(User).where(*active_user))
            user = result.scalars().first()
            if not user:
                # User doesn't exist yet — JIT-provision, then re-query.
                try:
                    async with AsyncSessionLocal() as prov_db:
                        await ensure_user_and_org(prov_db, token_data)
                        await prov_db.commit()
                except Exception as e:
                    logger.debug("JIT-provision skipped (non-fatal): %s", e)
                result = await db.execute(select(User).where(*active_user))
                user = result.scalars().first()
            if not user or not user.organization_id:
                return None
            return {
                "organization_id": str(user.organization_id),
                "user_id": str(token_data.user_id),
                "role": _role_to_str(user.role),
            }
        except Exception as e:
            logger.error(f"Error resolving tenant from token: {e}")
            return None

    async def _validate_tenant_access(
        self,
        organization_id: str,
        db: Optional[AsyncSession] = None,
    ) -> bool:
        """Validate that the organization exists and is active"""
        try:
            if db is not None:
                result = await db.execute(
                    select(Organization).where(
                        Organization.id == organization_id,
                        Organization.is_active == True,
                    )
                )
                organization = result.scalars().first()
            else:
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(Organization).where(
                            Organization.id == organization_id,
                            Organization.is_active == True,
                        )
                    )
                    organization = result.scalars().first()

            if not organization:
                raise PermissionDeniedException(
                    required_permission="organization_access",
                    user_role="unknown",
                    details={"organization_id": organization_id},
                )

            return True

        except PermissionDeniedException:
            raise
        except Exception as e:
            # Audit I7: the internal exception text used to be embedded in the
            # client-visible ``details`` (rendered by the analytics exception
            # handler). Log it server-side instead; the client only sees the
            # organization_id it supplied itself.
            logger.error(
                "Error validating tenant access for org %s: %s",
                organization_id,
                e,
                exc_info=True,
            )
            raise PermissionDeniedException(
                required_permission="organization_access",
                user_role="unknown",
                details={"organization_id": organization_id},
            )


# Per-request tenant context accessors


def get_current_tenant_id() -> Optional[str]:
    """Get current tenant ID from context"""
    return tenant_context.get()


def get_current_user_id() -> Optional[str]:
    """Get current user ID from context"""
    return user_context.get()


def get_current_user_role() -> Optional[str]:
    """Get current user role from context"""
    return role_context.get()


# Context manager for setting organization context

from contextlib import contextmanager


@contextmanager
def tenant_context_manager(organization_id: str, user_id: str, user_role: str = "user"):
    """Context manager for setting tenant information"""
    try:
        # Set context
        token_tenant = tenant_context.set(organization_id)
        token_user = user_context.set(user_id)
        token_role = role_context.set(user_role)

        yield

    finally:
        # Reset context
        tenant_context.reset(token_tenant)
        user_context.reset(token_user)
        role_context.reset(token_role)


# Utility functions for tenant validation

# Role-based permissions mapping
ROLE_PERMISSIONS = {
    "super_admin": [
        "organization_create",
        "organization_read",
        "organization_update",
        "organization_delete",
        "organization_users_read",
        "organization_users_manage",
        "organization_analytics",
        "tenant_access",
        "cross_tenant_access",
        "update_access",
        "delete_access",
        "system_admin",
        "audit_logs",
        "security_management",
    ],
    "admin": [
        "organization_read",
        "organization_update",
        "organization_users_read",
        "organization_users_manage",
        "organization_analytics",
        "tenant_access",
        "update_access",
        "delete_access",
    ],
    "content_manager": [
        "organization_read",
        "organization_users_read",
        "tenant_access",
        "update_access",
    ],
    "analyst": ["organization_read", "organization_analytics", "tenant_access"],
    "user": ["organization_read", "tenant_access"],
}


def check_tenant_permission(required_permission: str) -> bool:
    """Check if the current user has the required permission based on their role"""
    current_role = get_current_user_role()

    if not current_role:
        return False

    user_permissions = ROLE_PERMISSIONS.get(current_role, [])
    return required_permission in user_permissions


def validate_tenant_access(organization_id: str) -> bool:
    """Validate that current user has access to the specified organization"""
    current_tenant = get_current_tenant_id()

    if not current_tenant:
        return False

    return current_tenant == organization_id


# Export main components
__all__ = [
    "MultiTenancyMiddleware",
    "get_current_tenant_id",
    "get_current_user_id",
    "get_current_user_role",
    "tenant_context_manager",
    "check_tenant_permission",
    "validate_tenant_access",
    "ROLE_PERMISSIONS",
]
