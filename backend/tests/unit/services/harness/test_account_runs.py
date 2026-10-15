"""Local Codex runs under an account approval (account-connection design §3).

SQLite unit evidence. The PostgreSQL twin is
backend/tests/integration/test_harness_account_postgres.py.

Mutation verification (2026-10-14, PR 2 Tasks 1-3): for each guard named in a
``# Mutation`` comment below, the line was broken in a throwaway copy of the
backend, the named tests were observed FAIL, and the source was restored.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.services.harness.test_runs import (  # noqa: F401
    CONVERSATION,
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
    context,
    db,
)

from src.api.agent.harness_streaming import create_chat_context
from src.core.config import settings
from src.models.agent_run import AgentRun
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.account import (
    consent_kind,
    live_account_consent,
    project_in_account,
    run_project,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    create_request,
    decide_request,
    exchange_request,
    mint_integration_grant,
    resolve_integration_context,
)

pytestmark = pytest.mark.unit
OWNER = SimpleNamespace(id=USER, organization_id=ORG)
(
    ACCOUNT_DEVICE,
    WORKSPACE_B,
    PROJECT_B,
    CONVERSATION_B,
    THREAD_B,
    FOLDER_A,
    FOLDER_B,
) = (uuid4() for _ in range(7))
PAIRING_SCOPES = {"harness:execute", "tools:read"}


@pytest.fixture(autouse=True)
async def account_world(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second workspace and project in the account, and an account device."""
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)
    connection = await db.connection()
    models: tuple[Any, ...] = (HarnessCommand, HarnessReceipt, HarnessNativeRequest)
    for model in models:
        await connection.run_sync(model.__table__.create, checkfirst=True)
    # One flush per parent level: these models declare no relationships.
    for row in (
        Workspace(id=WORKSPACE_B, name="w2", owner_id=USER, organization_id=ORG),
        Collection(id=PROJECT_B, workspace_id=WORKSPACE_B, name="p2"),
        Conversation(
            id=CONVERSATION_B, workspace_id=WORKSPACE_B, title="c2", created_by_id=USER
        ),
        Thread(
            id=THREAD_B,
            conversation_id=CONVERSATION_B,
            source_project_id=PROJECT_B,
            title="t2",
            created_by_id=USER,
            message_count=0,
        ),
        BridgeDevice(
            id=ACCOUNT_DEVICE, user_id=USER, organization_id=ORG, label="desktop"
        ),
    ):
        db.add(row)
        await db.flush()
    await db.commit()


async def _paired(db: AsyncSession, data: GrantRequestCreate) -> IntegrationContext:
    request = await create_request(db, OWNER, data)
    await decide_request(db, OWNER, request.id, True)
    issued = await exchange_request(db, OWNER, request.id)
    return await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )


async def pair_account(
    db: AsyncSession, device: UUID = ACCOUNT_DEVICE
) -> IntegrationContext:
    """The bridge's account pairing grant for ``device``."""
    return await _paired(
        db, GrantRequestCreate(account=True, device_id=device, scopes=PAIRING_SCOPES)
    )


async def pair_project(
    db: AsyncSession, project_id: UUID, device: UUID
) -> IntegrationContext:
    """A project pairing grant (today's kind) for ``device``."""
    return await _paired(
        db,
        GrantRequestCreate(
            project_id=project_id, device_id=device, scopes={"harness:execute"}
        ),
    )


@pytest.fixture
async def account(db: AsyncSession) -> IntegrationContext:
    """The account pairing on ACCOUNT_DEVICE, with two account folders."""
    paired = await pair_account(db)
    db.add_all(
        [
            WorkspaceBinding(
                device_id=ACCOUNT_DEVICE,
                workspace_id=FOLDER_A,
                project_id=None,
                label="a",
            ),
            WorkspaceBinding(
                device_id=ACCOUNT_DEVICE,
                workspace_id=FOLDER_B,
                project_id=None,
                label="b",
            ),
        ]
    )
    await db.commit()
    return paired


async def start(
    db: AsyncSession, thread_id: UUID, folder: UUID, device: UUID = ACCOUNT_DEVICE
) -> str:
    """One chat turn that runs Local Codex in ``folder``; returns the run id."""
    thread = await db.get(Thread, thread_id)
    run_context = await create_chat_context(
        db, current_user=OWNER, thread=thread, device_id=device, workspace_id=folder
    )
    accepted = await accept_submission(
        db,
        current_user=OWNER,
        request=AgentExecuteRequest(
            messages=[
                AgentMessage(role="user", content="hello", client_message_id=uuid4())
            ],
            execution_provider="codex",
            device_id=device,
            workspace_id=folder,
            thread_id=str(thread_id),
        ),
        thread=thread,
        integration_context=run_context,
    )
    return str(accepted.run_id)


async def leave_account(db: AsyncSession, workspace_id: UUID = WORKSPACE_B) -> None:
    """Archive a workspace: its projects leave the account; chat access stays."""
    await db.execute(
        update(Workspace).where(Workspace.id == workspace_id).values(is_archived=True)
    )
    await db.commit()


async def _mint(
    db: AsyncSession,
    project_id: UUID,
    thread_id: UUID,
    device_id: UUID,
    **extra: Any,
) -> Any:
    return await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=project_id,
        scopes=frozenset({"harness:execute"}),
        thread_id=thread_id,
        device_id=device_id,
        **extra,
    )


async def test_mint_under_the_account_approval_issues_a_project_grant(
    db: AsyncSession, account: IntegrationContext
) -> None:
    consent = await live_account_consent(
        db, user_id=USER, organization_id=ORG, device_id=ACCOUNT_DEVICE
    )
    assert consent is not None and consent.id == account.consent_id
    issued = await _mint(
        db, PROJECT_B, THREAD_B, ACCOUNT_DEVICE, consent_kind="account"
    )
    grant = await db.get(IntegrationGrant, issued.grant_id)
    assert grant is not None
    assert (grant.binding_kind, grant.project_id, grant.thread_id) == (
        "project",
        PROJECT_B,
        THREAD_B,
    )
    assert grant.request_id == consent.id
    assert await consent_kind(db, grant.request_id) == "account"


# Mutation MA2: in services/integrations/context._consent_admits replace the
# account branch's `exists().where(Collection.id == project_id,
# account_scope_condition(...))` with `true()`; this test must fail.
async def test_mint_under_the_account_approval_refuses_a_project_outside_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await _mint(db, PROJECT_B, THREAD_B, ACCOUNT_DEVICE, consent_kind="account")


async def test_mint_under_the_account_approval_needs_one_on_the_device(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # DEVICE holds only a project approval (the `context` fixture).
    with pytest.raises(IntegrationAccessDenied):
        await _mint(db, PROJECT, THREAD, DEVICE, consent_kind="account")


# Mutation MA2b: delete the `consent_kind is not None and device_id is None`
# refusal in mint_integration_grant; this test must fail.
async def test_a_stated_consent_kind_needs_a_device(
    db: AsyncSession, account: IntegrationContext
) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT_B,
            scopes=frozenset({"harness:execute"}),
            thread_id=THREAD_B,
            consent_kind="account",
        )


async def test_a_project_approval_mints_only_for_its_own_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    await pair_project(db, PROJECT, ACCOUNT_DEVICE)
    # consent_kind="project" ignores the device's account approval...
    with pytest.raises(IntegrationAccessDenied):
        await _mint(db, PROJECT_B, THREAD_B, ACCOUNT_DEVICE, consent_kind="project")
    # ...and uses the project approval for its own project.
    issued = await _mint(db, PROJECT, THREAD, ACCOUNT_DEVICE, consent_kind="project")
    grant = await db.get(IntegrationGrant, issued.grant_id)
    assert grant is not None
    assert await consent_kind(db, grant.request_id) == "project"


async def test_the_live_account_consent_must_be_one_runnable_approval(
    db: AsyncSession, account: IntegrationContext
) -> None:
    async def live() -> IntegrationGrantRequest | None:
        return await live_account_consent(
            db, user_id=USER, organization_id=ORG, device_id=ACCOUNT_DEVICE
        )

    # An account approval without harness:execute does not count, so it cannot
    # make the run approval ambiguous either.
    other = IntegrationGrantRequest(
        id=uuid4(),
        user_id=USER,
        organization_id=ORG,
        binding_kind="account",
        device_id=ACCOUNT_DEVICE,
        scopes=["tools:read"],
        status="consumed",
        expires_at=datetime.now(timezone.utc),
    )
    db.add(other)
    await db.commit()
    found = await live()
    assert found is not None and found.id == account.consent_id
    # Two runnable approvals are ambiguous: fail closed.
    other.scopes = ["harness:execute"]
    await db.commit()
    assert await live() is None
    # A revoked one is gone.
    other.consent_revoked_at = datetime.now(timezone.utc)
    await db.commit()
    found = await live()
    assert found is not None and found.id == account.consent_id


async def test_a_project_is_in_the_account_only_while_the_account_covers_it(
    db: AsyncSession, account: IntegrationContext
) -> None:
    async def inside(project_id: UUID | None) -> bool:
        return await project_in_account(
            db, user_id=USER, organization_id=ORG, project_id=project_id
        )

    assert await inside(PROJECT) and await inside(PROJECT_B)
    assert not await inside(None) and not await inside(uuid4())
    await leave_account(db)
    assert await inside(PROJECT) and not await inside(PROJECT_B)


def test_run_project_takes_the_project_from_the_run_only_under_an_account_grant() -> (
    None
):
    run = SimpleNamespace(project_id=PROJECT_B)
    ids: dict[str, Any] = {"user_id": USER, "organization_id": ORG, "grant_id": uuid4()}
    assert run_project(IntegrationContext(binding_kind="account", **ids), run) == (
        PROJECT_B
    )
    # A project grant names its own project, whatever the run says.
    assert run_project(IntegrationContext(project_id=PROJECT, **ids), run) == PROJECT
    # A workspace grant never runs Codex.
    with pytest.raises(IntegrationAccessDenied):
        run_project(IntegrationContext(workspace_id=WORKSPACE, **ids), run)
