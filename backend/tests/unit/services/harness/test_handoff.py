"""Machine handoff child runs: creation, idempotency and authority.

Mutation checks (2026-10-29, SQLite; source restored after each, each named
test observed FAIL). The parent refusals assert the exact opaque base class
and no minted grant, because later layers raise its subclasses and would
otherwise mask a missing org or nested-parent guard:

* removing the ``parent.status != RUNNING`` guard fails
  test_parent_must_be_running;
* removing the existing-child lookup fails
  test_creation_is_idempotent_by_tool_call_id (workspace 409 on the second
  call);
* removing the parent ``user_id`` / ``organization_id`` comparison fails
  test_another_users_parent_is_refused / test_another_orgs_parent_is_refused;
* removing the ``execution_provider != "nous"`` comparison fails
  test_a_top_level_codex_run_cannot_hand_off; removing the ``parent_run_id is
  not None`` comparison fails test_a_nested_nous_run_cannot_hand_off
  (test_a_child_cannot_hand_off_again needs both removed);
* removing the blank-brief check fails test_blank_brief_is_refused;
* removing the ``[:MAX_BRIEF_CHARS]`` cap fails test_brief_is_capped;
* dropping the ``handoff_tool_call_id`` predicate from existing_handoff_run
  fails test_locked_folder_is_refused (call-2 would return call-1's child);
* removing ``not run.parent_run_id`` in prompt_context.build_harness_prompt
  fails test_prompt_context.test_a_brief_without_a_parent_run_is_refused.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import WorkspaceBinding
from src.models.collection import Collection
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant
from src.models.thread import Thread
from src.schemas.integration_context import IntegrationContext
from src.services.agent.agent_run_service import ActiveRunConflict
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.handoff import MAX_BRIEF_CHARS, create_handoff_run
from src.services.integrations.context import (
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
)
from src.shared.enums import JobStatus
from tests.unit.services.harness.test_runs import (  # noqa: F401
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
BRIEF = "Run the tests and fix the failures."


@pytest.fixture(autouse=True)
def bridge_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)


async def parent_run(
    db: AsyncSession,
    status: JobStatus = JobStatus.RUNNING,
    thread_id: UUID = THREAD,
) -> str:
    """A NOUS chat run on ``thread_id``, moved to ``status``."""
    accepted = await accept_submission(
        db,
        current_user=OWNER,
        request=AgentExecuteRequest(
            messages=[
                AgentMessage(role="user", content="fix it", client_message_id=uuid4())
            ],
            thread_id=str(thread_id),
        ),
        thread=await db.get(Thread, thread_id),
    )
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == accepted.run_id)
        .values(status=status.value)
    )
    await db.commit()
    return accepted.run_id


async def handoff(
    db: AsyncSession, parent: str, call: str = "call-1", **kw: Any
) -> str:
    return await create_handoff_run(
        db,
        parent_run_id=parent,
        current_user=kw.get("user", OWNER),
        tool_call_id=call,
        brief=kw.get("brief", BRIEF),
        device_id=kw.get("device_id", DEVICE),
        workspace_id=kw.get("workspace_id", LOCAL),
    )


async def refused_before_minting(db: AsyncSession, parent: str, **kw: Any) -> None:
    """The parent check refuses with the opaque base denial, before any grant
    is minted (later layers raise subclasses and would mask a missing guard)."""
    grants = await db.scalar(select(func.count()).select_from(IntegrationGrant))
    with pytest.raises(IntegrationAccessDenied) as raised:
        await handoff(db, parent, **kw)
    assert type(raised.value) is IntegrationAccessDenied
    assert await db.scalar(select(func.count()).select_from(IntegrationGrant)) == grants


async def children(db: AsyncSession, parent: str) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.parent_run_id == parent)
    )
    return int(count or 0)


async def test_creates_a_queued_child_with_session_outbox_and_event(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent)
    child = await db.get(AgentRun, child_id, populate_existing=True)
    assert child is not None
    assert (child.parent_run_id, child.execution_provider, child.status) == (
        parent,
        "codex",
        JobStatus.QUEUED.value,
    )
    assert child.thread_id == THREAD and child.user_message_id is None
    assert child.project_id == PROJECT
    assert (child.user_id, child.organization_id) == (USER, ORG)
    assert child.run_metadata["handoff_brief"] == BRIEF
    assert child.run_metadata["handoff_tool_call_id"] == "call-1"
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == child_id)
    )
    assert session is not None and session.workspace_locked
    assert (session.device_id, session.workspace_id) == (DEVICE, LOCAL)
    grant = await db.get(IntegrationGrant, session.grant_id)
    assert grant is not None
    assert (grant.user_id, grant.project_id, grant.thread_id) == (
        USER,
        PROJECT,
        THREAD,
    )
    outbox = (
        await db.scalars(select(AgentOutbox).where(AgentOutbox.run_id == child_id))
    ).all()
    assert [row.kind for row in outbox] == ["harness.execute"]
    assert outbox[0].payload["parent_run_id"] == parent
    events = (
        await db.scalars(
            select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == child_id)
        )
    ).all()
    assert events == ["run.created"]
    # The parent keeps its own single-writer slot and status.
    parent_row = await db.get(AgentRun, parent, populate_existing=True)
    assert parent_row is not None and parent_row.status == JobStatus.RUNNING.value


async def test_creation_is_idempotent_by_tool_call_id(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    assert await handoff(db, parent) == await handoff(db, parent)
    assert await children(db, parent) == 1


async def test_replay_returns_the_child_after_the_parent_was_parked(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == parent)
        .values(status=JobStatus.DELEGATED.value)
    )
    await db.commit()
    assert await handoff(db, parent) == child


async def test_parent_must_be_running(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db, JobStatus.AWAITING_CONFIRMATION)
    await refused_before_minting(db, parent)
    assert await children(db, parent) == 0


async def test_unknown_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    await refused_before_minting(db, str(uuid4()))


async def test_another_users_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    stranger = SimpleNamespace(id=uuid4(), organization_id=ORG)
    await refused_before_minting(db, parent, user=stranger)
    assert await children(db, parent) == 0


async def test_another_orgs_parent_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    elsewhere = SimpleNamespace(id=USER, organization_id=uuid4())
    await refused_before_minting(db, parent, user=elsewhere)
    assert await children(db, parent) == 0


async def test_a_child_cannot_hand_off_again(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    # Even a running child is not a NOUS top-level run.
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == child)
        .values(status=JobStatus.RUNNING.value)
    )
    await db.commit()
    await refused_before_minting(db, child, call="call-2", workspace_id=uuid4())
    assert await children(db, child) == 0


async def test_a_nested_nous_run_cannot_hand_off(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    child = await handoff(db, parent)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == child)
        .values(status=JobStatus.RUNNING.value, execution_provider="nous")
    )
    await db.commit()
    await refused_before_minting(db, child, call="call-2", workspace_id=uuid4())
    assert await children(db, child) == 0


async def test_a_top_level_codex_run_cannot_hand_off(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == parent)
        .values(execution_provider="codex")
    )
    await db.commit()
    await refused_before_minting(db, parent)
    assert await children(db, parent) == 0


async def test_blank_brief_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await refused_before_minting(db, parent, brief="   \n")
    assert await children(db, parent) == 0


async def test_brief_is_capped(db: AsyncSession, context: IntegrationContext) -> None:
    parent = await parent_run(db)
    child_id = await handoff(db, parent, brief="x" * (MAX_BRIEF_CHARS + 500))
    child = await db.get(AgentRun, child_id, populate_existing=True)
    assert child is not None
    assert child.run_metadata["handoff_brief"] == "x" * MAX_BRIEF_CHARS


async def test_thread_without_a_project_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    await db.execute(
        update(Thread).where(Thread.id == THREAD).values(source_project_id=None)
    )
    await db.commit()
    with pytest.raises(ValueError):
        await handoff(db, parent)
    assert await children(db, parent) == 0


async def test_a_disabled_bridge_refuses(
    db: AsyncSession, context: IntegrationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = await parent_run(db)
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    with pytest.raises(ValueError):
        await handoff(db, parent)
    assert await children(db, parent) == 0


async def test_folder_of_another_project_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    other_project, other_folder = uuid4(), uuid4()
    db.add(Collection(id=other_project, workspace_id=WORKSPACE, name="other"))
    await db.flush()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE,
            workspace_id=other_folder,
            project_id=other_project,
            label="other",
        )
    )
    await db.commit()
    parent = await parent_run(db)
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, workspace_id=other_folder)
    assert await children(db, parent) == 0


async def test_unknown_folder_or_device_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    parent = await parent_run(db)
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, workspace_id=uuid4())
    with pytest.raises(DeviceNotConnectedToProject):
        await handoff(db, parent, device_id=uuid4())
    assert await children(db, parent) == 0


async def test_locked_folder_is_refused(
    db: AsyncSession, context: IntegrationContext
) -> None:
    first = await parent_run(db)
    await handoff(db, first)
    # A second parent cannot run on the same thread; use the same parent with
    # a new call id to target the still-locked folder.
    with pytest.raises(ActiveRunConflict):
        await handoff(db, first, call="call-2")
    assert await children(db, first) == 1
