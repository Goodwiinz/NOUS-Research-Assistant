"""Local Codex runs under an account approval (account-connection design §3).

SQLite unit evidence. The PostgreSQL twin is
backend/tests/integration/test_harness_account_postgres.py.

Mutation verification (2026-10-15, SQLite; the PostgreSQL twin re-checked
MA3+MA15, MA5 and MA1c): MA1, MA1b, MA1c, MA1d, MA1e, MA2, MA2b, MA3, MA4, MA5,
MA6, MA7, MA8, MA9, MA10, MA10b (in test_harness_workspace_guard.py), MA11 and
the live-consent filter drops each observed FAIL, with the source restored
after each. Two guards are redundant by design and are caught only in pairs:
the account lease filter (``_lease_scope``) behind ``ProjectOutsideAccount``
(MA15+MA3), and
``test_a_project_pairing_still_cannot_lease_another_projects_run``, which holds
through five independent guards (the lease project filter, the run-project
comparison in ``_authorize``, the thread-to-project check in
``validate_binding``, the folder predicate in ``authorize_external_submission``
and the consent-lineage check) and fails only when all five are removed.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent.harness_streaming import (
    context_for_accepted_run,
    create_chat_context,
)
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
from src.schemas.harness import BridgeEvent
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


def event_for(command: Any, seq: int = 1, body: Any = None) -> BridgeEvent:
    values = command.model_dump(exclude={"expiresAt", "body", "projectId"})
    values.update(
        sourceId=f"source-{command.runId}",
        sourceSeq=seq,
        body=body
        or {
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"text": "answer"},
        },
    )
    event: BridgeEvent = BridgeEvent.model_validate(values)
    return event


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

    # Each of these would be a second runnable approval but for one property,
    # so each must be ignored (dropping its filter makes the lineage ambiguous).
    def lookalike(**changes: Any) -> IntegrationGrantRequest:
        values: dict[str, Any] = {
            "id": uuid4(),
            "user_id": USER,
            "organization_id": ORG,
            "binding_kind": "account",
            "device_id": ACCOUNT_DEVICE,
            "scopes": ["harness:execute"],
            "status": "consumed",
            "expires_at": datetime.now(timezone.utc),
        }
        values.update(changes)
        return IntegrationGrantRequest(**values)

    ignored = [
        lookalike(status="pending"),
        lookalike(status="denied"),
        lookalike(is_deleted=True),
        lookalike(organization_id=uuid4()),
        lookalike(user_id=uuid4()),
        lookalike(thread_id=THREAD_B),
        lookalike(device_id=uuid4()),
        lookalike(binding_kind="project", project_id=PROJECT),
    ]
    db.add_all(ignored)
    await db.commit()
    found = await live()
    assert found is not None and found.id == account.consent_id
    for row in ignored:
        await db.delete(row)
    await db.commit()
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
        found: bool = await project_in_account(
            db, user_id=USER, organization_id=ORG, project_id=project_id
        )
        return found

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


async def test_an_account_folder_serves_chats_in_every_project_of_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    for thread_id, project_id in ((THREAD, PROJECT), (THREAD_B, PROJECT_B)):
        run_context = await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, thread_id),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )
        assert run_context.binding_kind == "project"
        assert run_context.project_id == project_id
        grant = await db.get(IntegrationGrant, run_context.grant_id)
        assert grant is not None and grant.request_id == account.consent_id
        assert grant.thread_id == thread_id and grant.device_id == ACCOUNT_DEVICE


# End to end, two layers refuse this: _run_consent_kind and the mint's own
# account check (_consent_admits, MA2). Mutation MA1 (delete `or not await
# project_in_account(...)` in harness_streaming._run_consent_kind) is therefore
# caught by test_the_run_consent_kind_is_decided_before_any_grant_is_minted,
# which calls the first layer directly, not by this one.
async def test_an_account_folder_refuses_a_project_outside_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD_B),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )


async def test_an_account_folder_needs_a_live_account_approval_on_its_device(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    # DEVICE holds a project approval only; give it an account folder anyway.
    loose = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=loose, project_id=None, label="x"
        )
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=DEVICE,
            workspace_id=loose,
        )
    # A revoked account approval no longer serves its folders.
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == account.consent_id)
        .values(consent_revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )


async def test_a_project_folder_still_serves_only_its_own_project(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # The device also holds an approval for PROJECT_B, so only the folder check
    # keeps LOCAL (tied to PROJECT) away from a chat in PROJECT_B.
    await pair_project(db, PROJECT_B, DEVICE)
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD_B),
            device_id=DEVICE,
            workspace_id=LOCAL,
        )
    ok = await create_chat_context(
        db,
        current_user=OWNER,
        thread=await db.get(Thread, THREAD),
        device_id=DEVICE,
        workspace_id=LOCAL,
    )
    assert ok.project_id == PROJECT


# Mutation MA1b: drop the `WorkspaceBinding.device_id == device_id` predicate in
# _run_consent_kind; this test must fail.
async def test_a_folder_of_another_device_is_not_this_devices(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    # ACCOUNT_DEVICE holds a project approval for PROJECT, and LOCAL is a
    # folder of DEVICE for PROJECT: only the device predicate refuses it.
    await pair_project(db, PROJECT, ACCOUNT_DEVICE)
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=ACCOUNT_DEVICE,
            workspace_id=LOCAL,
        )


async def test_the_run_consent_kind_is_decided_before_any_grant_is_minted(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    """The folder check refuses on its own: the mint's own account check is a
    second layer, so exercise the first one directly."""
    from src.api.agent.harness_streaming import _run_consent_kind

    async def kind(thread_id: UUID, device: UUID, folder: UUID) -> str:
        decided: str = await _run_consent_kind(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, thread_id),
            device_id=device,
            workspace_id=folder,
        )
        return decided

    assert await kind(THREAD, DEVICE, LOCAL) == "project"
    assert await kind(THREAD, ACCOUNT_DEVICE, FOLDER_A) == "account"
    assert await kind(THREAD_B, ACCOUNT_DEVICE, FOLDER_B) == "account"
    # An account folder on a device without an account approval.
    loose = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=loose, project_id=None, label="x"
        )
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await kind(THREAD, DEVICE, loose)
    # A project the account no longer covers.
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await kind(THREAD_B, ACCOUNT_DEVICE, FOLDER_A)
    assert await kind(THREAD, ACCOUNT_DEVICE, FOLDER_A) == "account"


# Mutation MA1c: return "project" for every folder tied to a project in
# harness_streaming._run_consent_kind (the pre-review behaviour); this test and
# test_a_project_folder_runs_under_the_account_approval_alone must fail.
async def test_a_project_folder_on_a_device_with_both_approvals_runs_under_the_account_one(
    db: AsyncSession, account: IntegrationContext
) -> None:
    project_approval = await pair_project(db, PROJECT, ACCOUNT_DEVICE)
    assert project_approval.consent_id != account.consent_id
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
            project_id=PROJECT,
            label="repo",
        )
    )
    await db.commit()
    # The bridge holds the account pairing, so the account lineage is the one
    # every folder's run is minted under (PR 1's _single_lineage precedence).
    for folder_id in (folder, FOLDER_A):
        run_context = await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder_id,
        )
        grant = await db.get(IntegrationGrant, run_context.grant_id)
        assert grant is not None and grant.request_id == account.consent_id


# Review scenario A: after `nous --rc` a device holds only the account approval.
async def test_a_project_folder_runs_under_the_account_approval_alone(
    db: AsyncSession, account: IntegrationContext
) -> None:
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
            project_id=PROJECT_B,
            label="repo",
        )
    )
    await db.commit()
    run_context = await create_chat_context(
        db,
        current_user=OWNER,
        thread=await db.get(Thread, THREAD_B),
        device_id=ACCOUNT_DEVICE,
        workspace_id=folder,
    )
    grant = await db.get(IntegrationGrant, run_context.grant_id)
    assert grant is not None and grant.request_id == account.consent_id
    # A chat in another project still cannot use the folder.
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
        )


# Review scenario B: both approvals live and the bridge holds the account
# pairing; the project folder's run must lease (lineage = the account one).
async def test_a_project_folder_run_leases_under_the_account_pairing(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )

    await pair_project(db, PROJECT_B, ACCOUNT_DEVICE)
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
            project_id=PROJECT_B,
            label="repo",
        )
    )
    await db.commit()
    run_id = await start(db, THREAD_B, folder)
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    assert str(command.runId) == run_id and command.projectId == PROJECT_B
    assert await ingest_bridge_event(db, account, event_for(command)) >= 1


# Mutation MA1d: return "account" for every project folder; this test must fail.
async def test_a_project_folder_falls_back_to_its_project_approval_when_the_account_lacks_it(
    db: AsyncSession, account: IntegrationContext
) -> None:
    project_approval = await pair_project(db, PROJECT_B, ACCOUNT_DEVICE)
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
            project_id=PROJECT_B,
            label="repo",
        )
    )
    await db.commit()
    await leave_account(db)  # PROJECT_B is no longer in the account
    run_context = await create_chat_context(
        db,
        current_user=OWNER,
        thread=await db.get(Thread, THREAD_B),
        device_id=ACCOUNT_DEVICE,
        workspace_id=folder,
    )
    grant = await db.get(IntegrationGrant, run_context.grant_id)
    assert grant is not None and grant.request_id == project_approval.consent_id


async def test_a_project_folder_with_no_approval_covering_its_project_is_refused(
    db: AsyncSession, account: IntegrationContext
) -> None:
    folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
            project_id=PROJECT_B,
            label="repo",
        )
    )
    await db.commit()
    await leave_account(db)  # no project approval either
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD_B),
            device_id=ACCOUNT_DEVICE,
            workspace_id=folder,
        )


async def test_an_unknown_or_deleted_folder_is_denied(
    db: AsyncSession, account: IntegrationContext
) -> None:
    thread = await db.get(Thread, THREAD)
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=thread,
            device_id=ACCOUNT_DEVICE,
            workspace_id=uuid4(),
        )
    await db.execute(
        update(WorkspaceBinding)
        .where(WorkspaceBinding.workspace_id == FOLDER_A)
        .values(is_deleted=True)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=thread,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )


async def _run_and_session(db: AsyncSession, run_id: str) -> tuple[Any, HarnessSession]:
    run = await db.get(AgentRun, run_id, populate_existing=True)
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == run_id)
    )
    assert run is not None and session is not None
    return run, session


async def test_an_account_folder_run_is_bound_to_its_chats_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    run_id = await start(db, THREAD_B, FOLDER_A)
    run, session = await _run_and_session(db, run_id)
    assert run.project_id == PROJECT_B and run.execution_provider == "codex"
    grant = await db.get(IntegrationGrant, session.grant_id)
    assert grant is not None and grant.binding_kind == "project"
    assert grant.request_id == account.consent_id


async def test_the_run_stream_context_keeps_the_grants_binding_kind(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.api.agent.harness_streaming import context_for_accepted_run

    run_id = await start(db, THREAD_B, FOLDER_A)
    streamed = await context_for_accepted_run(
        db, run_id=UUID(run_id), current_user=OWNER
    )
    assert streamed.binding_kind == "project"
    assert streamed.project_id == PROJECT_B


async def test_the_account_pairing_grant_authorizes_each_run_by_its_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import _authorize

    for thread_id, folder in ((THREAD, FOLDER_A), (THREAD_B, FOLDER_B)):
        run, session = await _run_and_session(db, await start(db, thread_id, folder))
        granted = await _authorize(db, account, run, session)
        assert granted.id == account.grant_id


async def test_a_grant_context_carries_the_grants_binding_kind(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import grant_context

    pairing = await db.get(IntegrationGrant, account.grant_id)
    assert pairing is not None
    rebuilt = grant_context(pairing)
    assert (rebuilt.binding_kind, rebuilt.project_id) == ("account", None)
    _run, session = await _run_and_session(db, await start(db, THREAD, FOLDER_A))
    run_grant = await db.get(IntegrationGrant, session.grant_id)
    assert run_grant is not None
    assert grant_context(run_grant).binding_kind == "project"


# Mutation MA3: delete the `if under_account and not await
# project_in_account(...)` raise in authorize_external_submission; this test
# and test_a_run_grant_names_only_its_own_project must fail.
async def test_an_account_grant_refuses_a_run_whose_project_left_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.account import ProjectOutsideAccount
    from src.services.harness.delivery import _authorize

    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    await leave_account(db)
    with pytest.raises(ProjectOutsideAccount):
        await _authorize(db, account, run, session)


async def test_a_project_grant_cannot_authorize_another_projects_account_run(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import _authorize

    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    with pytest.raises(IntegrationAccessDenied):
        await _authorize(db, context, run, session)


async def test_a_project_pairing_cannot_authorize_an_account_folder_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    """Same device, same project: the project pairing is another lineage and
    an account folder is not its own."""
    from src.services.harness.delivery import _authorize

    project_pairing = await pair_project(db, PROJECT_B, ACCOUNT_DEVICE)
    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    with pytest.raises(IntegrationAccessDenied):
        await _authorize(db, project_pairing, run, session)


async def test_another_devices_account_pairing_cannot_authorize_the_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import _authorize

    other_device = uuid4()
    db.add(
        BridgeDevice(id=other_device, user_id=USER, organization_id=ORG, label="two")
    )
    await db.commit()
    elsewhere = await pair_account(db, other_device)
    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    with pytest.raises(IntegrationAccessDenied):
        await _authorize(db, elsewhere, run, session)


# Mutation R7: drop `grant.device_id != device_id` in
# authorize_external_submission; this test must fail. (The lineage check in
# delivery._authorize refuses the same call, so the first layer is tested
# directly.)
async def test_an_account_pairing_authorizes_only_runs_on_its_own_device(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.runs import authorize_external_submission

    other_device = uuid4()
    db.add(
        BridgeDevice(id=other_device, user_id=USER, organization_id=ORG, label="two")
    )
    await db.commit()
    elsewhere = await pair_account(db, other_device)

    async def authorize(context: IntegrationContext) -> None:
        await authorize_external_submission(
            db,
            context=context,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
            thread_id=THREAD_B,
            project_id=PROJECT_B,
        )

    await authorize(account)
    with pytest.raises(IntegrationAccessDenied):
        await authorize(elsewhere)


# Mutation MA4: add the `WorkspaceBinding.project_id.is_(None)` alternative to
# authorize_external_submission's folder predicates unconditionally (drop
# `if under_account`); this test must fail.
async def test_a_project_approval_never_runs_in_an_account_folder(
    db: AsyncSession, context: IntegrationContext
) -> None:
    from tests.unit.services.harness.test_runs import accept, request

    loose = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=loose, project_id=None, label="x"
        )
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context, request().model_copy(update={"workspace_id": loose}))


async def test_a_run_grant_names_only_its_own_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    """A caller-supplied project is honoured only for an account pairing grant."""
    from src.services.harness.runs import authorize_external_submission

    run_context = await create_chat_context(
        db,
        current_user=OWNER,
        thread=await db.get(Thread, THREAD),
        device_id=ACCOUNT_DEVICE,
        workspace_id=FOLDER_A,
    )

    async def authorize(project_id: UUID | None) -> None:
        await authorize_external_submission(
            db,
            context=run_context,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
            thread_id=THREAD,
            project_id=project_id,
        )

    await authorize(None)
    await authorize(PROJECT)
    with pytest.raises(IntegrationAccessDenied):
        await authorize(PROJECT_B)
    # An account pairing grant needs the run's project, and only a covered one.
    with pytest.raises(IntegrationAccessDenied):
        await authorize_external_submission(
            db,
            context=account,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
            thread_id=THREAD,
        )


async def test_one_account_bridge_leases_runs_in_two_projects(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        lease_commands,
        lease_runs,
    )

    run_a = await start(db, THREAD, FOLDER_A)
    run_b = await start(db, THREAD_B, FOLDER_B)
    assert await dispatch_pending(db) == 2
    commands = await lease_commands(db, account, ACCOUNT_DEVICE)
    assert {(str(c.runId), c.projectId) for c in commands} == {
        (run_a, PROJECT),
        (run_b, PROJECT_B),
    }
    assert set(await lease_runs(db, account, ACCOUNT_DEVICE)) == {run_a, run_b}
    # The long-run swap: both sessions now hold the bridge's pairing grant.
    sessions = (await db.scalars(select(HarnessSession))).all()
    assert {s.grant_id for s in sessions} == {account.grant_id}


# Mutation MA10 (Task 7): replace `context.binding_kind == "account"` in the
# BridgeCommand(projectId=...) expression with `True`; this test must fail.
async def test_a_start_command_names_its_project_only_under_an_account_pairing(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import dispatch_pending, lease_commands
    from tests.unit.services.harness.test_runs import accept

    await accept(db, context)  # a project pairing's run on DEVICE / LOCAL
    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 2
    (project_command,) = await lease_commands(db, context, DEVICE)
    assert project_command.projectId is None
    assert "projectId" not in project_command.wire()  # old bridges refuse the key
    (account_command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    assert account_command.wire()["projectId"] == str(PROJECT_B)


async def test_a_run_whose_project_leaves_the_account_is_refused_at_the_next_command(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
        lease_runs,
    )

    run_a = await start(db, THREAD, FOLDER_A)
    run_b = await start(db, THREAD_B, FOLDER_B)
    assert await dispatch_pending(db) == 2
    commands = {
        str(c.runId): c for c in await lease_commands(db, account, ACCOUNT_DEVICE)
    }
    await leave_account(db)
    await db.execute(update(HarnessCommand).values(lease_until=None))
    await db.commit()
    assert [
        str(c.runId) for c in await lease_commands(db, account, ACCOUNT_DEVICE)
    ] == [run_a]
    assert set(await lease_runs(db, account, ACCOUNT_DEVICE)) == {run_a}
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, account, event_for(commands[run_b]))
    assert await ingest_bridge_event(db, account, event_for(commands[run_a])) >= 1


async def test_a_project_pairing_still_cannot_lease_another_projects_run(
    db: AsyncSession, context: IntegrationContext
) -> None:
    """Regression: a project pairing reaches its own project's runs only.

    Three independent guards hold it (the lease query's project filter,
    run_project in _authorize and the thread binding in validate_binding), so
    no single-line mutation makes it pass; see Task 7.
    """
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
        lease_runs,
    )

    # DEVICE also holds a project approval for PROJECT_B and a folder tied to it.
    other_folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=other_folder, project_id=PROJECT_B, label="b"
        )
    )
    await db.commit()
    pairing_b = await pair_project(db, PROJECT_B, DEVICE)
    run_b = await start(db, THREAD_B, other_folder, device=DEVICE)
    assert await dispatch_pending(db) == 1
    assert await lease_commands(db, context, DEVICE) == []
    assert await lease_runs(db, context, DEVICE) == {}
    (command,) = await lease_commands(db, pairing_b, DEVICE)
    assert str(command.runId) == run_b and command.projectId is None
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, context, event_for(command))


def native_request_event(command: Any, seq: int) -> BridgeEvent:
    return event_for(
        command,
        seq,
        {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-1",
            "requestId": 7,
            "approvalId": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-1",
                "approvalId": "approval-1",
                "command": "pytest -q",
                "cwd": "/workspace",
            },
        },
    )


def running_event(command: Any, seq: int = 1) -> BridgeEvent:
    return event_for(
        command,
        seq,
        {
            "kind": "observation",
            "state": "running",
            "sessionId": "native-session",
            "turnId": "native-turn",
        },
    )


async def expire_run_grants(db: AsyncSession) -> None:
    """Run grants are chat-bound and last 15 minutes; pairing grants are not."""
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.thread_id.is_not(None))
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db.commit()


async def test_dispatch_after_the_bridge_polled_still_starts_the_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import dispatch_pending, lease_commands

    run_id = await start(db, THREAD_B, FOLDER_A)
    # The bridge polls between acceptance and dispatch: the swap comes first.
    assert await lease_commands(db, account, ACCOUNT_DEVICE) == []
    _, session = await _run_and_session(db, run_id)
    assert session.grant_id == account.grant_id
    assert await dispatch_pending(db) == 1
    run, _ = await _run_and_session(db, run_id)
    assert run.status == "queued"


# Mutation MA5 (Task 7): delete `session.grant_id = grant.id` in
# lease_commands; this test must fail.
async def test_a_long_run_outlives_its_run_grant_and_resolves_an_approval_card(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.approvals import (
        NativeDecision,
        ingest_native_request,
        native_request_context,
        resolve_native_request,
    )
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )
    from src.services.integrations.context import renew_grant

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (first,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    await expire_run_grants(db)
    issued = await renew_grant(db, OWNER, account.grant_id)
    renewed = await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )
    # A never-acknowledged start is re-leased: its prompt is rebuilt and
    # checked under the session's (pairing) grant, not the expired run grant.
    await db.execute(update(HarnessCommand).values(lease_until=None))
    await db.commit()
    (again,) = await lease_commands(db, renewed, ACCOUNT_DEVICE)
    assert again.commandId == first.commandId and again.projectId == PROJECT_B
    await ingest_bridge_event(db, renewed, running_event(again, 1))
    request_id, _ = await ingest_native_request(
        db, renewed, native_request_event(again, 2)
    )
    decision_context = await native_request_context(db, request_id, USER)
    assert decision_context.binding_kind == "account"
    assert decision_context.grant_id == renewed.grant_id
    await resolve_native_request(
        db, decision_context, request_id, NativeDecision.allow()
    )
    responses = [
        c
        for c in await lease_commands(db, renewed, ACCOUNT_DEVICE)
        if c.body.kind == "respond"
    ]
    assert len(responses) == 1 and responses[0].projectId is None


# Mutations MA7 / MA8 (Task 7): delete `request.project_id != run.project_id`
# in native_request_context (first assertion fails) or in
# resolve_native_request (second assertion fails).
async def test_a_native_request_recorded_for_another_project_is_refused(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.approvals import (
        NativeDecision,
        ingest_native_request,
        native_request_context,
        resolve_native_request,
    )
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    await ingest_bridge_event(db, account, running_event(command, 1))
    request_id, _ = await ingest_native_request(
        db, account, native_request_event(command, 2)
    )
    decision_context = await native_request_context(db, request_id, USER)
    await db.execute(
        update(HarnessNativeRequest)
        .where(HarnessNativeRequest.id == request_id)
        .values(project_id=PROJECT)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await native_request_context(db, request_id, USER)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_native_request(
            db, decision_context, request_id, NativeDecision.allow()
        )


# Mutation MA6 (Task 7): delete `original.request_id != grant.request_id` from
# _authorize's lineage check; this test must fail.
async def test_a_second_account_approval_on_the_device_cannot_take_over_a_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    other = await pair_account(db)  # a second consent lineage on the same device
    assert other.consent_id != account.consent_id
    assert await lease_commands(db, other, ACCOUNT_DEVICE) == []
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, other, event_for(command))


async def test_a_run_that_cannot_start_says_its_project_left_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.agent.run_event_store import read_events
    from src.services.harness.account import PROJECT_UNREACHABLE
    from src.services.harness.delivery import dispatch_pending

    run_id = await start(db, THREAD_B, FOLDER_A)
    await leave_account(db)
    assert await dispatch_pending(db) == 0
    run, session = await _run_and_session(db, run_id)
    assert run.status == "failed" and not session.workspace_locked
    events = await read_events(db, run_id, organization_id=ORG, user_id=USER)
    assert events[-1].payload["message"] == PROJECT_UNREACHABLE


async def test_stream_resume_after_the_swap_reads_as_the_owner(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import lease_commands

    run_id = await start(db, THREAD_B, FOLDER_A)
    await lease_commands(db, account, ACCOUNT_DEVICE)  # swap
    stream = await context_for_accepted_run(db, run_id=UUID(run_id), current_user=OWNER)
    assert (stream.user_id, stream.organization_id) == (USER, ORG)
    assert stream.grant_id == account.grant_id


async def test_the_prompt_is_built_under_the_swapped_pairing_grant_and_rechecked(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import lease_commands
    from src.services.harness.prompt_context import build_harness_prompt

    run_id = await start(db, THREAD_B, FOLDER_A)
    await lease_commands(db, account, ACCOUNT_DEVICE)  # swap
    _, session = await _run_and_session(db, run_id)
    assert session.grant_id == account.grant_id
    assert (await build_harness_prompt(db, run_id=run_id)).input
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await build_harness_prompt(db, run_id=run_id)


# Mutation MA9: make `_holds_account_run_consent` accept any runnable account
# approval again (`consent is not None` -> `True` over the unfiltered rows);
# this test must fail.
async def test_account_folders_need_the_one_approval_every_run_would_use(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.schemas.integration_context import WorkspaceBindingCreate
    from src.services.integrations.context import bind_workspace, list_workspaces

    async def listed() -> set[UUID]:
        return {
            b.workspace_id for b in await list_workspaces(db, OWNER, ACCOUNT_DEVICE)
        }

    registered = uuid4()
    data = WorkspaceBindingCreate(workspace_id=registered, label="c")
    assert (await bind_workspace(db, OWNER, ACCOUNT_DEVICE, data)).project_id is None
    assert await listed() == {FOLDER_A, FOLDER_B, registered}
    # A second runnable approval makes the lineage ambiguous: every run on this
    # device is refused, so no account folder may be registered or listed.
    await pair_account(db)
    assert await listed() == set()
    with pytest.raises(IntegrationAccessDenied):
        await bind_workspace(
            db,
            OWNER,
            ACCOUNT_DEVICE,
            WorkspaceBindingCreate(workspace_id=uuid4(), label="d"),
        )


# Mutation MA11: drop the `account and project_id is None` refusal in
# authorize_external_submission; this test must fail.
async def test_an_account_caller_that_names_no_project_is_a_plain_denial(
    db: AsyncSession, account: IntegrationContext
) -> None:
    """A caller bug is not shown as "project no longer reachable"."""
    from src.services.harness.account import ProjectOutsideAccount
    from src.services.harness.runs import authorize_external_submission

    with pytest.raises(IntegrationAccessDenied) as refused:
        await authorize_external_submission(
            db,
            context=account,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
            thread_id=THREAD_B,
        )
    assert not isinstance(refused.value, ProjectOutsideAccount)
    await leave_account(db)
    with pytest.raises(ProjectOutsideAccount):
        await authorize_external_submission(
            db,
            context=account,
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
            thread_id=THREAD_B,
            project_id=PROJECT_B,
        )
