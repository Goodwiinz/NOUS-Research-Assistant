"""Restricted integration authority. Writes and transaction boundaries live here."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Iterable, cast
from uuid import UUID, uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

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


def check_scopes(scopes: Iterable[str]) -> None:
    if not scopes or not set(scopes) <= STANDARD_SCOPES:
        raise IntegrationAccessDenied()


async def authorized_project(
    db: AsyncSession, user_id: UUID, organization_id: UUID, project_id: UUID
) -> Row[Any]:
    member = exists().where(
        WorkspaceMember.workspace_id == Workspace.id,
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.is_deleted.is_(False),
    )
    row = (
        await db.execute(
            select(Collection.id, Collection.name, Workspace.id.label("workspace_id"))
            .join(Workspace, Collection.workspace_id == Workspace.id)
            .where(
                Collection.id == project_id,
                Collection.is_deleted.is_(False),
                Workspace.is_deleted.is_(False),
                or_(Workspace.owner_id == user_id, member),
                exists().where(
                    Organization.id == Workspace.organization_id,
                    Organization.is_deleted.is_(False),
                    Organization.is_active.is_(True),
                ),
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
    project_id: UUID,
    thread_id: UUID | None = None,
    run_id: UUID | str | None = None,
    device_id: UUID | None = None,
) -> None:
    await authorized_project(db, user_id, organization_id, project_id)
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
                Workspace.owner_id == user_id,
                Workspace.organization_id == organization_id,
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
    project_id: UUID,
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
    project_id: UUID,
    scopes: frozenset[str],
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
    device_id: UUID | None = None,
) -> IssuedGrant:
    """Trusted internal API. External issuance must use the consent exchange."""
    check_scopes(scopes)
    await validate_binding(
        db,
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
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
                IntegrationGrantRequest.device_id == device_id,
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
        matching_consents = [
            consent for consent in consents if scopes <= set(consent.scopes)
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
    check_scopes(grant.scopes)
    await validate_binding(
        db,
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
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
    db: AsyncSession, token: str, *, required_scope: str
) -> IntegrationContext:
    if not token.startswith("nous_ig_") or len(token) != 51:
        raise IntegrationAccessDenied()
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.token_hash == sha256(token.encode()).hexdigest())
        .execution_options(populate_existing=True)
    )
    grant = await _validate_grant(db, grant)
    if required_scope not in grant.scopes:
        raise IntegrationAccessDenied()
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        thread_id=grant.thread_id,
        run_id=UUID(grant.run_id) if grant.run_id else None,
        grant_id=grant.id,
        consent_id=grant.request_id,
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
    check_scopes(data.scopes)
    await validate_binding(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=data.project_id,
        device_id=data.device_id,
        thread_id=data.thread_id,
    )
    request = IntegrationGrantRequest(
        id=uuid4(),
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=data.project_id,
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
        device_id=request.device_id,
        thread_id=request.thread_id,
    )
    return cast(IntegrationGrantRequest, request)


async def request_dto(db: AsyncSession, user: Any, request_id: UUID) -> GrantRequestDTO:
    request = await owned_request(db, user, request_id)
    project = await authorized_project(
        db, user.id, user.organization_id, request.project_id
    )
    device = await owned_device(db, request.device_id, user.id, user.organization_id)
    status = request.status
    if status in {"pending", "approved"} and not live(request.expires_at):
        status = "expired"
    thread_label = None
    if request.thread_id is not None:
        title = await db.scalar(
            select(Thread.title).where(Thread.id == request.thread_id)
        )
        thread_label = title or "Untitled chat"
    return GrantRequestDTO(
        id=request.id,
        status=cast(GrantRequestStatus, status),
        expires_at=request.expires_at,
        approval_url=f"/integrations/approve?request_id={request.id}",
        project_id=request.project_id,
        device_id=request.device_id,
        scopes=set(request.scopes),
        project_label=project.name,
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
