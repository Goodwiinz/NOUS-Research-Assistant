"""Account-approval checks for Local Codex runs (account-connection design §3).

An account approval lets any chat in the account run Codex on its device. Each
run still gets a project-kind grant for its chat's project, minted under the
account approval. Once the bridge's account pairing grant replaces that grant
on the session (the long-run swap in ``delivery.lease_commands``), the run's
own project is used, and only while the account rule admits it.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrantRequest
from src.schemas.integration_context import IntegrationContext
from src.services.integrations.context import (
    IntegrationAccessDenied,
    account_scope_condition,
    live_account_run_consent,
)

PROJECT_UNREACHABLE = "This project is no longer reachable from this computer."


class ProjectOutsideAccount(IntegrationAccessDenied):
    """The run's project is not, or is no longer, in the device's account."""


def account_projects(user_id: UUID, organization_id: UUID) -> Select[tuple[UUID]]:
    """The account's Collection ids as a statement, evaluated on every call."""
    return select(Collection.id).where(
        account_scope_condition(user_id, organization_id)
    )


async def project_in_account(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None,
) -> bool:
    if project_id is None:
        return False
    found = await db.scalar(
        account_projects(user_id, organization_id).where(Collection.id == project_id)
    )
    return found is not None


async def live_account_consent(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, device_id: UUID
) -> IntegrationGrantRequest | None:
    """The device's one runnable account approval (see
    ``live_account_run_consent``, shared with account-folder registration and
    listing), else None."""
    consent: IntegrationGrantRequest | None = await live_account_run_consent(
        db, user_id, organization_id, device_id
    )
    return consent


async def consent_kind(db: AsyncSession, request_id: UUID | None) -> str | None:
    """The binding kind of the approval a grant was issued under."""
    if request_id is None:
        return None
    kind = await db.scalar(
        select(IntegrationGrantRequest.binding_kind).where(
            IntegrationGrantRequest.id == request_id
        )
    )
    return None if kind is None else str(kind)


def run_project(context: IntegrationContext, run: Any) -> UUID | None:
    """The project ``context`` acts on for ``run``.

    A project or run grant names it. An account pairing grant names none, so
    the run's own project is used; ``authorize_external_submission`` checks it
    against the account rule on every call. A workspace grant never runs Codex.
    """
    if context.binding_kind == "account":
        project: UUID | None = run.project_id
        return project
    if context.binding_kind == "project":
        named: UUID | None = context.project_id
        return named
    raise IntegrationAccessDenied()
