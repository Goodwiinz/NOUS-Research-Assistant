"""
RBAC permission dependency for API endpoints.

History (audit I12): this module previously also carried an unmounted
``RBACMiddleware`` class and several ``@require_*`` decorators / helpers.
They had zero consumers, targeted non-existent ``/api/documents.*`` paths,
and called ``RBACService()`` with no session — which per RBACService's own
docstring silently returns no permissions (→ always 403). All dead symbols
were removed; the surviving piece is ``require_permission_dep``, the
dependency-style enforcer used by the compliance, RBAC-management and
encryption routers.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import get_db_sync
from src.middleware.multi_tenancy import get_current_tenant_id, get_current_user_id
from src.services.security.rbac_service import RBACService


def require_permission_dep(permission_name: str):
    """FastAPI **dependency** that enforces a permission — use with ``Depends()``.

    ``Depends(require_permission(...))`` with a decorator never enforces the
    check — FastAPI calls the outer ``decorator`` and never invokes the inner
    ``wrapper`` that holds the 401/403 logic, leaving the endpoint broken
    (422/500) and unguarded. This factory returns a real dependency callable so
    ``Depends(require_permission_dep("x"))`` works as intended.
    """

    def dependency(db: Session = Depends(get_db_sync)) -> None:
        user_id = get_current_user_id()
        organization_id = get_current_tenant_id()

        if not user_id or not organization_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        # Pass the request-scoped session explicitly. RBACService() with no db
        # leaves self.db=None and silently returns no permissions (→ always
        # 403); and do NOT use it as a context manager — __exit__ would close
        # the FastAPI-managed session early.
        if not RBACService(db).user_has_permission(
            user_id, permission_name, organization_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission_name} required",
            )

    return dependency


__all__ = ["require_permission_dep"]
