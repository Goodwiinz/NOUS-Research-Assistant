"""Tenant resolution shared by the search, knowledge-graph and search-quality
routers."""

from fastapi import HTTPException

from src.models.user import User


def require_org_id(current_user: User) -> str:
    """Resolve the caller's organization id, or 403.

    ``User.organization_id`` is nullable and ``str(None)`` is the literal
    ``"None"``, which the search and graph services would otherwise treat as
    a tenant (or, where they filter only ``if organization_id:``, as no scope
    at all). Call it before touching the database or a service, and outside
    a handler's generic ``except Exception`` block (or re-raise
    ``HTTPException``) so the 403 is not rewritten as a 500.
    """
    org_id = getattr(current_user, "organization_id", None)
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="No organization associated with this account",
        )
    return str(org_id)
