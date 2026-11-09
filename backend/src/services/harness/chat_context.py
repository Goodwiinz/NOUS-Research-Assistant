"""Chat-run grant minting for Local Codex runs.

Moved from ``src.api.agent.harness_streaming`` so a service (machine handoff)
can mint a chat's Codex run authority without importing the API package. The
API module re-exports these names.
"""

from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select

from src.models.bridge_device import WorkspaceBinding
from src.models.integration_grant import IntegrationGrant
from src.schemas.integration_context import BindingKind, IntegrationContext
from src.services.harness.account import live_account_consent, project_in_account
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
    mint_integration_grant,
)


async def _run_consent_kind(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> Literal["project", "account"]:
    """Which approval a chat's Codex run on this folder is minted under.

    An account folder (no project) needs the device's account approval to cover
    the chat's project on this call. A folder tied to the chat's project uses
    the account approval too when the device holds one that covers the
    project: that is the lineage the bridge's account pairing holds and leases
    runs under (``delivery._authorize``), and it wins over a project approval
    as in ``_single_lineage``. Otherwise it uses the project's own approval,
    which the mint then requires. Every refusal is an opaque
    IntegrationAccessDenied.
    """
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        raise IntegrationAccessDenied()
    if (
        binding.project_id is not None
        and binding.project_id != thread.source_project_id
    ):
        raise IntegrationAccessDenied()
    consent = await live_account_consent(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        device_id=device_id,
    )
    covered = consent is not None and await project_in_account(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        project_id=thread.source_project_id,
    )
    if covered:
        return "account"
    if binding.project_id is None:
        raise IntegrationAccessDenied()
    return "project"


async def create_chat_context(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Mint a short-lived run-authority from prior browser-approved consent.

    The returned token is intentionally discarded. It is never returned to the
    browser; only its database identity is attached to the accepted run.

    Every refusal about this computer, its folder or the chat's project is
    raised as DeviceNotConnectedToProject: one message for all of them, so the
    chat can name it without revealing which check failed or whether a device
    or folder id exists. A computer bound to another chat keeps its own
    message; server-policy refusals stay ValueError and so the generic error.
    """
    from src.core.config import settings

    if not settings.HARNESS_BRIDGE_ENABLED:
        raise ValueError("Local harness execution is disabled by server policy")
    if thread is None or thread.source_project_id is None:
        raise ValueError("Codex runs require a project-bound NOUS chat thread")
    if current_user.organization_id is None:
        raise ValueError("Codex runs require an organization-backed account")
    try:
        authorized: IntegrationContext = await _authorize_chat_run(
            db,
            current_user=current_user,
            thread=thread,
            device_id=device_id,
            workspace_id=workspace_id,
        )
    except DeviceBoundToAnotherChat:
        raise
    except IntegrationAccessDenied as exc:
        raise DeviceNotConnectedToProject() from exc
    return authorized


async def _authorize_chat_run(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Check the folder, decide the approval and mint the run's grant (the
    policy guards have passed). Denials here are opaque; the caller names them
    for the chat."""
    kind = await _run_consent_kind(
        db,
        current_user=current_user,
        thread=thread,
        device_id=device_id,
        workspace_id=workspace_id,
    )
    issued = await mint_integration_grant(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        project_id=thread.source_project_id,
        scopes=frozenset({"harness:execute"}),
        thread_id=thread.id,
        device_id=device_id,
        consent_kind=kind,
    )
    # Re-resolve through the same validation gate used by the bridge transport.
    grant = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    if grant is None:
        raise PermissionError("Could not bind Codex run to its accepted grant")
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        binding_kind=cast(BindingKind, grant.binding_kind),
        thread_id=grant.thread_id,
        run_id=None,
        grant_id=grant.id,
    )
