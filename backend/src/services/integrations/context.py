"""Restricted integration authority. Writes and transaction boundaries live here."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Iterable, cast
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, exists, false, or_, select, update
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.core.cli_token_revocation import revoke_user_cli_tokens
from src.models.agent_run import AgentRun
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import (
    STANDARD_SCOPES,
    DeviceCreate,
    GrantRequestCreate,
    GrantRequestDTO,
    GrantRequestStatus,
    IntegrationContext,
    IssuedGrant,
    WorkspaceBindingCreate,
)


class IntegrationAccessDenied(PermissionError):
    """Opaque denial: do not reveal foreign objects or credentials."""


class IntegrationConflict(Exception):
    """A request was already decided, consumed or expired."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def live(value: datetime) -> bool:
    return value.replace(tzinfo=timezone.utc) > now()


# A grant never carries a library scope without the gateway scope the
# /integrations/tools router requires, nor a write without its read.
_SCOPE_REQUIRES = {
    "library:read": frozenset({"tools:read"}),
    "library:write": frozenset({"library:read", "tools:write"}),
}
# Harness runs, artifact publication, chat handoffs and selected memories stay
# bound to one Collection. A handoff also needs a chat, which a workspace grant
# cannot have; the memories a user selects are stored per consent of one
# project, and read_selected_context takes its project from the grant.
_PROJECT_ONLY_SCOPES = frozenset(
    {
        "harness:execute",
        "artifacts:publish",
        "handoff:read",
        "handoff:write",
        "context:read",
    }
)


def check_scopes(scopes: Iterable[str], *, workspace_bound: bool = False) -> None:
    held = set(scopes)
    if not held or not held <= STANDARD_SCOPES:
        raise IntegrationAccessDenied()
    if any(
        not needed <= held for scope, needed in _SCOPE_REQUIRES.items() if scope in held
    ):
        raise IntegrationAccessDenied()
    if workspace_bound and held & _PROJECT_ONLY_SCOPES:
        raise IntegrationAccessDenied()


def _legacy_workspace_in_org(organization_id: UUID) -> ColumnElement[bool]:
    """A workspace without organization metadata belongs to its owner's.

    ``workspaces.organization_id`` is nullable and legacy rows carry NULL.
    Research-engine access (``project_access.py``) coalesces such a row to its
    owner's organization; every integration check must agree with it. The
    owner is aliased so an enclosing query that joins ``users`` cannot
    correlate this subquery onto its own row.
    """
    owner = aliased(User)
    return and_(
        Workspace.organization_id.is_(None),
        exists().where(
            owner.id == Workspace.owner_id,
            owner.organization_id == organization_id,
        ),
    )


def workspace_in_org(organization_id: UUID | None) -> ColumnElement[bool]:
    """``Workspace`` belongs to ``organization_id``, legacy NULL rows included.

    The one organization predicate for integration queries that join
    ``Workspace``. Never compare ``Workspace.organization_id`` directly
    (rule c of test_integration_boundaries.py): 2c3d56d82 taught two checks
    the legacy rule and missed three (WG-2). A missing organization admits
    nothing: ``== None`` would compile to ``IS NULL`` and match every legacy row.
    """
    if organization_id is None:
        return false()
    return or_(
        Workspace.organization_id == organization_id,
        _legacy_workspace_in_org(organization_id),
    )


async def workspace_organization_id(
    db: AsyncSession, workspace: Workspace
) -> UUID | None:
    """The organization ``workspace_in_org`` places ``workspace`` in: its own,
    or for a legacy workspace without one, its owner's (None when neither
    has one). For callers that need the value, such as the browser handoff
    card."""
    if workspace.organization_id is not None:
        return cast(UUID, workspace.organization_id)
    owner_org = await db.scalar(
        select(User.organization_id).where(User.id == workspace.owner_id)
    )
    return cast("UUID | None", owner_org)


def _workspace_organization_admits(
    organization_id: UUID | None,
) -> ColumnElement[bool]:
    """The organization rule of ``authorized_project`` and
    ``authorized_workspace``: a live owning organization (an explicit member
    from another organization keeps access), or a legacy workspace whose
    owner is in the caller's organization. A missing organization admits
    nothing. The owning organization is aliased for the same reason as the
    owner in ``_legacy_workspace_in_org``."""
    if organization_id is None:
        return false()
    owning = aliased(Organization)
    return or_(
        exists().where(
            owning.id == Workspace.organization_id,
            owning.is_deleted.is_(False),
            owning.is_active.is_(True),
        ),
        _legacy_workspace_in_org(organization_id),
    )


async def authorized_project(
    db: AsyncSession, user_id: UUID, organization_id: UUID, project_id: UUID
) -> Row[Any]:
    row = (
        await db.execute(
            select(Collection.id, Collection.name, Workspace.id.label("workspace_id"))
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                Collection.id == project_id,
                Workspace.is_deleted.is_(False),
                # Owner or live member. Keep the Collection predicate inline
                # for test_project_service_soft_delete's authorization sweep.
                Collection.is_deleted.is_(False),
                or_(
                    Workspace.owner_id == user_id,
                    exists().where(
                        WorkspaceMember.workspace_id == Workspace.id,
                        WorkspaceMember.user_id == user_id,
                        WorkspaceMember.is_deleted.is_(False),
                    ),
                ),
                # Legacy workspaces without organization metadata inherit the
                # owner's organization, matching research-engine access.
                _workspace_organization_admits(organization_id),
                exists().where(
                    User.id == user_id,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                ),
                exists().where(
                    Organization.id == organization_id,
                    Organization.is_deleted.is_(False),
                    Organization.is_active.is_(True),
                ),
            )
        )
    ).first()
    if row is None:
        raise IntegrationAccessDenied()
    return row


async def authorized_workspace(
    db: AsyncSession, user_id: UUID, organization_id: UUID, workspace_id: UUID
) -> Row[Any]:
    """The workspace if the user owns it or is a live member, under the same
    rules as ``authorized_project``: a public workspace alone is not access,
    and the owning organization, the user and the user's organization are live."""
    member = exists().where(
        WorkspaceMember.workspace_id == Workspace.id,
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.is_deleted.is_(False),
    )
    row = (
        await db.execute(
            select(Workspace.id, Workspace.name).where(
                Workspace.id == workspace_id,
                Workspace.is_deleted.is_(False),
                or_(Workspace.owner_id == user_id, member),
                _workspace_organization_admits(organization_id),
                exists().where(
                    User.id == user_id,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                ),
                exists().where(
                    Organization.id == organization_id,
                    Organization.is_deleted.is_(False),
                    Organization.is_active.is_(True),
                ),
            )
        )
    ).first()
    if row is None:
        raise IntegrationAccessDenied()
    return row


async def authorized_scope_filter(
    db: AsyncSession, context: IntegrationContext
) -> ColumnElement[bool]:
    """The grant's scope as a condition on ``Collection``, after the same
    per-call access re-check as ``authorized_scope``.

    Filter by it where the ids ``authorized_scope`` returns would only be bound
    back into a statement. A workspace can hold more live Collections than one
    statement may bind (asyncpg refuses more than 32,767 parameters, and the
    collection routes put no cap on a workspace), so an ``IN`` list of its ids
    is an outage waiting for that many folders. The condition does not exclude
    soft-deleted Collections or Workspaces: the caller's statement must.
    """
    if context.project_id is not None:
        await authorized_project(
            db, context.user_id, context.organization_id, context.project_id
        )
        return Collection.id == context.project_id
    if context.workspace_id is None:
        raise IntegrationAccessDenied()
    await authorized_workspace(
        db, context.user_id, context.organization_id, context.workspace_id
    )
    return Collection.workspace_id == context.workspace_id


async def authorized_scope(db: AsyncSession, context: IntegrationContext) -> set[UUID]:
    """Collection ids the grant may touch; re-checked on every call."""
    scope = await authorized_scope_filter(db, context)
    rows = await db.execute(
        select(Collection.id).where(scope, Collection.is_deleted.is_(False))
    )
    return {UUID(str(value)) for value in rows.scalars().all()}


async def owned_device(
    db: AsyncSession, device_id: UUID, user_id: UUID, organization_id: UUID
) -> BridgeDevice:
    device = await db.scalar(
        select(BridgeDevice)
        .where(
            BridgeDevice.id == device_id,
            BridgeDevice.user_id == user_id,
            BridgeDevice.organization_id == organization_id,
            BridgeDevice.is_deleted.is_(False),
            BridgeDevice.revoked_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if device is None:
        raise IntegrationAccessDenied()
    return cast(BridgeDevice, device)


async def validate_binding(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None,
    workspace_id: UUID | None = None,
    thread_id: UUID | None = None,
    run_id: UUID | str | None = None,
    device_id: UUID | None = None,
) -> None:
    if project_id is not None and workspace_id is None:
        await authorized_project(db, user_id, organization_id, project_id)
    elif workspace_id is not None and project_id is None:
        await authorized_workspace(db, user_id, organization_id, workspace_id)
        # A workspace has no single project for a chat or a run to belong to.
        if thread_id is not None or run_id is not None:
            raise IntegrationAccessDenied()
    else:
        raise IntegrationAccessDenied()
    if device_id is not None:
        await owned_device(db, device_id, user_id, organization_id)
    if thread_id is not None:
        thread = await db.scalar(
            select(Thread.id)
            .join(Conversation, Thread.conversation_id == Conversation.id)
            .join(Workspace, Conversation.workspace_id == Workspace.id)
            .where(
                Thread.id == thread_id,
                Thread.is_deleted.is_(False),
                Conversation.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
                or_(
                    Workspace.owner_id == user_id,
                    exists().where(
                        WorkspaceMember.workspace_id == Workspace.id,
                        WorkspaceMember.user_id == user_id,
                        WorkspaceMember.is_deleted.is_(False),
                    ),
                ),
                # The chat's workspace is in the grant's organization, by the
                # same legacy rule as authorized_project (DECISION B-1).
                workspace_in_org(organization_id),
                Thread.source_project_id == project_id,
            )
        )
        if thread is None:
            raise IntegrationAccessDenied()
    if run_id is not None:
        run = await db.scalar(
            select(AgentRun.job_id).where(
                AgentRun.job_id == str(run_id),
                AgentRun.user_id == user_id,
                AgentRun.organization_id == organization_id,
                AgentRun.project_id == project_id,
                AgentRun.thread_id == thread_id,
            )
        )
        if run is None or thread_id is None:
            raise IntegrationAccessDenied()


def _new_grant(
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: Iterable[str],
    thread_id: UUID | None = None,
    run_id: UUID | str | None = None,
    device_id: UUID | None = None,
    request_id: UUID | None = None,
) -> tuple[IntegrationGrant, IssuedGrant]:
    token = "nous_ig_" + token_urlsafe(32)
    grant = IntegrationGrant(
        id=uuid4(),
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        workspace_id=workspace_id,
        scopes=sorted(scopes),
        thread_id=thread_id,
        run_id=str(run_id) if run_id else None,
        device_id=device_id,
        request_id=request_id,
        token_hash=sha256(token.encode()).hexdigest(),
        expires_at=now() + timedelta(minutes=15),
        consented_at=now(),
    )
    return grant, IssuedGrant(token=token, grant_id=grant.id)


async def mint_integration_grant(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: frozenset[str],
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
    device_id: UUID | None = None,
) -> IssuedGrant:
    """Trusted internal API. External issuance must use the consent exchange."""
    check_scopes(scopes, workspace_bound=workspace_id is not None)
    await validate_binding(
        db,
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        device_id=device_id,
    )
    request_id = None
    if device_id is not None:
        consents = await db.scalars(
            select(IntegrationGrantRequest)
            .where(
                IntegrationGrantRequest.user_id == user_id,
                IntegrationGrantRequest.organization_id == organization_id,
                IntegrationGrantRequest.project_id == project_id,
                IntegrationGrantRequest.workspace_id == workspace_id,
                IntegrationGrantRequest.device_id == device_id,
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
        # A chat-bound consent authorizes only its chat; a project-wide
        # consent (thread_id NULL) authorizes any chat in the project.
        matching_consents = [
            consent
            for consent in consents
            if scopes <= set(consent.scopes) and consent.thread_id in (None, thread_id)
        ]
        # A device may have more than one consumed consent lineage. Selecting
        # the first database row is nondeterministic and can mint run authority
        # that the currently paired bridge credential cannot renew. Fail closed
        # until the owner leaves one unambiguous active consent for this device.
        if len(matching_consents) != 1:
            raise IntegrationAccessDenied()
        request_id = matching_consents[0].id
    grant, issued = _new_grant(
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        workspace_id=workspace_id,
        scopes=scopes,
        thread_id=thread_id,
        run_id=run_id,
        device_id=device_id,
        request_id=request_id,
    )
    db.add(grant)
    await db.commit()
    return issued


async def _validate_grant(
    db: AsyncSession, grant: IntegrationGrant | None
) -> IntegrationGrant:
    if (
        grant is None
        or grant.is_deleted
        or grant.revoked_at is not None
        or not live(grant.expires_at)
    ):
        raise IntegrationAccessDenied()
    check_scopes(grant.scopes, workspace_bound=grant.workspace_id is not None)
    await validate_binding(
        db,
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        workspace_id=grant.workspace_id,
        thread_id=grant.thread_id,
        run_id=grant.run_id,
        device_id=grant.device_id,
    )
    if grant.device_id is not None:
        consent = await db.scalar(
            select(IntegrationGrantRequest)
            .where(
                IntegrationGrantRequest.id == grant.request_id,
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.user_id == grant.user_id,
                IntegrationGrantRequest.organization_id == grant.organization_id,
                IntegrationGrantRequest.project_id == grant.project_id,
                IntegrationGrantRequest.workspace_id == grant.workspace_id,
                IntegrationGrantRequest.device_id == grant.device_id,
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
        if consent is None or not set(grant.scopes) <= set(consent.scopes):
            raise IntegrationAccessDenied()

    return grant


async def resolve_integration_context(
    db: AsyncSession, token: str, *, required_scope: str | tuple[str, ...]
) -> IntegrationContext:
    """The live grant behind ``token`` as a context, if it holds the scope.

    ``required_scope`` is the scope the route needs, or a tuple of scopes any
    one of which admits the grant: for a route whose service then checks the
    scope of each operation itself (the action routes). An empty tuple admits
    nothing.
    """
    accepted = (required_scope,) if isinstance(required_scope, str) else required_scope
    if not token.startswith("nous_ig_") or len(token) != 51:
        raise IntegrationAccessDenied()
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.token_hash == sha256(token.encode()).hexdigest())
        .execution_options(populate_existing=True)
    )
    grant = await _validate_grant(db, grant)
    if not set(accepted) & set(grant.scopes):
        raise IntegrationAccessDenied()
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        workspace_id=grant.workspace_id,
        thread_id=grant.thread_id,
        run_id=UUID(grant.run_id) if grant.run_id else None,
        grant_id=grant.id,
        consent_id=grant.request_id,
        scopes=frozenset(grant.scopes),
    )


async def revoke_integration_grant(
    db: AsyncSession, grant_id: UUID, *, end_cli_sessions: bool = False
) -> None:
    """Revoke a grant and its consent. ``end_cli_sessions`` (set when the CLI
    disconnects itself) also moves the owner's CLI revoked-before cutoff,
    because CLI JWTs carry no device id."""
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.id == grant_id)
        .execution_options(populate_existing=True)
    )
    if grant is not None:
        await db.execute(
            update(IntegrationGrant)
            .execution_options(synchronize_session="fetch")
            .where(IntegrationGrant.id == grant_id)
            .values(revoked_at=now())
        )
        if grant.request_id:
            await db.execute(
                update(IntegrationGrantRequest)
                .execution_options(synchronize_session="fetch")
                .where(IntegrationGrantRequest.id == grant.request_id)
                .values(consent_revoked_at=now())
            )
    user_id = grant.user_id if grant is not None else None
    await db.commit()
    if end_cli_sessions and user_id is not None:
        # After the commit, never inside it: best-effort and never raises.
        await revoke_user_cli_tokens(str(user_id))


async def create_request(
    db: AsyncSession, user: Any, data: GrantRequestCreate
) -> GrantRequestDTO:
    check_scopes(data.scopes, workspace_bound=data.workspace_id is not None)
    await validate_binding(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=data.project_id,
        workspace_id=data.workspace_id,
        device_id=data.device_id,
        thread_id=data.thread_id,
    )
    request = IntegrationGrantRequest(
        id=uuid4(),
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=data.project_id,
        workspace_id=data.workspace_id,
        device_id=data.device_id,
        thread_id=data.thread_id,
        scopes=sorted(data.scopes),
        status="pending",
        expires_at=now() + timedelta(minutes=10),
    )
    db.add(request)
    await db.commit()
    return await request_dto(db, user, request.id)


async def owned_request(
    db: AsyncSession, user: Any, request_id: UUID
) -> IntegrationGrantRequest:
    request = await db.scalar(
        select(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.user_id == user.id,
            IntegrationGrantRequest.organization_id == user.organization_id,
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise IntegrationAccessDenied()
    await validate_binding(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=request.project_id,
        workspace_id=request.workspace_id,
        device_id=request.device_id,
        thread_id=request.thread_id,
    )
    return cast(IntegrationGrantRequest, request)


async def request_dto(db: AsyncSession, user: Any, request_id: UUID) -> GrantRequestDTO:
    request = await owned_request(db, user, request_id)
    project_label = workspace_label = None
    if request.project_id is not None:
        project = await authorized_project(
            db, user.id, user.organization_id, request.project_id
        )
        project_label = project.name
    elif request.workspace_id is not None:
        workspace = await authorized_workspace(
            db, user.id, user.organization_id, request.workspace_id
        )
        workspace_label = workspace.name
    device = await owned_device(db, request.device_id, user.id, user.organization_id)
    status = request.status
    if status in {"pending", "approved"} and not live(request.expires_at):
        status = "expired"
    thread_label = None
    if request.thread_id is not None:
        title = await db.scalar(
            select(Thread.title).where(
                Thread.id == request.thread_id, Thread.is_deleted.is_(False)
            )
        )
        thread_label = title or "Untitled chat"
    return GrantRequestDTO(
        id=request.id,
        status=cast(GrantRequestStatus, status),
        expires_at=request.expires_at,
        approval_url=f"/integrations/approve?request_id={request.id}",
        project_id=request.project_id,
        workspace_id=request.workspace_id,
        device_id=request.device_id,
        scopes=set(request.scopes),
        project_label=project_label,
        workspace_label=workspace_label,
        device_label=device.label,
        thread_id=request.thread_id,
        thread_label=thread_label,
    )


async def decide_request(
    db: AsyncSession, user: Any, request_id: UUID, approved: bool
) -> GrantRequestDTO:
    await owned_request(db, user, request_id)
    result = await db.execute(
        update(IntegrationGrantRequest)
        .execution_options(synchronize_session="fetch")
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.status == "pending",
            IntegrationGrantRequest.expires_at > now(),
        )
        .values(
            status="approved" if approved else "denied",
            approved_at=now() if approved else None,
        )
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        await db.rollback()
        raise IntegrationConflict()
    await db.commit()
    return await request_dto(db, user, request_id)


async def exchange_request(
    db: AsyncSession, user: Any, request_id: UUID
) -> IssuedGrant:
    request = await owned_request(db, user, request_id)
    # Atomic compare-and-set: exactly one exchanger consumes this browser consent.
    result = await db.execute(
        update(IntegrationGrantRequest)
        .execution_options(synchronize_session="fetch")
        .where(
            IntegrationGrantRequest.id == request_id,
            IntegrationGrantRequest.status == "approved",
            IntegrationGrantRequest.expires_at > now(),
            IntegrationGrantRequest.consent_revoked_at.is_(None),
        )
        .values(status="consumed")
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        await db.rollback()
        raise IntegrationConflict()
    grant, issued = _new_grant(
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=request.project_id,
        workspace_id=request.workspace_id,
        device_id=request.device_id,
        thread_id=request.thread_id,
        scopes=request.scopes,
        request_id=request.id,
    )
    db.add(grant)
    await db.commit()
    return issued


async def owned_grant(db: AsyncSession, user: Any, grant_id: UUID) -> IntegrationGrant:
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(
            IntegrationGrant.id == grant_id,
            IntegrationGrant.user_id == user.id,
            IntegrationGrant.organization_id == user.organization_id,
        )
        .execution_options(populate_existing=True)
    )
    if grant is None:
        raise IntegrationAccessDenied()
    return cast(IntegrationGrant, grant)


async def renew_grant(db: AsyncSession, user: Any, grant_id: UUID) -> IssuedGrant:
    grant = await owned_grant(db, user, grant_id)
    grant = await _validate_grant(db, grant)
    if grant.device_id is None:
        raise IntegrationAccessDenied()
    result = await db.execute(
        update(IntegrationGrant)
        .execution_options(synchronize_session="fetch")
        .where(
            IntegrationGrant.id == grant_id,
            IntegrationGrant.revoked_at.is_(None),
            IntegrationGrant.expires_at > now(),
        )
        .values(revoked_at=now())
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        await db.rollback()
        raise IntegrationConflict()
    replacement, issued = _new_grant(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        workspace_id=grant.workspace_id,
        device_id=grant.device_id,
        scopes=grant.scopes,
        thread_id=grant.thread_id,
        run_id=grant.run_id,
        request_id=grant.request_id,
    )
    db.add(replacement)
    await db.commit()
    return issued


async def register_device(
    db: AsyncSession, user: Any, data: DeviceCreate
) -> BridgeDevice:
    device = BridgeDevice(
        id=uuid4(),
        user_id=user.id,
        organization_id=user.organization_id,
        label=data.label,
    )
    db.add(device)
    await db.commit()
    return device


async def list_devices(db: AsyncSession, user: Any) -> list[BridgeDevice]:
    return list(
        (
            await db.scalars(
                select(BridgeDevice).where(
                    BridgeDevice.user_id == user.id,
                    BridgeDevice.organization_id == user.organization_id,
                    BridgeDevice.revoked_at.is_(None),
                    BridgeDevice.is_deleted.is_(False),
                )
            )
        ).all()
    )


async def bind_workspace(
    db: AsyncSession, user: Any, device_id: UUID, data: WorkspaceBindingCreate
) -> WorkspaceBinding:
    await owned_device(db, device_id, user.id, user.organization_id)
    await authorized_project(db, user.id, user.organization_id, data.project_id)
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == data.workspace_id,
        )
    )
    if binding is not None:
        # A local workspace ID cannot silently change its approved project.
        if (
            binding.project_id != data.project_id
            or binding.label != data.label
            or binding.is_deleted
        ):
            raise IntegrationConflict()
        return cast(WorkspaceBinding, binding)
    binding = WorkspaceBinding(device_id=device_id, **data.model_dump())
    db.add(binding)
    await db.commit()
    return cast(WorkspaceBinding, binding)


async def list_workspaces(
    db: AsyncSession, user: Any, device_id: UUID
) -> list[WorkspaceBinding]:
    await owned_device(db, device_id, user.id, user.organization_id)
    bindings = (
        await db.scalars(
            select(WorkspaceBinding).where(
                WorkspaceBinding.device_id == device_id,
                WorkspaceBinding.is_deleted.is_(False),
            )
        )
    ).all()
    authorized = []
    for binding in bindings:
        try:
            await authorized_project(
                db, user.id, user.organization_id, binding.project_id
            )
        except IntegrationAccessDenied:
            continue
        authorized.append(binding)
    return authorized
