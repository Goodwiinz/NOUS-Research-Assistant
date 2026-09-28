"""
Workspace and workspace-member persistence (Task 4.3 consolidation).

Canonical owner for workspace CRUD and member management — the router-inline
copy in ``backend/src/api/threads/workspace_routes/{workspaces,members}.py``
and ``ChatService``'s workspace methods both called into this module's
predecessor logic independently; this is now the one implementation.

Divergence flags (see docs/plans/2026-07-15-maintainability-foundation.md
Task 4.3 + its 2026-07-16 amendment A2): the router and ``ChatService``
diverged on two workspace concerns pre-4.3. Router semantics were always
canonical for the endpoints the router serves; ``ChatService``'s own callers
initially kept the old (unsafe) observed behavior via an explicit flag
defaulting to that old value. A follow-up tenant-gap fix (see
``chat_service.py``'s ``create_workspace``/``list_workspaces``) switched
``ChatService`` onto the router-safe value for both, after which the
defaults here were flipped fail-closed — passing the permissive value now
requires an explicit, justified opt-out:

- ``enforce_org_match`` (create): router raises on a cross-org create
  request; ``ChatService.create_workspace`` used to trust the caller's
  ``organization_id`` verbatim (a pre-existing tenant gap on the
  ``conversations.py`` endpoint it backs) and now enforces the same guard.
- ``filter_deleted_memberships`` (list): router excludes a workspace the
  caller was removed from (soft-deleted ``WorkspaceMember`` row);
  ``ChatService.list_workspaces`` used to not filter it, so a removed
  member still saw the workspace (another pre-existing tenant gap), and
  now filters it too.

Workspace member management (add/update/remove) has no ``ChatService``
duplicate to reconcile — it was router-only before this split — so those
three functions are a straight move, no flag.
"""

from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.chat import (
    WorkspaceCreate,
    WorkspaceMemberCreate,
    WorkspaceMemberUpdate,
    WorkspaceUpdate,
)
from src.services.threads import workspace_access

# =============================================================================
# Workspace CRUD
# =============================================================================


async def create_workspace(
    db: AsyncSession,
    data: WorkspaceCreate,
    owner_id: UUID,
    user_organization_id: Optional[UUID],
    *,
    enforce_org_match: bool = True,
) -> Workspace:
    """Create a workspace + its owner membership row.

    Raises ``PermissionError`` when ``enforce_org_match=True`` (the
    fail-closed default) and the request's ``organization_id`` doesn't match
    the caller's own org — the router turns this into a 403. Passing
    ``False`` trusts ``data.organization_id`` verbatim; no in-repo caller
    does, and any new one must justify it explicitly.
    """
    if enforce_org_match:
        if data.organization_id is not None and str(data.organization_id) != str(
            user_organization_id
        ):
            raise PermissionError(
                "Cannot create workspace for a different organization"
            )
        organization_id = user_organization_id
    else:
        organization_id = data.organization_id

    # R5-L11: unbounded workspace minting — cap per org (config-overridable).
    from src.core.config import settings as _settings

    org_scope = organization_id or user_organization_id
    if org_scope is not None:
        existing_count = (
            await db.execute(
                select(func.count(Workspace.id)).where(
                    Workspace.organization_id == org_scope,
                    Workspace.is_deleted == False,  # noqa: E712
                )
            )
        ).scalar() or 0
        cap = getattr(_settings, "MAX_WORKSPACES_PER_ORG", 200)
        if existing_count >= cap:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=402,
                detail=f"Workspace limit reached ({cap} for this organization)",
            )

    workspace = Workspace(
        name=data.name,
        description=data.description,
        is_public=data.is_public,
        owner_id=owner_id,
        organization_id=organization_id,
    )
    db.add(workspace)
    db.add(
        WorkspaceMember(workspace=workspace, user_id=owner_id, role=WorkspaceRole.OWNER)
    )
    await db.flush()

    # Re-fetch with full eager load — the response/presenter reads
    # member/conversation/collection counts immediately after create.
    created = await workspace_access.get_workspace(db, workspace.id, owner_id)
    if created is None:
        raise RuntimeError(
            "Workspace lookup failed immediately after creation"
        )  # pragma: no cover
    return created


async def list_workspaces(
    db: AsyncSession,
    user_id: UUID,
    *,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    filter_deleted_memberships: bool = True,
) -> Tuple[List[Workspace], int]:
    """List workspaces the caller is a member of.

    Always eager-loads members/conversations/collections — the count fields
    every ``WorkspaceResponse`` renders.

    ``filter_deleted_memberships=True`` (the fail-closed default) excludes
    workspaces the caller was removed from. Passing ``False`` shows them;
    no in-repo caller does, and any new one must justify it explicitly.
    """
    base_conditions = [
        WorkspaceMember.user_id == user_id,
        Workspace.is_deleted == False,  # noqa: E712
    ]
    if filter_deleted_memberships:
        base_conditions.append(WorkspaceMember.is_deleted == False)  # noqa: E712
    if not include_archived:
        base_conditions.append(Workspace.is_archived == False)  # noqa: E712

    count_stmt = (
        select(func.count(Workspace.id)).join(WorkspaceMember).where(*base_conditions)
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        select(Workspace)
        .options(
            selectinload(Workspace.members),
            selectinload(Workspace.conversations),
            selectinload(Workspace.collections),
        )
        .join(WorkspaceMember)
        .where(*base_conditions)
        .order_by(desc(Workspace.updated_at))
        .offset(offset)
        .limit(limit)
    )
    workspaces = (await db.execute(stmt)).scalars().all()
    return list(workspaces), total


async def update_workspace(
    db: AsyncSession, workspace_id: UUID, data: WorkspaceUpdate, user_id: UUID
) -> Optional[Workspace]:
    """Update a workspace. ``None`` if not found/accessible; raises
    ``PermissionError`` if found but the caller lacks admin rights."""
    workspace = await workspace_access.get_workspace(
        db, workspace_id, user_id, for_update=True
    )
    if not workspace:
        return None
    if not workspace.can_user_admin(str(user_id)):
        raise PermissionError("Insufficient permissions")

    if data.name is not None:
        workspace.name = data.name
    if data.description is not None:
        workspace.description = data.description
    if data.is_public is not None:
        workspace.is_public = data.is_public
    if data.is_archived is not None:
        workspace.is_archived = data.is_archived

    workspace.updated_at = datetime.utcnow()
    await db.flush()
    # No db.refresh(): every mutated field is a Python-side assignment
    # already reflecting final state, and a bare refresh() would expire the
    # members/conversations/collections eager-loaded above (untouched by
    # this mutation) — the presenter reads all three right after, and an
    # expired-then-accessed relationship MissingGreenlets under the async
    # session.
    return workspace


async def delete_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    stamp_deleted_at: bool = False,
) -> Optional[bool]:
    """Soft-delete a workspace. ``None`` if not found; raises
    ``PermissionError`` if found but the caller isn't the owner.

    ``stamp_deleted_at=True`` (router-canonical) also sets ``deleted_at``.
    ``False`` (old ``ChatService`` default) leaves it ``NULL``, matching the
    pre-4.3 divergence recorded for existing ``ChatService`` callers.
    """
    workspace = await workspace_access.get_workspace(
        db, workspace_id, user_id, for_update=True
    )
    if not workspace:
        return None
    if str(workspace.owner_id) != str(user_id):
        raise PermissionError("Only the owner can delete a workspace")

    workspace.is_deleted = True
    if stamp_deleted_at:
        workspace.deleted_at = datetime.utcnow()
    workspace.updated_at = datetime.utcnow()
    await db.flush()
    return True


# =============================================================================
# Workspace member management (router-only before this split; no flag needed)
# =============================================================================


async def add_member(
    db: AsyncSession,
    workspace_id: UUID,
    data: WorkspaceMemberCreate,
    current_user_id: UUID,
) -> Optional[WorkspaceMember]:
    """Add (or restore) a workspace member.

    ``None`` if the workspace isn't found/accessible. Raises
    ``PermissionError`` if the caller can't admin the workspace, or
    ``ValueError`` if the target user is already an active member — both
    ports of the router's exact checks.
    """
    if data.role.value == WorkspaceRole.OWNER.value:
        # OWNER is reserved for the creating owner's row: remove_member and
        # update_member_role both refuse OWNER rows, so a minted second owner
        # would hold admin rights forever with no API path to demote/remove.
        raise ValueError("Cannot assign owner role to a member")
    workspace = await workspace_access.get_workspace(
        db, workspace_id, current_user_id, for_update=True
    )
    if not workspace:
        return None
    if not workspace.can_user_admin(str(current_user_id)):
        raise PermissionError("Insufficient permissions to add members")

    # R5-L9: validate the target user exists — the FK violation surfaced as
    # a raw IntegrityError 500 otherwise.
    from src.models.user import User

    user_exists = (
        await db.execute(select(User.id).where(User.id == data.user_id))
    ).scalar_one_or_none()
    if user_exists is None:
        raise ValueError("Target user does not exist")

    stmt = (
        select(WorkspaceMember)
        .options(selectinload(WorkspaceMember.user))
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == data.user_id,
        )
    )
    existing: Optional[WorkspaceMember] = (await db.execute(stmt)).scalars().first()

    if existing and not existing.is_deleted:
        raise ValueError("User is already a member")

    member: Optional[WorkspaceMember]
    if existing:
        # Restore a previously removed member. uq_workspace_member is not a
        # partial index, so inserting a fresh row for a re-added user would
        # hit the unique constraint and 500 — restore the soft-deleted row.
        existing.restore()
        existing.role = data.role
        existing.invited_by_id = current_user_id
        existing.joined_at = datetime.utcnow()
        member = existing
        await db.flush()
    else:
        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=data.user_id,
            role=data.role,
            invited_by_id=current_user_id,
        )
        db.add(member)
        await db.flush()
        # A freshly-constructed member's `.user` was never loaded (it's a
        # many-to-one that would otherwise lazy-load on first access, which
        # MissingGreenlets under the async session) — re-fetch it eager-loaded
        # rather than db.refresh(), which would only re-expire it again.
        member = (await db.execute(stmt)).scalars().first()
    return member


async def update_member_role(
    db: AsyncSession,
    workspace_id: UUID,
    target_user_id: UUID,
    data: WorkspaceMemberUpdate,
    current_user_id: UUID,
) -> Optional[WorkspaceMember]:
    """Update a member's role. ``None`` if workspace/member not found.
    Raises ``PermissionError``/``ValueError`` matching the router's checks."""
    if data.role.value == WorkspaceRole.OWNER.value:
        # Same guard as add_member: an OWNER row is irremovable via the API.
        raise ValueError("Cannot assign owner role to a member")
    workspace = await workspace_access.get_workspace(
        db, workspace_id, current_user_id, for_update=True
    )
    if not workspace:
        return None
    if not workspace.can_user_admin(str(current_user_id)):
        raise PermissionError("Insufficient permissions")

    stmt = (
        select(WorkspaceMember)
        .options(selectinload(WorkspaceMember.user))
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target_user_id,
            WorkspaceMember.is_deleted == False,  # noqa: E712
        )
    )
    member: Optional[WorkspaceMember] = (await db.execute(stmt)).scalars().first()
    if not member:
        return None
    if member.role == WorkspaceRole.OWNER:
        raise ValueError("Cannot change owner role")

    member.role = data.role
    member.updated_at = datetime.utcnow()
    await db.flush()
    # No db.refresh(): `.user` is eager-loaded above and untouched by this
    # mutation; refresh() would only expire it again (MissingGreenlet on the
    # presenter's next access under the async session).
    return member


async def remove_member(
    db: AsyncSession,
    workspace_id: UUID,
    target_user_id: UUID,
    current_user_id: UUID,
) -> Optional[bool]:
    """Remove (soft-delete) a workspace member. ``None`` if workspace/member
    not found. Raises ``PermissionError``/``ValueError`` matching the
    router's checks (self-removal always allowed; owner can't be removed)."""
    workspace = await workspace_access.get_workspace(
        db, workspace_id, current_user_id, for_update=True
    )
    if not workspace:
        return None
    if str(target_user_id) != str(current_user_id) and not workspace.can_user_admin(
        str(current_user_id)
    ):
        raise PermissionError("Insufficient permissions")

    stmt = select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == target_user_id,
        WorkspaceMember.is_deleted == False,  # noqa: E712
    )
    member = (await db.execute(stmt)).scalars().first()
    if not member:
        return None
    if member.role == WorkspaceRole.OWNER:
        raise ValueError("Cannot remove workspace owner")

    member.is_deleted = True
    member.deleted_at = datetime.utcnow()
    await db.flush()
    return True
